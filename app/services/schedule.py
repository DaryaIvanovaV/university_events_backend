"""
Детерминированная правка даты и времени после модели.

Ручная разметка прогона показала: относительные даты по дню недели модель
считает неверно системно. «Наступної п'ятниці» от понедельника 2026-11-02 она
дала как 2026-11-08 (это воскресенье), «у понеділок» от среды 2026-12-16 — как
2026-12-17 (четверг). Усиление промта помогло для «завтра» и явных дат, но
арифметику дней недели маленькая модель не тянет.

Календарная арифметика — не то, что стоит поручать LLM: она детерминирована,
проверяема и занимает десяток строк. Модель оставляем распознавать формулировку,
а считать дату будем сами.

Здесь же нормализация времени: схема требует "HH:MM", а модель на «з 11:00 до
16:00» возвращает "11:00-16:00", и Pydantic это пропускает (валидаторов нет).
Берём начало — именно к нему приходит аудитория, — а диапазон остаётся в тексте
описания.
"""

from __future__ import annotations

import datetime as dt
import re

from app.models.schemas import ExtractedEvent
from app.services.clock import today

# Дни недели украинским: основа -> номер (понедельник = 0).
_WEEKDAYS: tuple[tuple[str, int], ...] = (
    ("понеділ", 0), ("вівтор", 1), ("серед", 2), ("четвер", 3),
    ("п'ятниц", 4), ("пʼятниц", 4), ("суббот", 5), ("субот", 5), ("неділ", 6),
)
# «наступного», «наступної», «наступному» перед днём недели = следующая неделя.
_NEXT_WEEK = re.compile(r"наступн\w*\s+(?:тижн\w*\s+)?$", re.IGNORECASE)

_TODAY = re.compile(r"\bсьогодні\b", re.IGNORECASE)
_TOMORROW = re.compile(r"\bзавтра\b", re.IGNORECASE)
_AFTER_TOMORROW = re.compile(r"\bпіслязавтра\b", re.IGNORECASE)
_YESTERDAY = re.compile(r"\bвчора\b", re.IGNORECASE)
_BEFORE_YESTERDAY = re.compile(r"\bпозавчора\b", re.IGNORECASE)
# «через 3 дні», «через тиждень», «через два тижні»
_IN_N = re.compile(
    r"через\s+(\d{1,2}|один|два|три|чотири|п'ять|шість|сім)?\s*"
    r"(день|дні|днів|тиждень|тижні|тижнів)",
    re.IGNORECASE,
)
_WORD_NUM = {
    "один": 1, "два": 2, "три": 3, "чотири": 4,
    "п'ять": 5, "шість": 6, "сім": 7,
}
# Смещения по числу дней. Порядок проверки: сначала более длинные слова,
# иначе «позавчора» поймается правилом «вчора».
_SHIFTS: tuple[tuple[re.Pattern[str], int], ...] = (
    (_BEFORE_YESTERDAY, -2),
    (_AFTER_TOMORROW, 2),
    (_YESTERDAY, -1),
    (_TOMORROW, 1),
    (_TODAY, 0),
)
_PAST_MARKERS = (_YESTERDAY, _BEFORE_YESTERDAY)
_FUTURE_MARKERS = (_TOMORROW, _AFTER_TOMORROW)

# "11:00-16:00", "11:00 – 16:00", "11.00-16.00"
_TIME_RANGE = re.compile(r"^\s*(\d{1,2})[:.](\d{2})\s*[-–—]\s*\d{1,2}[:.]\d{2}\s*$")
_TIME_OK = re.compile(r"^(\d{1,2}):(\d{2})$")

# Явная дата в тексте: число + название месяца, либо 15.10 / 15.10.2026.
# Названия перечислены целиком: основа «май» совпала бы с «майстер-клас».
# Проверять «цифры + любое слово» нельзя — «о 15:00 засідання» тогда
# читается как дата «00 засідання» и относительный пересчёт не срабатывает.
_MONTHS = (
    "січня|січень|лютого|лютий|березня|березень|квітня|квітень|травня|травень"
    "|червня|червень|липня|липень|серпня|серпень|вересня|вересень|жовтня"
    "|жовтень|листопада|листопад|грудня|грудень"
)
_EXPLICIT_DATE = re.compile(
    r"\d{1,2}\s*(?:-?го)?\s*(?:[-–—]\s*\d{1,2}\s*)?(?:" + _MONTHS + r")\b"
    r"|\b\d{1,2}[./]\d{1,2}(?:[./]\d{2,4})?\b",
    re.IGNORECASE,
)


