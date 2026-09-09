"""
Пре-фильтр: дешёвый отсев нерелевантных сообщений ДО вызова LLM.

Идея: не гонять инференс на болтовне в чате. Эвристика по ключевым словам
(укр./рус.) и паттернам даты/времени. Возвращает score; решение —
по порогу. Это не точный классификатор, а грубый недорогой gate:
лучше пропустить лишнее (LLM вернёт events:[]), чем отсечь настоящий анонс.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Маркеры события (украинские и русские основы слов, регистронезависимо).
KEYWORDS: tuple[str, ...] = (
    # типы мероприятий
    "конференц", "семінар", "семинар", "лекці", "лекци", "вебінар", "вебинар",
    "воркшоп", "майстер-клас", "мастер-класс", "захід", "мероприят", "зустріч",
    "встреч", "презентац", "хакатон", "олімпіад", "олимпиад",
    # призывы / действия
    "відбудеться", "состоится", "запрошуємо", "приглашаем", "запрошуєм",
    "реєстрац", "регистрац", "дедлайн", "deadline", "явка", "розклад",
    "расписание", "перенесено", "перенос", "оголошенн", "объявлен",
    # атрибуты
    "аудитор", "ауд.", "корпус", "онлайн", "online", "zoom", "розпочн",
)

# Паттерны времени и дат — сильные индикаторы события.
TIME_RE = re.compile(r"\b([01]?\d|2[0-3])[:.]([0-5]\d)\b")
DATE_RE = re.compile(
    r"\b(\d{1,2}[./]\d{1,2}([./]\d{2,4})?)\b"  # 12.05 / 12.05.2026
    r"|\b\d{1,2}\s+(січ|лют|бер|квіт|трав|черв|лип|серп|вер|жовт|лист|груд"  # укр
    r"|янв|фев|мар|апр|ма|июн|июл|авг|сен|окт|ноя|дек)",  # рус
    re.IGNORECASE,
)
RELATIVE_RE = re.compile(
    r"\b(завтра|сьогодні|сегодня|післязавтра|послезавтра|наступн|следующ)\b",
    re.IGNORECASE,
)


@dataclass
class PrefilterResult:
    is_candidate: bool
    score: float
    matched: list[str] = field(default_factory=list)


def prefilter(text: str, threshold: float = 1.0) -> PrefilterResult:
    if not text or not text.strip():
        return PrefilterResult(False, 0.0)

    lower = text.casefold()
    matched: list[str] = []
    score = 0.0

    for kw in KEYWORDS:
        if kw in lower:
            matched.append(kw)
            score += 1.0

    if TIME_RE.search(text):
        matched.append("<time>")
        score += 1.0
    if DATE_RE.search(text):
        matched.append("<date>")
        score += 1.0
    if RELATIVE_RE.search(text):
        matched.append("<relative-date>")
        score += 0.5

    return PrefilterResult(score >= threshold, score, matched)
