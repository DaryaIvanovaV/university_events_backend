"""
SQLAlchemy 2.0 ORM-модели.

Две таблицы:

  events      — результат работы системы: структурированные поля события
                (для быстрых запросов по дате/типу) + raw_json извлечённого
                объекта + сырой текст источника + статус модерации;
  ingest_jobs — очередь входящих сообщений, ожидающих обработки LLM
                (см. класс IngestJob и app/worker.py).

content_hash = sha256(нормализованное название | дата) — используется для
нечёткой дедупликации (один и тот же анонс, переотправленный в разных
формулировках). Пара (source_channel, source_message_id) уникальна для
точной дедупликации повторной обработки того же поста.

raw_json: на PostgreSQL хранится как JSONB + GIN-индекс (быстрые запросы по
содержимому, напр. containment `@>`); на SQLite (dev/тесты) — generic JSON.
Портируемость — через JSON().with_variant(JSONB, "postgresql"); GIN-индекс
создаётся только на PostgreSQL (см. event.listen в конце файла).
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    DDL,
    JSON,
    Date,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.services.clock import utc_now


class Base(DeclarativeBase):
    pass


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)

    # --- Поля, извлечённые LLM ---
    event_title: Mapped[str] = mapped_column(String(512))
    date: Mapped[dt.date | None] = mapped_column(default=None)
    time: Mapped[str | None] = mapped_column(String(16), default=None)
    location: Mapped[str | None] = mapped_column(String(256), default=None)
    organizer: Mapped[str | None] = mapped_column(String(256), default=None)
    target_audience: Mapped[str | None] = mapped_column(String(256), default=None)
    event_type: Mapped[str] = mapped_column(String(32), default="other")
    description: Mapped[str | None] = mapped_column(String(2048), default=None)
    language: Mapped[str | None] = mapped_column(String(8), default=None)
    link: Mapped[str | None] = mapped_column(String(512), default=None)
    # ID конференции / код доступа к онлайн-встрече. Отдельно от link, чтобы
    # приложение рисовало кнопку «Приєднатися» (link) и чип с кодом, не
    # разбирая description регулярками.
    meeting_code: Mapped[str | None] = mapped_column(String(128), default=None)

    # --- Модерация ---
    # pending  — извлечено моделью, ждёт подтверждения администратором
    # approved — подтверждено (видно в приложении, ушёл/уйдёт push)
    # rejected — отклонено (запись сохраняется ради дедупликации)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)

    # --- Метаданные источника / дедупликации ---
    source_channel: Mapped[str | None] = mapped_column(String(128), default=None)
    source_message_id: Mapped[int | None] = mapped_column(default=None)
    raw_text: Mapped[str | None] = mapped_column(String(8192), default=None)
    # На PostgreSQL — JSONB (+ GIN-индекс ниже); на SQLite — generic JSON.
    raw_json: Mapped[dict] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), default=dict
    )
    content_hash: Mapped[str] = mapped_column(String(64), index=True)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # Момент последнего изменения записи. Нужен мобильному приложению для
    # инкрементальной синхронизации: без него клиент не может узнать, что
    # модератор поправил уже опубликованное событие (перенёс дату, сменил
    # аудиторию), и продолжал бы показывать устаревшую карточку.
    # onupdate срабатывает на любом ORM-UPDATE — и на правках модератора,
    # и на смене статуса, и на переобработке того же поста.
    #
    # Значение проставляет Python (utc_now), а НЕ func.now(). Разница
    # принципиальна на SQLite: CURRENT_TIMESTAMP пишет время с точностью до
    # секунды, а SQLAlchemy подставляет в запрос значение с микросекундами,
    # и сравнение в keyset-курсоре синхронизации не совпадает ни по «=»,
    # ни по «>» — постраничный обход застревал на первой странице.
    # server_default оставлен ради backfill существующих строк в миграции.
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        default=utc_now,
        onupdate=utc_now,
    )

    __table_args__ = (
        UniqueConstraint(
            "source_channel", "source_message_id", name="uq_source_message"
        ),
        Index("ix_events_date", "date"),
        Index("ix_events_type", "event_type"),
        # Основной запрос фида — WHERE status=? AND date>=? ORDER BY date.
        # Отдельные индексы по status и date его покрывают хуже составного.
        Index("ix_events_status_date", "status", "date"),
        # Инкрементальная синхронизация приложения: WHERE updated_at >= ?
        Index("ix_events_updated_at", "updated_at"),
    )


# GIN-индекс по raw_json — ускоряет запросы по содержимому JSONB (@>, ?, ?| ...).
# Только для PostgreSQL: DDL с execute_if(dialect="postgresql") на SQLite
# пропускается, поэтому dev/тесты на SQLite не затрагиваются. Индекс создаётся
# в момент create_all (событие after_create таблицы events).
event.listen(
    Event.__table__,
    "after_create",
    DDL(
        "CREATE INDEX IF NOT EXISTS ix_events_raw_json_gin "
        "ON events USING gin (raw_json)"
    ).execute_if(dialect="postgresql"),
)


# --- Очередь обработки входящих сообщений ---

# Статусы задачи. Строки, а не Enum в БД: добавить статус миграцией по строке
# дешевле, чем ALTER TYPE, а проверок на уровне БД тут не требуется.
JOB_QUEUED = "queued"
JOB_PROCESSING = "processing"
JOB_DONE = "done"
JOB_FAILED = "failed"


class IngestJob(Base):
    """
    Задача «обработать одно сообщение»: сырой текст + метаданные источника.

    Очередь живёт в той же БД, а не в отдельном брокере. Причины:

      * одна система вместо двух: один бэкап, одна точка отказа, никакого
        расхождения «сообщение из брокера подтверждено, а запись в БД не
        сохранилась». Доставка здесь at-least-once, и это безопасно: повторная
        обработка того же поста идемпотентна — `crud.upsert_event` дедуплицирует
        и по (source_channel, source_message_id), и по content_hash;
      * не нужен ещё один сервис в docker-compose и его persistence;
      * вся история обработки видна обычным SQL. Отсюда же берутся метрики для
        статьи: latency (`duration_ms`), доля отсеянных, число ретраев.

    Воркер забирает задачи через `SELECT ... FOR UPDATE SKIP LOCKED` — это
    штатный для PostgreSQL способ раздать строки нескольким читателям без
    блокировок и дублей: занятые строки следующий воркер просто пропускает.
    """

    __tablename__ = "ingest_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)

    status: Mapped[str] = mapped_column(String(16), default=JOB_QUEUED, index=True)

    # --- Полезная нагрузка (то же, что раньше уходило прямо в Pipeline) ---
    text: Mapped[str] = mapped_column(Text)
    source_channel: Mapped[str | None] = mapped_column(String(128), default=None)
    source_message_id: Mapped[int | None] = mapped_column(default=None)
    # Дата публикации — без неё не разрешить «завтра» и «наступної п'ятниці».
    reference_date: Mapped[dt.date | None] = mapped_column(Date, default=None)

    # --- Исполнение ---
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, default=None)
    # Раньше какого момента задачу не брать. Так делается экспоненциальная
    # пауза перед повтором: Ollama упала — не долбим её раз в две секунды.
    available_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    locked_by: Mapped[str | None] = mapped_column(String(64), default=None)
    # Момент захвата. Если задача висит в processing дольше lease-таймаута,
    # значит воркер убит на середине — задача возвращается в очередь.
    locked_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )

    # --- Результат ---
    # id сохранённых событий и список отсеянных с причиной. Без этого HTTP-
    # клиент, получивший 202, никогда не узнал бы, чем кончилась его задача.
    event_ids: Mapped[list] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), default=list
    )
    skipped: Mapped[list] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), default=list
    )
    duration_ms: Mapped[int | None] = mapped_column(Integer, default=None)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    started_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    finished_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )

    __table_args__ = (
        # Запрос захвата: WHERE status=? AND available_at<=? ORDER BY id.
        Index("ix_jobs_claim", "status", "available_at", "id"),
        # Поиск уже стоящей в очереди задачи по тому же посту (антидубль).
        Index("ix_jobs_source", "source_channel", "source_message_id"),
    )
