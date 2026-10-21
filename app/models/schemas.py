"""
Pydantic-схемы системы.

Разделяем:
  * ExtractedEvent / EventList — то, что ВОЗВРАЩАЕТ LLM. Именно её
    JSON-schema передаётся в Ollama через `format=` (constrained decoding).
  * EventOut — то, что ОТДАЁТ REST API (поля LLM + метаданные + статус).
  * EventUpdate — PATCH-правки администратора на этапе модерации.
  * DeviceRegistration / IngestRequest — служебные.
  * SyncResponse — инкрементальная синхронизация мобильного приложения:
    что изменилось и что больше не показывать.
  * IngestAccepted / JobOut / Stats — очередь обработки: /events/ingest теперь
    отвечает 202 с номером задачи, а не готовыми событиями (извлечение делает
    отдельный воркер, см. app/worker.py).

Имена ключевых полей (event_title, date, time, location, target_audience)
совпадают с примером JSON из статьи.
"""

from __future__ import annotations

import datetime as dt
import re
from enum import Enum

from pydantic import BaseModel, Field, field_validator

# Набор символов, который FCM принимает в имени темы.
_FCM_TOPIC_RE = re.compile(r"[a-zA-Z0-9\-_.~%]+")


class EventType(str, Enum):
    """Тип события для классификации в календаре."""

    conference = "conference"
    lecture = "lecture"
    webinar = "webinar"
    seminar = "seminar"
    # workshop / hackathon добавлены по итогам разметки: майстер-клас
    # раньше падал в seminar, а олимпиада по программированию — в exam.
    workshop = "workshop"
    hackathon = "hackathon"
    deadline = "deadline"
    exam = "exam"
    # consultation — вспомогательный сигнал для фильтра индивидуальных
    # консультаций. САМ ПО СЕБЕ поводом отсеять не является: консультация
    # перед экзаменом для потока — массовое мероприятие. Решение принимает
    # app/services/audience.py по маркерам текста.
    consultation = "consultation"
    other = "other"


class EventStatus(str, Enum):
    """Статус модерации (этап «Перевірка та підтвердження» из статьи)."""

    pending = "pending"
    approved = "approved"
    rejected = "rejected"
    # expired — подія минула і прибрана автоочищенням (app/db/retention.py).
    # Не рішення модератора: запис лишається заради дедуплікації, а клієнт
    # отримує його id у списку `removed` інкрементальної синхронізації.
    expired = "expired"


class ExtractedEvent(BaseModel):
    """
    Одно событие, извлечённое моделью из текста объявления.

    Все поля кроме event_title — Optional с дефолтом None: модель ОБЯЗАНА
    вернуть null для отсутствующих данных, а не выдумывать их.
    """

    event_title: str = Field(description="Краткое название мероприятия")
    date: dt.date | None = Field(
        default=None,
        description="Абсолютная дата в формате YYYY-MM-DD. "
        "Относительные даты ('завтра', 'наступної п'ятниці') нужно "
        "преобразовать в абсолютные относительно даты-ориентира.",
    )
    time: str | None = Field(
        default=None, description="Время начала в формате HH:MM (24ч), либо null"
    )
    location: str | None = Field(
        default=None, description="Место проведения / аудитория, либо null"
    )
    organizer: str | None = Field(
        default=None, description="Организатор мероприятия, либо null"
    )
    target_audience: str | None = Field(
        default=None, description="Целевая аудитория (например, '4 курс'), либо null"
    )
    event_type: EventType = Field(
        default=EventType.other, description="Тип мероприятия"
    )
    description: str | None = Field(
        default=None, description="Короткое описание (1-2 предложения), либо null"
    )
    language: str | None = Field(
        default=None, description="Язык оригинала: 'uk', 'ru', 'en' и т.п."
    )
    link: str | None = Field(
        default=None,
        description="Полный URL регистрации или страницы с деталями "
        "(http/https), либо null. Не выдумывать — только если ссылка есть "
        "в тексте.",
    )
    meeting_code: str | None = Field(
        default=None,
        description="Идентификатор конференции и/или код доступа к онлайн-"
        "встрече (Zoom/Meet/Teams), например '845 2371 9004' или "
        "'ID 845 2371 9004, код 316742'. Отдельно от link: код — не URL. "
        "Либо null.",
    )


class EventList(BaseModel):
    """
    Обёртка-список. Сообщение может содержать 0, 1 или несколько событий.
    Эта модель — корневая схема для Ollama `format=`.
    """

    events: list[ExtractedEvent] = Field(default_factory=list)


# --- Схемы REST API ---


class EventOut(ExtractedEvent):
    """Событие, как его отдаёт API: поля LLM + статус + метаданные."""

    id: int
    status: EventStatus
    source_channel: str | None = None
    source_message_id: int | None = None
    created_at: dt.datetime
    # Момент последнего изменения. Мобильное приложение использует его как
    # курсор синхронизации (см. GET /events/sync).
    updated_at: dt.datetime

    model_config = {"from_attributes": True}


class EventDetail(EventOut):
    """
    Детальное представление одного события: всё из EventOut + оригинальный
    текст источника (raw_text).

    raw_text намеренно НЕ входит в публичный список GET /events (может содержать
    лишний текст/персональные данные) — только в GET /events/{id} для модерации.
    """

    raw_text: str | None = None


