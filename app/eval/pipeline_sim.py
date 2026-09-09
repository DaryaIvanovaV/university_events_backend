"""
Симуляция судьбы извлечённого события: попадёт ли оно в БД и в фид приложения.

Проверка промта работает БЕЗ базы, но главный практический вопрос — не «что
модель извлекла», а «что из этого реально доедет до модератора и до студента».
Здесь повторяются решения настоящего пайплайна:

  app/services/pipeline.py:43-46  пре-фильтр отсекает сообщение до вызова LLM
  app/db/crud.py:52-86            нечёткая дедупликация по content_hash
  app/db/crud.py:161-172          фид отдаёт только approved и date >= порога

compute_content_hash берётся из app/db/crud НАПРЯМУЮ, а не переписывается:
хэш обязан совпадать с продовым, иначе симуляция врёт. Импорт безопасен —
app.db.crud не тянет за собой движок БД (app.db.database не загружается).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from app.db.crud import compute_content_hash
from app.models.schemas import ExtractedEvent
from app.services.audience import classify_scope
from app.services.language import unsupported_reason
from app.services.relevance import stale_reason

# Судьба записи в БД.
SAVED = "saved"  # ляжет как pending и попадёт в очередь модерации
DUPLICATE = "duplicate"  # отсечёт нечёткая дедупликация (тот же анонс)
FILTERED = "filtered"  # пре-фильтр не пропустил бы сообщение к LLM
INDIVIDUAL = "individual"  # индивидуальная консультация — в календарь не идёт
STALE = "stale"  # отчёт о прошедшем — событие устарело уже при публикации
LANGUAGE = "language"  # сообщение не на украинском — канал украиноязычный

# Судьба в публичном фиде (после подтверждения администратором).
FEED = "feed"  # покажется в GET /events?upcoming=true
FEED_NO_DATE = "no_date"  # date=null: SQL-фильтр date >= ... его не пропустит
FEED_PAST = "past"  # дата раньше даты публикации сообщения

DB_LABEL = {
    SAVED: "→ БД (pending)",
    DUPLICATE: "дубль — не збережеться",
    FILTERED: "відсіяно пре-фільтром",
    INDIVIDUAL: "індивідуальна консультація — не зберігається",
    STALE: "подія вже минула — не зберігається",
    LANGUAGE: "не українською — не зберігається",
}
FEED_LABEL = {
    FEED: "у фід: так (після approve)",
    FEED_NO_DATE: "у фід: ні — немає дати",
    FEED_PAST: "у фід: ні — дата минула",
}


@dataclass
class Destiny:
    """Что стало бы с этим событием в настоящем пайплайне."""

    db: str
    feed: str  # пусто, если до БД дело не дошло
    content_hash: str
    note: str = ""

    @property
    def reaches_db(self) -> bool:
        return self.db == SAVED

    @property
    def reaches_feed(self) -> bool:
        return self.feed == FEED

    def to_dict(self) -> dict:
        return {
            "db": self.db,
            "feed": self.feed,
            "content_hash": self.content_hash,
            "note": self.note,
        }


class PipelineSimulator:
    """
    Хранит состояние между сообщениями — дедупликация сквозная по прогону.

    Симулируется обработка всей пачки в ПУСТУЮ базу: первое вхождение анонса
    сохраняется, повторы отсекаются, как это сделал бы crud.upsert_event.
    """

    def __init__(self) -> None:
        # content_hash -> индекс сообщения, где событие встретилось впервые
        self._seen: dict[str, int] = {}

    def decide(
        self,
        event: ExtractedEvent,
        *,
        index: int,
        prefilter_candidate: bool,
        reference_date: dt.date,
        source_text: str = "",
    ) -> Destiny:
        content_hash = compute_content_hash(event.event_title, event.date)

        if not prefilter_candidate:
            return Destiny(
                FILTERED, "", content_hash,
                "у проді пре-фільтр відсік би повідомлення — LLM не викликали б",
            )

        # Канал украиноязычный: русское сообщение до LLM не доходит.
        lang_issue = unsupported_reason(source_text, event.language)
        if lang_issue is not None:
            return Destiny(LANGUAGE, "", content_hash, lang_issue)

        # Индивидуальные консультации пайплайн не сохраняет вовсе.
        verdict = classify_scope(event, source_text)
        if verdict.is_individual:
            return Destiny(INDIVIDUAL, "", content_hash, verdict.reason)

        # Отчёты о прошедшем тоже не занимают очередь модерации.
        stale = stale_reason(event, reference_date, source_text)
        if stale is not None:
            return Destiny(STALE, "", content_hash, stale)

        first = self._seen.get(content_hash)
        if first is not None:
            return Destiny(
                DUPLICATE, "", content_hash,
                f"той самий анонс уже був у повідомленні #{first}",
            )

        self._seen[content_hash] = index

        if event.date is None:
            return Destiny(SAVED, FEED_NO_DATE, content_hash,
                           "без дати подія не потрапить у стрічку найближчих")
        return Destiny(SAVED, FEED, content_hash)


def summarize_destiny(records: list[dict]) -> dict[str, int]:
    """Сколько событий доедет до БД и до фида по всему прогону."""
    counts = {SAVED: 0, DUPLICATE: 0, FILTERED: 0, INDIVIDUAL: 0, STALE: 0,
              LANGUAGE: 0, FEED: 0}
    for record in records:
        for item in record.get("events_destiny", []):
            counts[item["db"]] = counts.get(item["db"], 0) + 1
            if item["feed"] == FEED:
                counts[FEED] += 1
    return counts