def normalize_time(value: str | None) -> str | None:
    """
    Приводит время к "HH:MM". Диапазон схлопывается к началу.

    "11:00-16:00" → "11:00", "9:5" → None (мусор лучше пустого поля не делать
    хуже: пусть решает модератор).
    """
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None

    span = _TIME_RANGE.match(raw)
    if span:
        hour, minute = int(span.group(1)), int(span.group(2))
    else:
        exact = _TIME_OK.match(raw)
        if not exact:
            return None
        hour, minute = int(exact.group(1)), int(exact.group(2))

    if hour > 23 or minute > 59:
        return None
    return f"{hour:02d}:{minute:02d}"


def resolve_relative_date(text: str, reference_date: dt.date) -> dt.date | None:
    """
    Дата из относительного указания в тексте, либо None.

    Правила совпадают с промтом:
      позавчора → -2, вчора → -1, сьогодні → 0, завтра → +1, післязавтра → +2
      «через 3 дні», «через тиждень» → reference_date + N
      «у п'ятницю» → ближайшая пятница СТРОГО после reference_date
      «наступної п'ятниці» → та же пятница неделей позже

    Прошедшие смещения нужны не ради самих дат, а ради фильтра актуальности:
    «вчора завершилася конференція» даёт дату вчерашнего дня, и relevance.py
    отсекает такую запись до сохранения. Без этого модель ставила дату
    публикации, и отчёт о прошедшем проходил в очередь модерации.
    """
    lowered = text.casefold()

    # Есть и прошлое, и будущее («вчора завершився X, завтра починається Y») —
    # какое из них относится к событию, по тексту не решить. Не трогаем.
    has_past = any(rx.search(lowered) for rx in _PAST_MARKERS)
    has_future = any(rx.search(lowered) for rx in _FUTURE_MARKERS)
    if has_past and has_future:
        return None

    for regex, shift in _SHIFTS:
        if regex.search(lowered):
            return reference_date + dt.timedelta(days=shift)

    span = _IN_N.search(lowered)
    if span:
        raw, unit = span.group(1), span.group(2)
        count = 1 if not raw else _WORD_NUM.get(raw, int(raw) if raw.isdigit() else 1)
        days = count * (7 if unit.startswith("тижд") or unit.startswith("тижн") else 1)
        return reference_date + dt.timedelta(days=days)

    for stem, weekday in _WEEKDAYS:
        position = lowered.find(stem)
        if position < 0:
            continue
        ahead = (weekday - reference_date.weekday()) % 7 or 7
        target = reference_date + dt.timedelta(days=ahead)
        # «наступної п'ятниці» — та же пятница, но следующей недели.
        if _NEXT_WEEK.search(lowered[:position]):
            target += dt.timedelta(days=7)
        return target
    return None


def normalize_schedule(
    event: ExtractedEvent, source_text: str, reference_date: dt.date | None = None
) -> ExtractedEvent:
    """
    Чинит время и относительную дату. Явные даты из текста не трогает.

    Дата пересчитывается ТОЛЬКО когда в тексте нет явной даты («15 жовтня»),
    но есть относительное указание: там модель ошибается, а мы считаем точно.
    """
    reference_date = reference_date or today()
    changes: dict = {}

    fixed_time = normalize_time(event.time)
    if fixed_time != event.time:
        changes["time"] = fixed_time

    # Явная дата в тексте — доверяем модели, с ними она справляется.
    if not _EXPLICIT_DATE.search(source_text):
        resolved = resolve_relative_date(source_text, reference_date)
        if resolved is not None and resolved != event.date:
            changes["date"] = resolved

    return event.model_copy(update=changes) if changes else event
