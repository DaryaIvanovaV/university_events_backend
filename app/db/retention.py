"""
Автоочищення минулих подій.

Календар ЗВО цінний лише майбутнім: подія, що вже відбулася, у фіді не
показується ніколи (`upcoming` фільтрує `date >= сьогодні`), але далі займає
місце в таблиці, у черзі модерації та — найгірше — у локальному кеші
мобільного застосунку.

Прибирання двокрокове, і це не надмірність:

  1. **`expired`** — через `event_expire_after_days` добу після СТАРТУ події
     статус змінюється на `expired`. Запис лишається в БД, тому
     дедуплікація за `content_hash` працює далі (той самий минулий анонс не
     заїде повторно), а `GET /events/sync` віддає id у списку `removed` —
     застосунок прибирає картку з кеша Room. Фізичне видалення тут було б
     гіршим: рядка немає → у `removed` його нічим згенерувати → подія
     лишилася б у кеші телефона назавжди.
  2. **Фізичне видалення** — через `event_purge_after_days` днів після дати
     події рядок зі статусом `expired` видаляється остаточно, щоб таблиця не
     росла вічно. Пристрій, який не синхронізувався довше цього строку,
     сигналу `removed` уже не побачить, тому строк беремо з великим запасом
     (дефолт — 90 днів).

Що НЕ прибирається:
  * **події без дати** — `date IS NULL` не з чим порівнювати, а «минуле» тут
    неполагоджуване: дату має проставити модератор через PATCH;
  * **`rejected`** — відхилені записи живуть заради дедуплікації, і сигнал
    `removed` клієнт за ними вже отримав;
  * **`expired` без дати** — узагалі неможливий стан, але фільтр явний.

«Зараз» береться з `app.services.clock` (зона `APP_TIMEZONE`), а не з часу
процесу: у контейнері він UTC, і з півночі до 03:00 за Києвом подія
«вчора о 18:00» ще не вважалася б минулою добу тому.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models.db import Event
from app.services.clock import now as clock_now

logger = logging.getLogger(__name__)

# Статуси, які підлягають протермінуванню. `pending` теж: минула подія в
# черзі модерації — сміття, підтверджувати її вже нема сенсу.
EXPIRABLE_STATUSES = ("approved", "pending")


@dataclass
class CleanupResult:
    """Підсумок одного проходу прибирання (їде в лог і у відповідь API)."""

    expired_ids: list[int] = field(default_factory=list)
    purged: int = 0

    @property
    def touched(self) -> bool:
        return bool(self.expired_ids) or bool(self.purged)


def event_start(event: Event) -> dt.datetime | None:
    """
    Момент початку події: дата + час, якщо він є.

    Час у колонці — рядок `HH:MM` (схема валідаторів не має, тому
    непридатне значення цілком можливе). Непарсабельний час трактуємо як
    початок дня: подія однаково протермінується, просто на кілька годин
    раніше, ніж якби час був відомий.
    """
    if event.date is None:
        return None
    start_time = dt.time(0, 0)
    if event.time:
        try:
            hours, _, minutes = event.time.partition(":")
            start_time = dt.time(int(hours), int(minutes or 0))
        except ValueError:
            logger.warning(
                "Подія %s: час %r не розбирається — вважаю початком дня",
                event.id, event.time,
            )
    return dt.datetime.combine(event.date, start_time)


def expire_due_events(
    session: Session,
    *,
    after_days: int | None = None,
    moment: dt.datetime | None = None,
) -> list[int]:
    """
    Проставляє `expired` подіям, старт яких був раніше ніж `after_days` тому.

    `after_days <= 0` вимикає крок (тоді не прибирається нічого, бо фізичне
    видалення працює тільки по `expired`).
    """
    days = settings.event_expire_after_days if after_days is None else after_days
    if days <= 0:
        return []

    reference = (moment or clock_now()).replace(tzinfo=None)
    # Звужуємо вибірку по індексованій колонці date: подія, дата якої
    # пізніше за цю межу, не могла протермінуватися навіть за часу 00:00.
    # Точну перевірку (дата + час) робить Python нижче.
    latest_date = (reference - dt.timedelta(days=days)).date()
    candidates = session.scalars(
        select(Event).where(
            Event.date.is_not(None),
            Event.date <= latest_date,
            Event.status.in_(EXPIRABLE_STATUSES),
        )
    )

    expired: list[int] = []
    for event in candidates:
        start = event_start(event)
        if start is None or start + dt.timedelta(days=days) > reference:
            continue
        event.status = "expired"  # onupdate зсуне updated_at → піде в sync
        expired.append(event.id)
    if expired:
        session.commit()
    return expired


def purge_expired_events(
    session: Session,
    *,
    after_days: int | None = None,
    moment: dt.datetime | None = None,
) -> int:
    """
    Видаляє рядки `expired`, дата яких старша за `after_days` днів.

    `after_days <= 0` вимикає крок: записи лишаються в БД назавжди (так само
    поводиться система до цієї функції).
    """
    days = settings.event_purge_after_days if after_days is None else after_days
    if days <= 0:
        return 0

    reference = (moment or clock_now()).replace(tzinfo=None)
    cutoff = (reference - dt.timedelta(days=days)).date()
    result = session.execute(
        delete(Event).where(
            Event.status == "expired",
            Event.date.is_not(None),
            Event.date < cutoff,
        )
    )
    deleted = int(result.rowcount or 0)
    if deleted:
        session.commit()
    else:
        session.rollback()
    return deleted


def cleanup(
    session: Session,
    *,
    expire_after_days: int | None = None,
    purge_after_days: int | None = None,
    moment: dt.datetime | None = None,
) -> CleanupResult:
    """Повний прохід: спершу протермінувати, потім видалити давнє."""
    reference = moment or clock_now()
    return CleanupResult(
        expired_ids=expire_due_events(
            session, after_days=expire_after_days, moment=reference
        ),
        purged=purge_expired_events(
            session, after_days=purge_after_days, moment=reference
        ),
    )