class EventUpdate(BaseModel):
    """
    PATCH-правки администратора (модерация).

    Применяются только явно переданные поля (exclude_unset). Значение null
    очищает поле; обязательные (event_title, event_type) очистить нельзя.

    Ограничения длины повторяют размеры колонок из app/models/db.py. Здесь они
    уместны, а в ExtractedEvent — нет: её JSON-schema уходит в Ollama как
    `format=`, и maxLength превратился бы в ограничения грамматики
    constrained decoding. Слишком длинные значения от МОДЕЛИ обрезаются
    в crud._apply; здесь же источник — человек, и честнее вернуть ему 422.
    """

    event_title: str | None = Field(default=None, max_length=512)
    date: dt.date | None = None
    time: str | None = Field(default=None, max_length=16)
    location: str | None = Field(default=None, max_length=256)
    organizer: str | None = Field(default=None, max_length=256)
    target_audience: str | None = Field(default=None, max_length=256)
    event_type: EventType | None = None
    description: str | None = Field(default=None, max_length=2048)
    language: str | None = Field(default=None, max_length=8)
    link: str | None = Field(default=None, max_length=512)
    meeting_code: str | None = Field(default=None, max_length=128)


class DeviceRegistration(BaseModel):
    """
    Регистрация устройства для push-уведомлений.

    Имя темы ограничено набором символов, который принимает FCM
    (`[a-zA-Z0-9-_.~%]+`). Проверяем на входе: иначе мусорная строка от
    клиента дошла бы до `messaging.subscribe_to_topic` и упала бы там
    невнятной ошибкой Firebase вместо понятного 422.
    """

    fcm_token: str = Field(min_length=1, max_length=4096)
    topics: list[str] = Field(
        default_factory=lambda: ["conferences"],
        min_length=1,
        max_length=20,
    )

    @field_validator("topics")
    @classmethod
    def _check_topics(cls, value: list[str]) -> list[str]:
        for topic in value:
            if not _FCM_TOPIC_RE.fullmatch(topic):
                raise ValueError(
                    f"недопустимое имя темы {topic!r}: разрешены символы "
                    "a-z A-Z 0-9 - _ . ~ %"
                )
        return value


class SyncResponse(BaseModel):
    """
    Ответ инкрементальной синхронизации (GET /events/sync).

    `changed` — опубликованные события, изменившиеся с момента `since`
    (в том числе появившиеся впервые). `removed` — id тех, что больше не
    должны показываться: модератор их отклонил (rejected) либо их прибрала
    автоочистка прошедших (expired, см. app/db/retention.py). Без второго
    списка клиент с локальным кэшем продолжал бы показывать отменённое
    мероприятие — для календаря вуза это самый неприятный вид ошибки.

    `cursor` — непрозрачная строка: клиент сохраняет её и возвращает в
    следующем запросе, не разбирая содержимое. Внутри пара (время, id), а не
    одно время: при совпадающих `updated_at` курсор по одному времени не
    сдвигался бы дальше, и синхронизация зациклилась бы на одной странице.
    Формат — деталь реализации и может измениться.

    Часы устройства для курсора не годятся вовсе: телефон может отставать
    или спешить, и события в этом промежутке потерялись бы.

    `has_more` — выдача упёрлась в `limit`, нужно повторить запрос сразу же
    с новым курсором.
    """

    cursor: str
    has_more: bool = False
    changed: list[EventOut] = Field(default_factory=list)
    removed: list[int] = Field(default_factory=list)


class IngestRequest(BaseModel):
    """Сырое сообщение, которое бот/импортёр шлёт в пайплайн."""

    # Предел Telegram — 4096 символов на сообщение; берём с запасом, но не
    # безлимитно: эндпоинт открыт для бота, и мегабайтный текст не должен
    # доехать до инференса.
    text: str = Field(min_length=1, max_length=32768)
    source_channel: str | None = Field(default=None, max_length=128)
    source_message_id: int | None = None
    reference_date: dt.date | None = Field(
        default=None,
        description="Дата публикации сообщения — нужна для разрешения "
        "относительных дат. Если null, берётся сегодняшняя.",
    )


# --- Очередь обработки ---


class JobStatus(str, Enum):
    """Статус задачи в очереди (таблица ingest_jobs)."""

    queued = "queued"
    processing = "processing"
    done = "done"
    failed = "failed"


class IngestAccepted(BaseModel):
    """
    Ответ на POST /events/ingest: сообщение принято в очередь.

    События здесь ещё нет — извлечение выполняет отдельный воркер. Следить
    за результатом: GET /jobs/{job_id}.
    """

    job_id: int
    status: JobStatus
    duplicate: bool = Field(
        default=False,
        description="true — этот пост уже стоял в очереди, новая задача "
        "не создавалась (вернули существующую).",
    )


class SkippedEventOut(BaseModel):
    """Событие, которое модель извлекла, а пайплайн решил не сохранять."""

    event_title: str
    scope: str = Field(description="language | individual | stale")
    reason: str


class JobOut(BaseModel):
    """Состояние задачи обработки."""

    id: int
    status: JobStatus
    source_channel: str | None = None
    source_message_id: int | None = None
    attempts: int = 0
    last_error: str | None = None
    # Заполняются, когда status=done.
    event_ids: list[int] = Field(default_factory=list)
    skipped: list[SkippedEventOut] = Field(default_factory=list)
    duration_ms: int | None = None
    created_at: dt.datetime
    started_at: dt.datetime | None = None
    finished_at: dt.datetime | None = None

    model_config = {"from_attributes": True}


class QueueStats(BaseModel):
    """Сводка по очереди для GET /stats."""

    queued: int = 0
    processing: int = 0
    done: int = 0
    failed: int = 0
    avg_duration_ms: int | None = None
    max_duration_ms: int | None = None
    measured_jobs: int = 0


class Stats(BaseModel):
    """
    Эксплуатационная сводка: сколько событий в каждом статусе модерации и как
    идёт обработка очереди. Тот же набор цифр нужен для таблицы результатов
    в статье.
    """

    events: dict[str, int]
    queue: QueueStats
