"""
CRUD + логика дедупликации + операции модерации.

upsert_event возвращает (Event, is_new):
  * is_new=True  — событие добавлено впервые (в статусе pending)
  * is_new=False — дубль (обновили существующую запись либо пропустили)

Два уровня дедупликации:
  1. Точная: совпадение (source_channel, source_message_id) — тот же пост
     обработан повторно. Обновляем запись.
  2. Нечёткая: совпадение content_hash (нормализованное название + дата) —
     тот же анонс из другого сообщения. Пропускаем. Работает и для
     rejected-записей: отклонённый анонс не появится снова.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import re
import unicodedata

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.db import Event
from app.models.schemas import EventType, ExtractedEvent
from app.services.clock import today

logger = logging.getLogger(__name__)

# Пределы длины колонок из app/models/db.py. PostgreSQL на превышении бросает
# StringDataRightTruncation и роняет запрос 500-й, а SQLite длину игнорирует —
# то есть на тестах такую ошибку не поймать. Обрезаем сами: модель вполне может
# выдать description длиннее лимита, и терять из-за этого всё событие нелепо.
_MAX_LEN = {
    "event_title": 512,
    "time": 16,
    "location": 256,
    "organizer": 256,
    "target_audience": 256,
    "event_type": 32,
    "description": 2048,
    "language": 8,
    "link": 512,
    "meeting_code": 128,
    "raw_text": 8192,
}


def _fit(field: str, value: str | None) -> str | None:
    """Обрезает значение до размера колонки, сообщая об этом в лог."""
    limit = _MAX_LEN[field]
    if value is None or len(value) <= limit:
        return value
    logger.warning(
        "Поле %s длиной %s символов обрезано до %s", field, len(value), limit
    )
    return value[:limit]


def _normalize_title(title: str) -> str:
    """Нижний регистр, схлопывание пробелов, удаление пунктуации."""
    text = unicodedata.normalize("NFKC", title).casefold()
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def compute_content_hash(title: str, date: dt.date | None) -> str:
    """Стабильный хэш для нечёткой дедупликации."""
    key = f"{_normalize_title(title)}|{date.isoformat() if date else ''}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def upsert_event(
    session: Session,
    event: ExtractedEvent,
    *,
    raw_text: str | None = None,
    source_channel: str | None = None,
    source_message_id: int | None = None,
) -> tuple[Event, bool]:
    content_hash = compute_content_hash(event.event_title, event.date)
    raw_json = event.model_dump(mode="json")

    # 1. Точная дедупликация по message_id.
    if source_channel is not None and source_message_id is not None:
        existing = session.scalar(
            select(Event).where(
                Event.source_channel == source_channel,
                Event.source_message_id == source_message_id,
            )
        )
        if existing is not None:
            _apply(existing, event, raw_text, raw_json, content_hash)
            session.commit()
            session.refresh(existing)
            return existing, False

    # 2. Нечёткая дедупликация по content_hash.
    dup = session.scalar(select(Event).where(Event.content_hash == content_hash))
    if dup is not None:
        return dup, False

    # 3. Новое событие (статус pending по умолчанию — ждёт модерации).
    db_event = Event(
        source_channel=source_channel,
        source_message_id=source_message_id,
        raw_text=_fit("raw_text", raw_text),
        raw_json=raw_json,
        content_hash=content_hash,
    )
    _apply(db_event, event, raw_text, raw_json, content_hash)
    session.add(db_event)
    try:
        session.commit()
    except IntegrityError:
        # Гонка: параллельный воркер (или повторная доставка того же поста)
        # успел вставить эту же запись между нашими SELECT и INSERT. Проверки
        # выше на это не рассчитаны — они видят состояние на момент чтения.
        # Уникальный индекс отработал как надо, осталось подобрать чужую
        # запись и вернуть её как дубль.
        session.rollback()
        existing = _find_duplicate(
            session, content_hash, source_channel, source_message_id
        )
        if existing is None:
            raise  # конфликт не по дедупликации — прятать его нельзя
        logger.info(
            "Событие «%s» уже вставлено параллельно — возвращаю существующее",
            event.event_title,
        )
        return existing, False
    session.refresh(db_event)
    return db_event, True


def _find_duplicate(
    session: Session,
    content_hash: str,
    source_channel: str | None,
    source_message_id: int | None,
) -> Event | None:
    """Ищет запись, из-за которой сорвалась вставка (после IntegrityError)."""
    if source_channel is not None and source_message_id is not None:
        found = session.scalar(
            select(Event).where(
                Event.source_channel == source_channel,
                Event.source_message_id == source_message_id,
            )
        )
        if found is not None:
            return found
    return session.scalar(select(Event).where(Event.content_hash == content_hash))


def _apply(
    db_event: Event,
    event: ExtractedEvent,
    raw_text: str | None,
    raw_json: dict,
    content_hash: str,
) -> None:
    """Копирует поля схемы в ORM-объект, обрезая их под размер колонок."""
    db_event.event_title = _fit("event_title", event.event_title)
    db_event.date = event.date
    db_event.time = _fit("time", event.time)
    db_event.location = _fit("location", event.location)
    db_event.organizer = _fit("organizer", event.organizer)
    db_event.target_audience = _fit("target_audience", event.target_audience)
    db_event.event_type = event.event_type.value
    db_event.description = _fit("description", event.description)
    db_event.language = _fit("language", event.language)
    db_event.link = _fit("link", event.link)
    db_event.meeting_code = _fit("meeting_code", event.meeting_code)
    # ВАЖНО: content_hash считается только по названию и дате. Новые поля в
    # него не входят намеренно — иначе все ранее сохранённые записи перестали
    # бы совпадать по хэшу и продублировались бы при переобработке.
    db_event.content_hash = content_hash
    db_event.raw_json = raw_json
    if raw_text is not None:
        db_event.raw_text = _fit("raw_text", raw_text)


def get_event(session: Session, event_id: int) -> Event | None:
    return session.get(Event, event_id)


def set_event_status(session: Session, event_id: int, status: str) -> Event | None:
    ev = session.get(Event, event_id)
    if ev is None:
        return None
    ev.status = status
    session.commit()
    session.refresh(ev)
    return ev


def update_event_fields(
    session: Session, event_id: int, fields: dict
) -> Event | None:
    """
    Точечное обновление полей (правки администратора). Пересчитывает
    content_hash. raw_json намеренно НЕ трогаем — это исходный вывод
    модели (provenance); исправленные значения живут в колонках.
    """
    ev = session.get(Event, event_id)
    if ev is None:
        return None
    for key, value in fields.items():
        if key in {"event_title", "event_type"} and value is None:
            continue  # обязательные поля нельзя очистить
        if isinstance(value, EventType):
            value = value.value
        setattr(ev, key, value)
    ev.content_hash = compute_content_hash(ev.event_title, ev.date)
    session.commit()
    session.refresh(ev)
    return ev


def _events_filter(
    status: str | None,
    upcoming_only: bool,
    date_from: dt.date | None,
    date_to: dt.date | None,
    event_type: str | None,
):
    """Условия WHERE, общие для выборки и подсчёта — чтобы не разъехались."""
    conditions = []
    if status is not None and status != "all":
        conditions.append(Event.status == status)
    if upcoming_only:
        # today() из app/services/clock, а не dt.date.today(): в контейнере
        # время процесса — UTC, и ночью фид показывал бы вчерашние события.
        conditions.append(Event.date >= today())
    if date_from is not None:
        conditions.append(Event.date >= date_from)
    if date_to is not None:
        conditions.append(Event.date <= date_to)
    if event_type is not None:
        conditions.append(Event.event_type == event_type)
    return conditions


def list_events(
    session: Session,
    *,
    status: str | None = "approved",
    upcoming_only: bool = False,
    date_from: dt.date | None = None,
    date_to: dt.date | None = None,
    event_type: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[Event]:
    """По умолчанию отдаёт только approved — публичный фид приложения."""
    conditions = _events_filter(
        status, upcoming_only, date_from, date_to, event_type
    )
    stmt = (
        select(Event)
        .where(*conditions)
        # Вторичная сортировка по id: без неё порядок событий с одинаковой
        # датой не определён, и постраничная выдача может показать одну
        # запись дважды, а другую пропустить.
        .order_by(Event.date.is_(None), Event.date.asc(), Event.id.asc())
        .limit(limit)
        .offset(offset)
    )
    return list(session.scalars(stmt))


def count_events(
    session: Session,
    *,
    status: str | None = "approved",
    upcoming_only: bool = False,
    date_from: dt.date | None = None,
    date_to: dt.date | None = None,
    event_type: str | None = None,
) -> int:
    """Всего записей под теми же фильтрами — уезжает в заголовок X-Total-Count."""
    conditions = _events_filter(
        status, upcoming_only, date_from, date_to, event_type
    )
    return int(
        session.scalar(select(func.count(Event.id)).where(*conditions)) or 0
    )


def changed_since(
    session: Session,
    after: tuple[dt.datetime, int] | None,
    *,
    statuses: tuple[str, ...] = ("approved", "rejected"),
    limit: int = 500,
) -> list[Event]:
    """
    Изменённые события ОДНИМ потоком, упорядоченным по (updated_at, id).

    Курсор составной — (время, id), а не одно время. Иначе синхронизация
    ломается, когда у нескольких записей `updated_at` совпадает: страница
    размером `limit` не сдвигает курсор дальше этого момента, и клиент
    вечно получает одну и ту же порцию. Это не теоретический случай — в
    PostgreSQL `now()` возвращает время НАЧАЛА транзакции, поэтому массовое
    подтверждение событий в одной транзакции проставит им всем один и тот
    же `updated_at`.

    Оба статуса выбираются вместе намеренно: два независимых запроса
    потребовали бы двух курсоров, и продвинуть их согласованно было бы
    нечем. Разделение на «изменилось» и «убрать» делает вызывающий код.
    """
    stmt = select(Event).where(Event.status.in_(statuses))
    if after is not None:
        moment, last_id = after
        stmt = stmt.where(
            (Event.updated_at > moment)
            | ((Event.updated_at == moment) & (Event.id > last_id))
        )
    return list(
        session.scalars(
            stmt.order_by(Event.updated_at.asc(), Event.id.asc()).limit(limit)
        )
    )


def status_counts(session: Session) -> dict[str, int]:
    """Сколько событий в каждом статусе модерации (для GET /stats)."""
    counts = {
        status: int(count)
        for status, count in session.execute(
            select(Event.status, func.count()).group_by(Event.status)
        )
    }
    for status in ("pending", "approved", "rejected"):
        counts.setdefault(status, 0)
    counts["total"] = sum(
        v for k, v in counts.items() if k != "total"
    )
    return counts
