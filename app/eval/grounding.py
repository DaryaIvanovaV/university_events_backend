"""
Проверка «заземления» (grounding) извлечённых полей на исходном тексте.

Задача: автоматически показать, какие значения модель ВЗЯЛА из сообщения, а
какие ВЫДУМАЛА. Это подсказка для ручного просмотра, а не приговор: финальное
решение принимает человек, поэтому каждая проверка возвращает не только статус,
но и пояснение (note) и позиции подтверждающих фрагментов в тексте (spans) —
отчёт подсвечивает их прямо в оригинале.

Почему не наивный substring: модель обязана перефразировать ("405 аудиторії" →
"Ауд. 405", "гостьова лекція ... про розробку" → "Гостьова лекція: розробка"),
и точное вхождение давало бы сплошные ложные тревоги. Поэтому:
  * слова сверяются по префиксу (устойчиво к укр/рус морфологии:
    "лекція"/"лекції", "конференцію"/"конференція");
  * ЦИФРЫ сверяются строго — выдуманный номер аудитории или год это самый
    сильный сигнал галлюцинации, и здесь поблажек быть не должно;
  * дата и время разбираются отдельными парсерами, потому что относительные
    даты ("завтра") в тексте не встречаются буквально по определению.

Модуль чистый: без сети, без БД, без Ollama — полностью покрывается тестами.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from app.models.schemas import ExtractedEvent

# --- Статусы проверки ---

OK = "ok"  # подтверждено текстом
SUSPECT = "suspect"  # подтверждено частично, нужен взгляд человека
INVENTED = "invented"  # в тексте нет опоры — вероятная выдумка
INVALID = "invalid"  # значение нарушает собственный формат (25:99, пустая строка)
EMPTY = "empty"  # null — модель честно ничего не нашла
MISSED = "missed"  # в тексте данные ЕСТЬ, а поле пустое (пропуск, не выдумка)
INFO = "info"  # нейтральное наблюдение

# Порядок отображения полей в отчёте.
FIELD_ORDER = (
    "event_title",
    "date",
    "time",
    "location",
    "organizer",
    "target_audience",
    "event_type",
    "description",
    "language",
    "link",
    "meeting_code",
)

# Поля со свободным текстом — проверяются токенами.
TEXT_FIELDS = (
    "event_title",
    "location",
    "organizer",
    "target_audience",
    "description",
)

# Доля подтверждённых токенов, ниже которой значение считается подозрительным.
SUSPECT_THRESHOLD = 0.7
INVENTED_THRESHOLD = 0.35

# Длина префикса при сравнении слов (гасит падежные окончания).
# Ровно 4: на 5 символах "група"/"групи" и "гурток"/"гуртка" уже расходятся
# (обрезание попадает на само окончание) и давали ложные тревоги, а на 3
# слишком многое начинает совпадать случайно.
_PREFIX_LEN = 4
# Длина, начиная с которой у слова отбрасывается последняя буква: в укр./рус.
# это почти всегда флексия ("зала"/"залі", "група"/"групи").
_STEM_FROM = 4
# Токены короче — служебный шум, в расчёт не идут.
_MIN_TOKEN_LEN = 3

# Служебные слова, которые не могут служить подтверждением.
_STOPWORDS = frozenset(
    """
    про для від при над під або так там тут вже вас нас них цей ця цього тому
    того буде було бути щоб який яка яке які всі все весь буде наш ваш його її
    как что это эта этот того тому уже еще все всех весь наш ваш его ее или
    при над под для от до по за из без быть был была было были есть очень
    the and for with from that this you your are was were will have has
    """.split()
)

_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
_DIGITS_RE = re.compile(r"\d+")
_URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>\"'()\[\],]+", re.IGNORECASE)

# Числительные СЛОВАМИ. Модель штатно нормализует "третього корпусу" → "корпус 3"
# и "другого та третього курсів" → "2 та 3 курси". Без этой таблицы строгая
# проверка цифр объявляла бы такую нормализацию выдумкой — на реальном прогоне
# это дало 4 ложные тревоги из 6.
# None — слова-обманки, которые начинаются как числительное, но числом не
# являются ("п'ятниця", "тривалість"). Они длиннее самих числительных, а
# альтернатива отсортирована по длине, поэтому перехватываются первыми.
_NUMERAL_WORDS: dict[str, int | None] = {
    "одинадцят": 11, "дванадцят": 12, "тринадцят": 13, "чотирнадцят": 14,
    "п'ятнадцят": 15, "шістнадцят": 16, "сімнадцят": 17, "вісімнадцят": 18,
    "дев'ятнадцят": 19, "двадцят": 20, "тридцят": 30, "сорок": 40,
    "п'ятдесят": 50, "шістдесят": 60, "сімдесят": 70, "вісімдесят": 80,
    "дев'яност": 90,
    # обманки
    "п'ятниц": None, "суботн": None, "сімейн": None, "однак": None,
    "тривал": None, "трива": None, "двері": None, "дружн": None,
    # единицы: порядковые и количественные
    "перш": 1, "один": 1, "одна": 1, "одного": 1,
    "друг": 2, "два": 2, "дві": 2, "двох": 2,
    "трет": 3, "три": 3, "трьох": 3,
    "четверт": 4, "чотир": 4,
    "п'ят": 5, "шост": 6, "шест": 6, "шість": 6,
    "сьом": 7, "сім": 7, "семи": 7,
    "восьм": 8, "вісім": 8, "восьми": 8,
    "дев'ят": 9, "десят": 10,
}
_NUMERAL_RE = re.compile(
    r"\b(" + "|".join(sorted(_NUMERAL_WORDS, key=len, reverse=True)) + r")\w*",
    re.IGNORECASE,
)


def _normalize(text: str) -> str:
    """Приводит к нижнему регистру и гасит вариативные символы."""
    text = text.casefold()
    for src, dst in (("ё", "е"), ("’", "'"), ("ʼ", "'"), ("`", "'")):
        text = text.replace(src, dst)
    return text


def _tokens_with_spans(text: str) -> list[tuple[str, int, int]]:
    """Список (нормализованный токен, начало, конец) по исходному тексту."""
    norm = _normalize(text)
    return [(m.group(0), m.start(), m.end()) for m in _TOKEN_RE.finditer(norm)]


def _content_tokens(text: str) -> list[str]:
    """Значимые токены значения: без коротких, без стоп-слов, без чистых цифр."""
    out = []
    for tok, _, _ in _tokens_with_spans(text):
        if tok.isdigit():
            continue  # цифры проверяются отдельно и строго
        if len(tok) < _MIN_TOKEN_LEN or tok in _STOPWORDS:
            continue
        out.append(tok)
    return out


def _stem(token: str) -> str:
    """Отбрасывает последнюю букву — грубая замена лемматизации."""
    return token[:-1] if len(token) >= _STEM_FROM else token


def _source_numbers(source: str) -> set[str]:
    """
    Все числа текста — и записанные цифрами, и словами.

    "в аудиторії 405 третього корпусу" → {"405", "3"}: перевод числительного
    в цифру это нормализация, а не выдумка.
    """
    numbers = set(_DIGITS_RE.findall(source))
    for m in _NUMERAL_RE.finditer(_normalize(source)):
        value = _NUMERAL_WORDS.get(m.group(1))
        if value is not None:
            numbers.add(str(value))
    return numbers


def _tokens_match(a: str, b: str) -> bool:
    """
    Совпадение слов по общей основе.

    Сначала отбрасывается флексия, потом сравнивается префикс не длиннее 4.
    Так "ауд" находит "аудиторії", "зала" — "залі", "група" — "групи",
    "гурток" — "гуртка", но "конференція" НЕ находит "консультація", а укр.
    "відкриття" не находит рус. "откроется" (перевод промтом запрещён, и
    подменять его подтверждением нельзя).
    """
    a, b = _stem(a), _stem(b)
    n = min(len(a), len(b), _PREFIX_LEN)
    if n < _MIN_TOKEN_LEN:
        return False
    return a[:n] == b[:n]


@dataclass
class FieldCheck:
    """Результат проверки одного поля одного события."""

    field: str
    value: object
    status: str
    note: str = ""
    coverage: float | None = None
    spans: list[tuple[int, int]] = field(default_factory=list)

    @property
    def is_problem(self) -> bool:
        return self.status in (SUSPECT, INVENTED, INVALID, MISSED)

    def to_dict(self) -> dict:
        return {
            "field": self.field,
            "value": self.value,
            "status": self.status,
            "note": self.note,
            "coverage": self.coverage,
            "spans": [list(s) for s in self.spans],
        }


# --- Текстовые поля ---


def check_text_field(field_name: str, value: str | None, source: str) -> FieldCheck:
    """Сверяет свободный текст со словами и цифрами исходного сообщения."""
    if value is None:
        return FieldCheck(field_name, value, EMPTY, "модель вернула null")
    if not str(value).strip():
        status = INVALID if field_name == "event_title" else EMPTY
        return FieldCheck(field_name, value, status, "пустая строка")

    value = str(value)
    src_tokens = _tokens_with_spans(source)
    src_digits = _source_numbers(source)

    # 1. Цифры — строго. Выдуманный номер аудитории/год ловится здесь.
    # Числительные словами засчитываются: "третього корпусу" подтверждает "3".
    val_digits = _DIGITS_RE.findall(_normalize(value))
    bad_digits = [d for d in val_digits if d not in src_digits]
    if bad_digits:
        return FieldCheck(
            field_name,
            value,
            INVENTED,
            f"цифр нет в тексте: {', '.join(sorted(set(bad_digits)))}",
            coverage=0.0,
        )

    # 2. Слова — по префиксу, с накоплением подсвечиваемых позиций.
    spans: list[tuple[int, int]] = []
    for digit in set(val_digits):
        for tok, start, end in src_tokens:
            if tok == digit:
                spans.append((start, end))

    content = _content_tokens(value)
    if not content:
        # Значение из одних цифр/стоп-слов: цифры уже проверены выше.
        note = "только цифры/служебные слова — сверено по цифрам" if val_digits else (
            "нет значимых слов для сверки"
        )
        return FieldCheck(
            field_name, value, OK if val_digits else SUSPECT, note,
            coverage=1.0 if val_digits else None, spans=_merge_spans(spans),
        )

    grounded = 0
    for vtok in content:
        hit = False
        for stok, start, end in src_tokens:
            if _tokens_match(vtok, stok):
                spans.append((start, end))
                hit = True
        if hit:
            grounded += 1

    coverage = grounded / len(content)
    missing = [t for t in content if not any(_tokens_match(t, s) for s, _, _ in src_tokens)]

    if coverage >= SUSPECT_THRESHOLD:
        status, note = OK, "подтверждено текстом"
    elif coverage >= INVENTED_THRESHOLD:
        status = SUSPECT
        note = f"часть слов не из текста: {', '.join(missing[:5])}"
    else:
        status = INVENTED
        note = f"почти нет опоры в тексте (нет: {', '.join(missing[:5])})"

    return FieldCheck(
        field_name, value, status, note, coverage=coverage, spans=_merge_spans(spans)
    )


def _merge_spans(spans: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    """Схлопывает пересекающиеся/смежные отрезки — для чистой подсветки."""
    if not spans:
        return []
    ordered = sorted(set(spans))
    merged = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


# --- Время ---

# Время в тексте: "14:30", "14.30", "о 14 год", "в 9 часов".
_SRC_TIME_RE = re.compile(r"(\d{1,2})\s*[:.]\s*(\d{2})")
_SRC_HOUR_RE = re.compile(
    r"(?:\bо\b|\bоб\b|\bв\b|\bу\b|\bat\b)\s*(\d{1,2})(?!\s*[:.]\s*\d)"
    # "з 8 ранку", "до 17 години", "18 вечора" — час без двоеточия
    r"|(\d{1,2})\s*(?:год|час|ранку|ранок|вечора|вечір|дня|ночі)",
    re.IGNORECASE,
)
_VALUE_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


def _source_times(source: str) -> tuple[set[tuple[int, int]], set[int], list[tuple[int, int]]]:
    """Время, найденное в тексте: точные (ч, мин), одинокие часы, их позиции."""
    norm = _normalize(source)
    exact: set[tuple[int, int]] = set()
    spans: list[tuple[int, int]] = []
    for m in _SRC_TIME_RE.finditer(norm):
        h, mi = int(m.group(1)), int(m.group(2))
        if h <= 23 and mi <= 59:
            exact.add((h, mi))
            spans.append((m.start(), m.end()))
    hours: set[int] = set()
    for m in _SRC_HOUR_RE.finditer(norm):
        raw = m.group(1) or m.group(2)
        if raw is None:
            continue
        h = int(raw)
        if h <= 23:
            hours.add(h)
            spans.append((m.start(), m.end()))
    return exact, hours, spans


def check_time(value: str | None, source: str) -> FieldCheck:
    """Проверяет формат HH:MM и наличие этого времени в тексте."""
    if value is None:
        return FieldCheck("time", value, EMPTY, "модель вернула null")

    m = _VALUE_TIME_RE.match(str(value).strip())
    if not m:
        return FieldCheck("time", value, INVALID, "не формат HH:MM")
    hour, minute = int(m.group(1)), int(m.group(2))
    if hour > 23 or minute > 59:
        return FieldCheck("time", value, INVALID, "время вне диапазона 00:00-23:59")

    exact, hours, spans = _source_times(source)
    if (hour, minute) in exact:
        return FieldCheck("time", value, OK, "время есть в тексте", 1.0, _merge_spans(spans))
    if minute == 0 and hour in hours:
        return FieldCheck(
            "time", value, SUSPECT,
            "в тексте указан только час — минуты :00 дописаны моделью",
            0.5, _merge_spans(spans),
        )
    if hour in hours or any(h == hour for h, _ in exact):
        return FieldCheck(
            "time", value, SUSPECT, "час совпадает, минуты в тексте другие",
            0.5, _merge_spans(spans),
        )
    found = sorted(f"{h:02d}:{mi:02d}" for h, mi in exact) + [f"{h}:??" for h in sorted(hours)]
    note = f"в тексте: {', '.join(found)}" if found else "в тексте времени нет вообще"
    return FieldCheck("time", value, INVENTED, note, 0.0, _merge_spans(spans))


# --- Дата ---

# Названия месяцев: укр. и рус., родительный (с числом) и именительный.
# Перечислены ЦЕЛИКОМ, а не основами: основа "май" совпала бы с "майстер-клас",
# а "март" — с "марафон". Дальше alternation сортируется по длине, чтобы
# "листопада" проверялось раньше "листопад".
_MONTH_WORDS: dict[int, tuple[str, ...]] = {
    1: ("січня", "січень", "января", "январь"),
    2: ("лютого", "лютий", "февраля", "февраль"),
    3: ("березня", "березень", "марта", "март"),
    4: ("квітня", "квітень", "апреля", "апрель"),
    5: ("травня", "травень", "мая", "мае", "май"),
    6: ("червня", "червень", "июня", "июнь"),
    7: ("липня", "липень", "июля", "июль"),
    8: ("серпня", "серпень", "августа", "август"),
    9: ("вересня", "вересень", "сентября", "сентябрь"),
    10: ("жовтня", "жовтень", "октября", "октябрь"),
    11: ("листопада", "листопад", "ноября", "ноябрь"),
    12: ("грудня", "грудень", "декабря", "декабрь"),
}
_MONTH_BY_WORD = {
    word: num for num, words in _MONTH_WORDS.items() for word in words
}
_MONTH_ALTERNATION = "|".join(sorted(_MONTH_BY_WORD, key=len, reverse=True))

# "15 травня", "15-16 травня", "з 15 по 16 травня", "20-го мая".
# Название месяца засчитывается ТОЛЬКО рядом с числом: иначе "у травні"
# без дня дал бы паре (день, месяц) взяться из воздуха.
_DAY_MONTH_RE = re.compile(
    r"(\d{1,2})\s*(?:-?го)?\s*(?:[-–—]|по|до|та|и|і)?\s*(\d{1,2})?\s*(?:-?го)?\s*"
    r"(" + _MONTH_ALTERNATION + r")\b",
    re.IGNORECASE,
)
# "15.05.2026", "15/05", "15-05-2026"
_NUMERIC_DATE_RE = re.compile(r"\b(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?\b")
_YEAR_RE = re.compile(r"\b(20\d{2})\b")

_RELATIVE_MARKERS: dict[str, tuple[int, int]] = {
    # маркер -> допустимое смещение от reference_date (включительно)
    "сьогодні": (0, 0),
    "сегодня": (0, 0),
    "завтра": (1, 1),
    "післязавтра": (2, 2),
    "послезавтра": (2, 2),
    "наступного тижня": (1, 14),
    "наступному тижні": (1, 14),
    "на следующей неделе": (1, 14),
    "следующей неделе": (1, 14),
    "цього тижня": (0, 7),
    "на этой неделе": (0, 7),
    "на цьому тижні": (0, 7),
}
_WEEKDAYS: dict[str, int] = {
    "понеділок": 0, "понедельник": 0, "понеділка": 0,
    "вівторок": 1, "вторник": 1, "вівторка": 1,
    "серед": 2, "среду": 2, "среда": 2,
    "четвер": 3, "четверг": 3,
    "п'ятниц": 4, "пятниц": 4,
    "субот": 5, "суббот": 5,
    "неділ": 6, "воскресен": 6,
}


def _source_dates(source: str) -> tuple[
    set[tuple[int, int]], set[int], list[tuple[int, int]], list[str]
]:
    """
    Извлекает из текста явные (день, месяц), годы, позиции и «сырые» подписи.
    """
    norm = _normalize(source)
    pairs: set[tuple[int, int]] = set()
    spans: list[tuple[int, int]] = []
    labels: list[str] = []

    for m in _DAY_MONTH_RE.finditer(norm):
        month = _MONTH_BY_WORD.get(m.group(3))
        if month is None:
            continue
        for raw_day in (m.group(1), m.group(2)):
            if raw_day is None:
                continue
            day = int(raw_day)
            if 1 <= day <= 31:
                pairs.add((day, month))
        spans.append((m.start(), m.end()))
        labels.append(m.group(0).strip())

    for m in _NUMERIC_DATE_RE.finditer(norm):
        day, month = int(m.group(1)), int(m.group(2))
        if 1 <= day <= 31 and 1 <= month <= 12:
            pairs.add((day, month))
            spans.append((m.start(), m.end()))
            labels.append(m.group(0).strip())

    years = {int(y) for y in _YEAR_RE.findall(norm)}
    return pairs, years, spans, labels


def _relative_window(
    source: str, reference_date: dt.date
) -> tuple[dt.date, dt.date, str] | None:
    """Допустимое окно дат по относительным маркерам («завтра», «у п'ятницю»)."""
    norm = _normalize(source)
    for marker, (lo, hi) in _RELATIVE_MARKERS.items():
        if marker in norm:
            return (
                reference_date + dt.timedelta(days=lo),
                reference_date + dt.timedelta(days=hi),
                marker,
            )
    for name, weekday in _WEEKDAYS.items():
        if name in norm:
            # Ближайшее наступление этого дня недели, начиная со следующего дня.
            ahead = (weekday - reference_date.weekday()) % 7
            first = reference_date + dt.timedelta(days=ahead or 7)
            # «У п'ятницю» может значить и эту, и следующую — окно на неделю.
            return first, first + dt.timedelta(days=7), name
    return None


def check_date(
    value: dt.date | None, source: str, reference_date: dt.date
) -> FieldCheck:
    """Сверяет дату с явными датами текста либо с относительными маркерами."""
    if value is None:
        return FieldCheck("date", value, EMPTY, "модель вернула null")

    pairs, years, spans, labels = _source_dates(source)
    rel = _relative_window(source, reference_date)
    iso = value.isoformat()
    merged = _merge_spans(spans)

    # Санитарная проверка диапазона — ловит съехавший год.
    delta = (value - reference_date).days
    sanity = ""
    if delta > 400:
        sanity = f" (на {delta} дн. позже даты-ориентира — проверьте год)"
    elif delta < -30:
        sanity = f" (на {-delta} дн. РАНЬШЕ даты-ориентира)"

    if pairs:
        if (value.day, value.month) in pairs:
            status = SUSPECT if sanity else OK
            note = f"день и месяц есть в тексте: {', '.join(labels[:3])}{sanity}"
            if years and value.year not in years:
                status = SUSPECT
                note += f"; год {value.year} в тексте не указан (в тексте: {sorted(years)})"
            return FieldCheck("date", iso, status, note, 1.0, merged)
        if rel is None:
            found = ", ".join(labels[:3])
            return FieldCheck(
                "date", iso, INVENTED,
                f"в тексте другие даты: {found}{sanity}", 0.0, merged,
            )

    if rel is not None:
        lo, hi, marker = rel
        if lo <= value <= hi:
            return FieldCheck(
                "date", iso, SUSPECT if sanity else OK,
                f"относительная дата «{marker}» от {reference_date.isoformat()}{sanity}",
                1.0, merged,
            )
        expected = lo.isoformat() if lo == hi else f"{lo.isoformat()}…{hi.isoformat()}"
        return FieldCheck(
            "date", iso, INVENTED,
            f"«{marker}» от {reference_date.isoformat()} даёт {expected}{sanity}",
            0.0, merged,
        )

    return FieldCheck(
        "date", iso, INVENTED,
        f"в тексте нет ни явной даты, ни относительного указания{sanity}",
        0.0, merged,
    )


# --- Ссылка ---


def _normalize_url(url: str) -> str:
    """Схема/www/хвостовая пунктуация не должны мешать сравнению."""
    url = _normalize(url).strip()
    url = re.sub(r"^https?://", "", url)
    url = re.sub(r"^www\.", "", url)
    return url.rstrip("/.,;:!?)»\"'")


def check_link(value: str | None, source: str) -> FieldCheck:
    """Ссылка обязана присутствовать в тексте дословно — здесь скидок нет."""
    src_urls = [m.group(0) for m in _URL_RE.finditer(source)]
    if value is None:
        if src_urls:
            spans = [(m.start(), m.end()) for m in _URL_RE.finditer(source)]
            return FieldCheck(
                "link", value, MISSED,
                f"в тексте есть ссылка, а поле пустое: {src_urls[0]}",
                0.0, _merge_spans(spans),
            )
        return FieldCheck("link", value, EMPTY, "модель вернула null")

    norm_value = _normalize_url(str(value))
    if not norm_value:
        return FieldCheck("link", value, INVALID, "пустая ссылка")

    norm_source = _normalize(source)
    if norm_value in norm_source:
        idx = norm_source.find(norm_value)
        return FieldCheck(
            "link", value, OK, "ссылка есть в тексте дословно", 1.0,
            [(idx, idx + len(norm_value))],
        )

    for src_url in src_urls:
        norm_src = _normalize_url(src_url)
        if norm_value.startswith(norm_src) or norm_src.startswith(norm_value):
            idx = _normalize(source).find(_normalize(src_url))
            return FieldCheck(
                "link", value, SUSPECT,
                f"похоже на ссылку из текста, но не совпадает: {src_url}",
                0.5, [(idx, idx + len(src_url))] if idx >= 0 else [],
            )

    note = (
        f"ссылки нет в тексте (в тексте: {', '.join(src_urls[:2])})"
        if src_urls
        else "в тексте нет ни одной ссылки — URL выдуман"
    )
    return FieldCheck("link", value, INVENTED, note, 0.0)


# --- Язык ---

_UK_LETTERS = set("іїєґ")
_RU_LETTERS = set("ыэъё")


def detect_script(source: str) -> str | None:
    """Грубое определение языка по характерным буквам: 'uk', 'ru' или None."""
    text = source.casefold()
    uk = sum(1 for ch in text if ch in _UK_LETTERS)
    ru = sum(1 for ch in text if ch in _RU_LETTERS)
    if uk == 0 and ru == 0:
        return None
    return "uk" if uk > ru else "ru"


def check_language(value: str | None, source: str) -> FieldCheck:
    """Сверяет заявленный язык с буквенными признаками текста."""
    if value is None:
        return FieldCheck("language", value, EMPTY, "модель вернула null")
    guess = detect_script(source)
    value_norm = str(value).strip().lower()
    if guess is None:
        return FieldCheck("language", value, INFO, "нет характерных букв для проверки")
    if value_norm == guess:
        return FieldCheck("language", value, OK, f"признаки текста: {guess}", 1.0)
    return FieldCheck(
        "language", value, SUSPECT,
        f"текст похож на «{guess}», модель сказала «{value_norm}»", 0.0,
    )


# --- Сборка по событию и по сообщению ---


def check_meeting_code(value: str | None, source: str) -> FieldCheck:
    """
    Код встречи проверяется ТОЛЬКО по цифрам.

    Слова-обёртки ("ID", "код") добавляет нормализация, их в тексте может не
    быть. А вот сами цифры идентификатора и пароля обязаны присутствовать
    дословно: выдуманный код доступа не пустит студента на встречу.
    """
    if value is None:
        return FieldCheck("meeting_code", value, EMPTY, "модель вернула null")

    digits = _DIGITS_RE.findall(str(value))
    if not digits:
        return FieldCheck("meeting_code", value, SUSPECT, "в коде нет ни одной цифры")

    src_groups = _DIGITS_RE.findall(source)
    # Пробелы внутри ID расставляют по-разному: "845 2371 9004" в тексте против
    # "84523719004" в ссылке. Поэтому сверяем и по группам, и по слитной записи.
    src_joined = "".join(src_groups)
    bad = [d for d in digits if d not in src_groups and d not in src_joined]
    if bad:
        return FieldCheck(
            "meeting_code", value, INVENTED,
            f"цифр нет в тексте: {', '.join(sorted(set(bad)))}", 0.0,
        )

    spans = []
    for tok, start, end in _tokens_with_spans(source):
        if tok in digits:
            spans.append((start, end))
    return FieldCheck(
        "meeting_code", value, OK, "цифры кода есть в тексте", 1.0, _merge_spans(spans)
    )


def check_event(
    event: ExtractedEvent, source: str, reference_date: dt.date
) -> list[FieldCheck]:
    """Полный набор проверок одного извлечённого события."""
    checks: dict[str, FieldCheck] = {}
    for name in TEXT_FIELDS:
        checks[name] = check_text_field(name, getattr(event, name), source)
    checks["date"] = check_date(event.date, source, reference_date)
    checks["time"] = check_time(event.time, source)
    checks["link"] = check_link(event.link, source)
    checks["meeting_code"] = check_meeting_code(event.meeting_code, source)
    checks["language"] = check_language(event.language, source)
    # event_type ограничен схемой (constrained decoding) — выдумать значение вне
    # перечисления модель не может, поэтому просто показываем его.
    checks["event_type"] = FieldCheck(
        "event_type", event.event_type.value, INFO, "выбор из фиксированного списка"
    )
    return [checks[name] for name in FIELD_ORDER if name in checks]


def check_omissions(
    events: Sequence[ExtractedEvent], source: str, *, prefilter_candidate: bool | None = None
) -> list[FieldCheck]:
    """
    Обратная сторона выдумок — пропуски на уровне сообщения.

    Проверяет то, что видно только по сообщению целиком: есть ли в тексте
    данные, которых нет ни в одном из извлечённых событий.
    """
    out: list[FieldCheck] = []

    if not events:
        if prefilter_candidate:
            out.append(
                FieldCheck(
                    "events", [], MISSED,
                    "пре-фильтр счёл сообщение анонсом, а модель не нашла события",
                )
            )
        else:
            out.append(FieldCheck("events", [], OK, "события не найдены (и не ожидались)"))
        return out

    if prefilter_candidate is False:
        out.append(
            FieldCheck(
                "events", len(events), INFO,
                "пре-фильтр отсёк бы это сообщение — в проде LLM его не увидела бы",
            )
        )

    src_urls = [m.group(0) for m in _URL_RE.finditer(source)]
    if src_urls and all(ev.link is None for ev in events):
        out.append(
            FieldCheck("link", None, MISSED, f"ссылка в тексте не извлечена: {src_urls[0]}")
        )

    exact, hours, _ = _source_times(source)
    if (exact or hours) and all(ev.time is None for ev in events):
        shown = sorted(f"{h:02d}:{m:02d}" for h, m in exact) or [f"{h}:00" for h in sorted(hours)]
        out.append(
            FieldCheck("time", None, MISSED, f"время в тексте не извлечено: {', '.join(shown)}")
        )

    return out


def summarize(checks: Sequence[FieldCheck]) -> dict[str, int]:
    """Счётчик статусов — для сводки в отчёте."""
    counts: dict[str, int] = {}
    for check in checks:
        counts[check.status] = counts.get(check.status, 0) + 1
    return counts


def worst_status(checks: Sequence[FieldCheck]) -> str:
    """Худший статус набора — по нему сортируется отчёт."""
    order = [INVENTED, INVALID, MISSED, SUSPECT, OK, INFO, EMPTY]
    present = {c.status for c in checks}
    for status in order:
        if status in present:
            return status
    return OK
