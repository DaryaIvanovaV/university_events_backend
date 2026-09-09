"""
Определение языка сообщения: обрабатываем только украинские объявления.

Факультет ведёт канал украинским, русскоязычные сообщения в календарь не идут.
Отсекаем ДО вызова LLM: инференс на них тратить незачем, а модель к тому же
норовит перевести текст на украинский вопреки запрету в промте (проверено на
прогоне: «в главном корпусе» → «Головний корпус»).

Определение по буквам, характерным ровно для одного языка:
  украинские  і ї є ґ
  русские     ы э ъ ё
Их достаточно почти всегда. Если ни одной такой буквы нет (короткий текст,
латиница) — смотрим служебные слова. Если и это не помогло, сообщение
считается поддерживаемым: пропустить лишнее дешевле, чем молча потерять
настоящий анонс.
"""

from __future__ import annotations

import re

UK = "uk"
RU = "ru"

_UK_LETTERS = frozenset("іїєґ")
_RU_LETTERS = frozenset("ыэъё")

# Служебные слова для случая, когда характерных букв в тексте не нашлось
# (короткое сообщение, латиница). Пересечения вычитаются автоматически:
# «перед», «кафедра» и т.п. пишутся одинаково и различать язык не помогают.
_UK_RAW = """
    що це ця цей усі всі або як коли хто де ще вже дуже тут щоб якщо
    був була було були є буде будуть може можна потрібно треба його її їх
    після між біля тільки також теж навіть адже лише знову сьогодні вчора
    зараз тепер потім далі друзі колеги нагадую запрошуємо відбудеться
    початок реєстрація участь учасники аудиторії кафедри захід зустріч
    відкрито оголошуємо шановні
"""
_RU_RAW = """
    что это эта этот все всех или как когда кто где ещё еще уже очень здесь
    чтобы если был была было были есть будет будут может можно нужно надо
    его её ее их них после между около только также тоже даже ведь лишь
    снова опять вчера сейчас теперь потом затем друзья коллеги напоминаю
    приглашаем состоится пройдет пройдёт начало регистрация участие
    участники аудитории кафедры мероприятие встреча открыта объявляем
    уважаемые дорогие напоминание
"""
_uk_set = set(_UK_RAW.split())
_ru_set = set(_RU_RAW.split())
# Слова, одинаковые в обоих языках, язык не различают — выбрасываем.
_UK_WORDS = frozenset(_uk_set - _ru_set)
_RU_WORDS = frozenset(_ru_set - _uk_set)

_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)


def detect_language(text: str) -> str | None:
    """'uk' / 'ru' / None, если признаков не хватило."""
    if not text or not text.strip():
        return None
    lowered = text.casefold()

    uk_letters = sum(1 for ch in lowered if ch in _UK_LETTERS)
    ru_letters = sum(1 for ch in lowered if ch in _RU_LETTERS)
    if uk_letters or ru_letters:
        return UK if uk_letters >= ru_letters else RU

    words = set(_WORD_RE.findall(lowered))
    uk_hits = len(words & _UK_WORDS)
    ru_hits = len(words & _RU_WORDS)
    if uk_hits == ru_hits:
        return None
    return UK if uk_hits > ru_hits else RU


def unsupported_reason(text: str, extracted_language: str | None = None) -> str | None:
    """
    Причина, по которой сообщение не обрабатываем, либо None.

    extracted_language — то, что вернула модель в поле language. Учитывается
    как дополнительный сигнал: если модель сама сказала «ru», верим ей.
    """
    detected = detect_language(text)
    if detected == RU:
        return "повідомлення російською — факультет веде канал українською"
    if detected is None and (extracted_language or "").strip().lower() == RU:
        return "модель визначила мову як російську"
    return None


def is_supported(text: str, extracted_language: str | None = None) -> bool:
    return unsupported_reason(text, extracted_language) is None
