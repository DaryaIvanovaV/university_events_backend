"""
Тесты раскладки онлайн-встречи по полям: link / meeting_code / location.

Контрольный случай взят с реального прогона (сообщение 221 датасета): модель
вернула link=None и пустой description, ID конференции и код доступа пропали
целиком. Нормализация обязана их восстановить, ничего не выдумывая.
"""

from app.models.schemas import ExtractedEvent
from app.services.meeting import (
    detect_platform,
    find_meeting_code,
    find_urls,
    normalize_meeting,
)

# Тот самый текст, на котором данные терялись.
ZOOM_TEXT = (
    "Наступного четверга, 29 жовтня, о 12:00 відбудеться онлайн-вебінар "
    "«Вступ до машинного навчання». Підключення через Zoom, ідентифікатор "
    "конференції 845 2371 9004, код доступу 316742. Посилання продублюємо "
    "тут у каналі за годину до початку."
)
ZOOM_WITH_URL = (
    "Вебінар о 12:00. Підключення: https://zoom.us/j/84523719004 , "
    "ідентифікатор конференції 845 2371 9004, код доступу 316742."
)


# --- Извлечение по кусочкам ---


def test_finds_meeting_id_and_passcode():
    assert find_meeting_code(ZOOM_TEXT) == "ID 845 2371 9004 · код 316742"


def test_finds_passcode_alone():
    assert find_meeting_code("Пароль: 4821") == "код 4821"


def test_no_code_in_plain_text():
    assert find_meeting_code("Лекція завтра о 14:30 в 405 аудиторії") is None


def test_url_without_trailing_punctuation():
    # Точка в конце предложения не должна попадать в ссылку.
    assert find_urls("Реєстрація: https://forms.gle/abc123.") == [
        "https://forms.gle/abc123"
    ]


def test_bare_www_url_gets_scheme():
    assert find_urls("Деталі на www.kn.example.ua/reg") == [
        "https://www.kn.example.ua/reg"
    ]


def test_platform_detected():
    assert detect_platform(ZOOM_TEXT) == "Zoom"
    assert detect_platform("Зустріч у Google Meet") == "Google Meet"
    assert detect_platform("Лекція в 405 аудиторії") is None


# --- Полная нормализация ---


def test_lost_code_is_recovered():
    # Ровно то, что модель отдала на сообщении 221.
    event = ExtractedEvent(
        event_title="Вебінар «Вступ до машинного навчання»", location="Онлайн"
    )
    fixed = normalize_meeting(event, ZOOM_TEXT)
    assert fixed.meeting_code == "ID 845 2371 9004 · код 316742"
    assert fixed.link is None, "URL в тексте нет — выдумывать нельзя"
    assert fixed.location == "Онлайн (Zoom)"


def test_url_and_code_go_to_separate_fields():
    event = ExtractedEvent(event_title="Вебінар")
    fixed = normalize_meeting(event, ZOOM_WITH_URL)
    assert fixed.link == "https://zoom.us/j/84523719004"
    assert fixed.meeting_code == "ID 845 2371 9004 · код 316742"
    # Код не должен просачиваться в ссылку.
    assert "316742" not in fixed.link


def test_url_never_stays_in_location():
    # Приложение по location показывает место — ссылка там дублирует link.
    event = ExtractedEvent(
        event_title="Вебінар", location="https://zoom.us/j/84523719004"
    )
    fixed = normalize_meeting(event, ZOOM_WITH_URL)
    assert "http" not in (fixed.location or "")
    assert fixed.location == "Онлайн (Zoom)"


def test_invented_link_replaced_by_real_one():
    # Модель уже выдумывала URL на прогоне — берём тот, что есть в тексте.
    event = ExtractedEvent(event_title="Вебінар", link="https://example.com/fake")
    fixed = normalize_meeting(event, ZOOM_WITH_URL)
    assert fixed.link == "https://zoom.us/j/84523719004"


def test_invented_link_dropped_when_text_has_none():
    # Реальный случай: модель скопировала URL из few-shot примера в сообщение,
    # где ссылки не было вовсе. Выдуманный адрес хуже отсутствующего —
    # студент кликнет и попадёт не туда.
    event = ExtractedEvent(
        event_title="Вебінар", link="https://zoom.us/j/84523719004"
    )
    fixed = normalize_meeting(event, ZOOM_TEXT)  # в тексте URL нет
    assert fixed.link is None
    # Код при этом сохраняется — он в тексте есть.
    assert fixed.meeting_code == "ID 845 2371 9004 · код 316742"


def test_model_values_are_kept_when_correct():
    event = ExtractedEvent(
        event_title="Вебінар",
        link="https://zoom.us/j/84523719004",
        meeting_code="ID 845 2371 9004 · код 316742",
        location="Онлайн (Zoom)",
    )
    assert normalize_meeting(event, ZOOM_WITH_URL) == event


def test_offline_event_untouched():
    text = "Завтра о 14:30 в 405 аудиторії лекція про Kotlin"
    event = ExtractedEvent(event_title="Лекція", location="Ауд. 405")
    fixed = normalize_meeting(event, text)
    assert fixed.location == "Ауд. 405"
    assert fixed.link is None
    assert fixed.meeting_code is None


def test_normalize_is_idempotent():
    event = ExtractedEvent(event_title="Вебінар")
    once = normalize_meeting(event, ZOOM_WITH_URL)
    assert normalize_meeting(once, ZOOM_WITH_URL) == once
