"""
Оркестрация пайплайна обработки одного сообщения.

raw text → prefilter → фильтр языка → LLM extract → нормализация встречи
        → фильтр охвата → фильтр актуальности → upsert (dedup) → PENDING.

Уведомления здесь НЕ отправляются: событие сначала попадает в очередь
модерации и ждёт подтверждения администратором (этап из статьи).
Push уходит на этапе approve — см. app/services/moderation.py.

Два вида сбоя LLM различаются намеренно (см. шаг 3): невалидный JSON гасится
здесь и сообщение считается обработанным, а недоступность модели пробрасывается
наверх, чтобы воркер вернул задачу в очередь.

Язык проверяется ДО вызова LLM: канал украиноязычный, русские сообщения
в календарь не идут, и тратить на них инференс незачем.

Шаги между извлечением и сохранением:
  * normalize_meeting — раскладывает ссылку, ID конференции и код доступа по
    отдельным полям (модель теряет коды, см. app/services/meeting.py);
  * normalize_schedule — чинит «HH:MM-HH:MM» и считает даты по дню недели:
    эту арифметику модель проваливает системно (см. schedule.py);
  * classify_scope — индивидуальные консультации преподавателей в общий
    календарь не сохраняются вовсе (см. app/services/audience.py);
  * stale_reason — отчёты о прошедшем («вчора завершилася конференція») тоже
    не сохраняются: модератор всё равно их отклонит (см. relevance.py).
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.db import crud
from app.llm.extractor import EventExtractor, ExtractionError, LLMUnavailableError
from app.models.db import Event
from app.models.schemas import ExtractedEvent
from app.services.audience import classify_scope
from app.services.language import unsupported_reason
from app.services.meeting import normalize_meeting
from app.services.schedule import normalize_schedule
from app.services.prefilter import prefilter
from app.services.relevance import stale_reason

logger = logging.getLogger(__name__)


@dataclass
class SkippedEvent:
    """Событие, которое модель извлекла, но пайплайн решил не сохранять."""

    event: ExtractedEvent
    scope: str
    reason: str


@dataclass
class ProcessResult:
    saved: list[Event] = field(default_factory=list)
    skipped: list[SkippedEvent] = field(default_factory=list)


class Pipeline:
    def __init__(
        self, extractor: EventExtractor, prefilter_threshold: float = 1.0
    ) -> None:
        self.extractor = extractor
        self.prefilter_threshold = prefilter_threshold

    def process(
        self,
        session: Session,
        text: str,
        *,
        source_channel: str | None = None,
        source_message_id: int | None = None,
        reference_date: dt.date | None = None,
    ) -> ProcessResult:
        result = ProcessResult()

        # 1. Дешёвый отсев болтовни.
        pf = prefilter(text, threshold=self.prefilter_threshold)
        if not pf.is_candidate:
            logger.debug("Пропущено пре-фильтром (score=%.1f)", pf.score)
            return result

        # 2. Язык: обрабатываем только украинские объявления.
        wrong_language = unsupported_reason(text)
        if wrong_language is not None:
            logger.info("Пропущено повідомлення: %s", wrong_language)
            return result

        # 3. Извлечение событий моделью.
        try:
            event_list = self.extractor.extract(text, reference_date)
        except LLMUnavailableError:
            # Сервис недоступен — это временно. Пробрасываем наверх, чтобы
            # воркер вернул задачу в очередь: иначе забытый `ollama pull`
            # молча превратил бы каждое сообщение в «обработано, событий 0».
            raise
        except ExtractionError:
            # А вот невалидный JSON воспроизводим: при temperature=0 повтор
            # даст тот же ответ, повторять задачу незачем.
            logger.warning("LLM не вернула валидный JSON для сообщения")
            return result

        # Модель иногда дробит одно событие на несколько одинаковых объектов
        # (наблюдалось на сообщениях 201 и 225 датасета: два экземпляра с
        # тем же названием и датой). Промтом это не лечится — проверено, —
        # а вот в коде решается детерминированно. Без отсева бот показал бы
        # администратору две одинаковые карточки модерации.
        seen_keys: set[tuple[str, dt.date | None]] = set()

        for event in event_list.events:
            # 4. Ссылка / код встречи по своим полям + время и дата.
            event = normalize_meeting(event, text)
            event = normalize_schedule(event, text, reference_date)

            key = (event.event_title.strip().casefold(), event.date)
            if key in seen_keys:
                logger.info(
                    "Пропускаю дубль у межах повідомлення: «%s»", event.event_title
                )
                continue
            seen_keys.add(key)

            # Модель могла определить язык иначе, чем детектор по буквам.
            lang_issue = unsupported_reason(text, event.language)
            if lang_issue is not None:
                logger.info("Не зберігаю «%s»: %s", event.event_title, lang_issue)
                result.skipped.append(SkippedEvent(event, "language", lang_issue))
                continue

            # 5. Индивидуальные консультации в общий календарь не идут.
            verdict = classify_scope(event, text)
            if verdict.is_individual:
                logger.info(
                    "Не зберігаю «%s»: індивідуальний формат (%s)",
                    event.event_title, verdict.reason,
                )
                result.skipped.append(
                    SkippedEvent(event, verdict.scope, verdict.reason)
                )
                continue

            # 6. Отчёты о прошедшем не занимают очередь модерации.
            stale = stale_reason(event, reference_date, text)
            if stale is not None:
                logger.info("Не зберігаю «%s»: %s", event.event_title, stale)
                result.skipped.append(SkippedEvent(event, "stale", stale))
                continue

            # 7. Сохранение + дедупликация. Новые события → pending.
            db_event, is_new = crud.upsert_event(
                session,
                event,
                raw_text=text,
                source_channel=source_channel,
                source_message_id=source_message_id,
            )
            result.saved.append(db_event)
            if is_new:
                logger.info(
                    "Нова подія у черзі модерації: %s", db_event.event_title
                )
        return result

    def process_message(
        self,
        session: Session,
        text: str,
        *,
        source_channel: str | None = None,
        source_message_id: int | None = None,
        reference_date: dt.date | None = None,
    ) -> list[Event]:
        """Совместимая обёртка: только сохранённые события."""
        return self.process(
            session,
            text,
            source_channel=source_channel,
            source_message_id=source_message_id,
            reference_date=reference_date,
        ).saved
