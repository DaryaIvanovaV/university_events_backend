"""
Тесты фильтра языка: обрабатываем только украинские объявления.

Русские сообщения отсекаются ДО вызова LLM. Две причины: инференс на них
тратить незачем, и модель норовит перевести текст на украинский вопреки
запрету в промте (проверено на прогоне: «в главном корпусе» → «Головний
корпус»).
"""

from app.services.language import (
    _RU_WORDS,
    _UK_WORDS,
    detect_language,
    is_supported,
    unsupported_reason,
)

UK = "Завтра о 14:30 в 405 аудиторії відбудеться гостьова лекція про Kotlin"
RU = "Завтра в 14:30 в 405 аудитории состоится лекция о Kotlin"


def test_word_lists_are_disjoint():
    # «перед», «кафедра» пишутся одинаково и язык не различают — если бы
    # они остались в обоих списках, голоса гасили бы друг друга.
    assert not (_UK_WORDS & _RU_WORDS)


def test_ukrainian_by_letters():
    assert detect_language(UK) == "uk"


def test_russian_by_letters():
    assert detect_language("Открыта регистрация на хакатон, финал пройдёт в мае") == "ru"


def test_russian_without_special_letters():
    # Ни ы/э/ъ/ё, ни і/ї/є/ґ — решают служебные слова.
    assert detect_language("Друзья, кто знает, когда откроется столовая после ремонта?") == "ru"
    assert detect_language("Коллеги, напоминаю: приглашаем всех, начало в мае") == "ru"


def test_ukrainian_without_special_letters():
    assert detect_language("Шановні студенти, запрошуємо на захід, початок о 10") == "uk"


def test_unknown_language_is_not_rejected():
    # Латиница/непонятный текст — пропускаем: потерять анонс хуже.
    assert detect_language("Docker workshop 15.10 at 14:00") is None
    assert is_supported("Docker workshop 15.10 at 14:00")


def test_russian_message_is_rejected_with_reason():
    reason = unsupported_reason(RU)
    assert reason and "росій" in reason


def test_ukrainian_message_is_supported():
    assert is_supported(UK)


def test_model_language_used_as_fallback():
    # Букв не хватило, но модель сама сказала «ru» — верим ей.
    assert not is_supported("Workshop 15.10", extracted_language="ru")
    assert is_supported("Workshop 15.10", extracted_language="uk")


def test_empty_text():
    assert detect_language("") is None
    assert is_supported("")
