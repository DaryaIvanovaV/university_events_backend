"""
Единый источник «сейчас» для всего приложения.

Зачем отдельный модуль, а не `dt.date.today()` по коду: `today()` берёт
локальное время процесса. На машине разработчика это Киев, а в Docker-
контейнере — UTC. Разница до трёх часов ломает ровно одно место, зато
видимое пользователю: фильтр `upcoming` в фиде. С полуночи до 03:00 по Киеву
«сегодня» в UTC — это ещё вчера, и приложение показывало бы студентам
мероприятие, которое уже прошло.

Часовой пояс берётся из настройки `APP_TIMEZONE` (по умолчанию Europe/Kyiv).
Если база IANA недоступна (на Windows её нет в системе — нужен пакет
`tzdata`), падать нельзя: логируем предупреждение один раз и работаем по
локальному времени процесса, как раньше.
"""

from __future__ import annotations

import datetime as dt
import logging
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.config import settings

logger = logging.getLogger(__name__)


@lru_cache(maxsize=None)
def tz() -> dt.tzinfo | None:
    """Часовой пояс приложения; None — если зона недоступна (работаем локально)."""
    name = settings.app_timezone.strip()
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        logger.warning(
            "Часовой пояс %r недоступен (%s: %s) — использую локальное время "
            "процесса. Поставьте пакет tzdata, если это Windows или slim-образ.",
            name, type(exc).__name__, exc,
        )
        return None


def now() -> dt.datetime:
    """Текущий момент в зоне приложения (aware, если зона доступна)."""
    return dt.datetime.now(tz())


def today() -> dt.date:
    """Сегодняшняя дата в зоне приложения — замена dt.date.today()."""
    return now().date()


def utc_now() -> dt.datetime:
    """
    UTC-момент для служебных отметок: очередь задач, блокировки, тайминги.

    Намеренно НЕ зона приложения. Эти значения человек не читает, зато их
    сравнивает БД, а на SQLite `func.now()` — это CURRENT_TIMESTAMP в UTC.
    Смешивать его с киевским временем означало бы расхождение в 2–3 часа
    в условиях выборки задач. На PostgreSQL (timestamptz) разницы нет, но
    единое правило проще, чем два поведения по диалектам.
    """
    return dt.datetime.now(dt.UTC)
