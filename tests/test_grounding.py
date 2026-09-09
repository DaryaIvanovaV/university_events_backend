"""
Тесты эвристик «заземления» извлечённых полей на исходном тексте.

Смысл проверок: инструмент должен ловить ВЫДУМКИ и при этом не поднимать
тревогу на нормальном перефразе модели («405 аудиторії» → «Ауд. 405»).
Ложная тревога здесь так же вредна, как пропуск: если отчёт краснеет на всём
подряд, его перестают читать. Сети и Ollama тесты не требуют.
"""

import datetime as dt

from app.eval import grounding as g
from app.models.schemas import ExtractedEvent

SRC_LECTURE = (
    "Увага! Завтра о 14:30 в 405 аудиторії відбудеться гостьова лекція "
    "від ІТ-компанії про розробку на Kotlin. Явка для 4 курсу обов'язкова!"
)
REF = dt.date(2026, 4, 2)


# --- Текстовые поля ---


def test_paraphrase_is_not_a_hallucination():
    # Модель обязана перефразировать — это не выдумка.
    check = g.check_text_field("location", "Ауд. 405", SRC_LECTURE)
    assert check.status == g.OK
    assert check.spans, "подтверждающие фрагменты должны быть найдены"


def test_title_from_reordered_words_is_ok():
    check = g.check_text_field(
        "event_title", "Гостьова лекція: розробка на Kotlin", SRC_LECTURE
    )
    assert check.status == g.OK


def test_invented_room_number_is_caught():
    # Самый частый вид выдумки: номер, которого в тексте нет.
    check = g.check_text_field("location", "Ауд. 512", SRC_LECTURE)
    assert check.status == g.INVENTED
    assert "512" in check.note


def test_invented_words_are_caught():
    check = g.check_text_field("organizer", "Кафедра філософії", SRC_LECTURE)
    assert check.status == g.INVENTED


def test_morphology_tolerated():
    # "ІТ-компанія" в тексте стоит в родительном падеже ("ІТ-компанії").
    check = g.check_text_field("organizer", "ІТ-компанія", SRC_LECTURE)
    assert check.status == g.OK


def test_morphology_where_prefix_5_would_fail():
    # Реальные ложные тревоги с прогона: обрезание на 5 символах попадало
    # ровно на окончание и разводило слова, которые совпадают по основе.
    cases = [
        ("target_audience", "Група КН-31", "Іспит для групи КН-31 перенесено"),
        ("target_audience", "Всі охочі", "Захід для всіх охочих"),
        ("organizer", "Науковий гурток", "семінар наукового гуртка з кібербезпеки"),
        ("location", "Актова зала корпусу №3", "в актовій залі корпусу №3"),
    ]
    for field_name, value, source in cases:
        assert g.check_text_field(field_name, value, source).status == g.OK, value


def test_translation_is_still_flagged():
    # Промт запрещает переводить: русский оригинал → украинское значение
    # опоры в тексте не имеет и должно быть замечено.
    check = g.check_text_field("location", "Головний корпус", "Финал пройдёт в главном корпусе")
    assert check.status in (g.SUSPECT, g.INVENTED)


def test_word_numerals_ground_digits():
    # Реальные ложные тревоги с прогона на 30 сообщениях: модель штатно
    # нормализует числительное словом в цифру, и это НЕ выдумка.
    cases = [
        ("location", "Ауд. 405, 3-й корпус", "в аудиторії 405 третього корпусу"),
        ("target_audience", "4 курс", "Увага, четвертий курс! Лекція завтра"),
        ("target_audience", "2 та 3 курси",
         "розрахований на другий і третій курси, але долучатися можуть усі"),
        ("location", "Кабінет 20", "приходьте у двадцятий кабінет"),
    ]
    for field_name, value, source in cases:
        check = g.check_text_field(field_name, value, source)
        assert check.status != g.INVENTED, f"{value} ← {source}: {check.note}"


def test_word_numeral_lookalikes_do_not_ground():
    # "п'ятниця" не подтверждает цифру 5, "тривалість" не подтверждает 3.
    assert "5" not in g._source_numbers("зустріч у п'ятницю в деканаті")
    assert "3" not in g._source_numbers("тривалість заходу — приблизно година")


def test_invented_digit_still_caught_with_numerals_enabled():
    # Послабление не должно ломать главную проверку.
    check = g.check_text_field("location", "Ауд. 512", "в аудиторії 405 третього корпусу")
    assert check.status == g.INVENTED


def test_hour_with_time_word_is_grounded():
    # "з 8 ранку" — час без двоеточия, раньше считался выдумкой.
    assert g.check_time("08:00", "волонтери чергували на реєстрації з 8 ранку").status != g.INVENTED


def test_empty_title_is_invalid():
    check = g.check_text_field("event_title", "   ", SRC_LECTURE)
    assert check.status == g.INVALID


def test_null_is_empty_not_invented():
    check = g.check_text_field("location", None, SRC_LECTURE)
    assert check.status == g.EMPTY


# --- Время ---


def test_time_present_in_text():
    assert g.check_time("14:30", SRC_LECTURE).status == g.OK


def test_time_out_of_range_is_invalid():
    # Схема ExtractedEvent такое пропускает — ловим здесь.
    assert g.check_time("25:99", SRC_LECTURE).status == g.INVALID


def test_time_wrong_format_is_invalid():
    assert g.check_time("14.30", SRC_LECTURE).status == g.INVALID


def test_invented_time_is_caught():
    assert g.check_time("09:00", SRC_LECTURE).status == g.INVENTED


def test_hour_only_in_text_is_suspect():
    check = g.check_time("15:00", "Зустріч відбудеться о 15 год у деканаті")
    assert check.status == g.SUSPECT


# --- Дата ---


def test_relative_tomorrow_resolved_correctly():
    check = g.check_date(dt.date(2026, 4, 3), SRC_LECTURE, REF)
    assert check.status == g.OK


def test_relative_tomorrow_resolved_wrong():
    check = g.check_date(dt.date(2026, 4, 10), SRC_LECTURE, REF)
    assert check.status == g.INVENTED


def test_explicit_date_matches():
    src = "Конференція «ІТ 2026» відбудеться 15-16 травня 2026 року."
    assert g.check_date(dt.date(2026, 5, 15), src, dt.date(2026, 4, 10)).status == g.OK
    assert g.check_date(dt.date(2026, 5, 16), src, dt.date(2026, 4, 10)).status == g.OK


def test_explicit_date_mismatch_is_invented():
    src = "Конференція відбудеться 15 травня 2026 року."
    check = g.check_date(dt.date(2026, 5, 20), src, dt.date(2026, 4, 10))
    assert check.status == g.INVENTED


def test_month_word_inside_other_word_is_not_a_date():
    # "майстер-клас" не должен читаться как май, "марафон" — как март.
    src = "У четвер проведемо майстер-клас та марафон для першокурсників."
    pairs, _, _, _ = g._source_dates(src)
    assert pairs == set()


def test_date_without_any_hint_is_invented():
    src = "Нагадуємо про необхідність оформити перепустки в деканаті."
    check = g.check_date(dt.date(2026, 6, 1), src, REF)
    assert check.status == g.INVENTED


def test_wrong_year_flagged_as_suspect():
    src = "Конференція відбудеться 15 травня 2026 року."
    check = g.check_date(dt.date(2027, 5, 15), src, dt.date(2026, 4, 10))
    assert check.status == g.SUSPECT


# --- Ссылка ---


def test_link_present_in_text():
    src = "Реєстрація за посиланням https://forms.gle/abc123 до п'ятниці."
    assert g.check_link("https://forms.gle/abc123", src).status == g.OK


def test_link_without_scheme_in_text_still_ok():
    src = "Реєстрація: forms.gle/abc123"
    assert g.check_link("https://forms.gle/abc123", src).status == g.OK


def test_invented_link_is_caught():
    src = "Реєстрація за посиланням https://forms.gle/abc123 до п'ятниці."
    check = g.check_link("https://example.com/register", src)
    assert check.status == g.INVENTED


# --- Код зустрічі ---


def test_meeting_code_digits_present():
    src = "Zoom, ідентифікатор конференції 845 2371 9004, код доступу 316742."
    assert g.check_meeting_code("ID 845 2371 9004 · код 316742", src).status == g.OK


def test_meeting_code_matches_joined_digits():
    # В тексте ID с пробелами, в ссылке — слитно. Это одно и то же.
    src = "Підключення https://zoom.us/j/84523719004 , ідентифікатор 845 2371 9004"
    assert g.check_meeting_code("ID 84523719004", src).status == g.OK


def test_invented_meeting_code_is_caught():
    src = "Zoom, ідентифікатор конференції 845 2371 9004, код доступу 316742."
    check = g.check_meeting_code("ID 845 2371 9004 · код 999999", src)
    assert check.status == g.INVENTED
    assert "999999" in check.note


def test_link_missed_when_text_has_one():
    src = "Реєстрація за посиланням https://forms.gle/abc123 до п'ятниці."
    assert g.check_link(None, src).status == g.MISSED


# --- Язык ---


def test_language_mismatch_is_suspect():
    assert g.check_language("ru", SRC_LECTURE).status == g.SUSPECT
    assert g.check_language("uk", SRC_LECTURE).status == g.OK


# --- Уровень сообщения ---


def test_non_event_without_events_is_clean():
    src = "Дякуємо всім, хто прийшов! Гарних вихідних."
    checks = g.check_omissions([], src, prefilter_candidate=False)
    assert all(not c.is_problem for c in checks)


def test_missing_event_on_candidate_is_flagged():
    checks = g.check_omissions([], SRC_LECTURE, prefilter_candidate=True)
    assert any(c.status == g.MISSED for c in checks)


def test_missed_link_flagged_at_message_level():
    src = "Вебінар у середу, реєстрація https://forms.gle/xyz"
    event = ExtractedEvent(event_title="Вебінар", link=None)
    checks = g.check_omissions([event], src, prefilter_candidate=True)
    assert any(c.field == "link" and c.status == g.MISSED for c in checks)


def test_check_event_covers_all_fields():
    event = ExtractedEvent(
        event_title="Гостьова лекція: розробка на Kotlin",
        date=dt.date(2026, 4, 3),
        time="14:30",
        location="Ауд. 405",
        organizer="ІТ-компанія",
        target_audience="4 курс",
        event_type="lecture",
        language="uk",
    )
    checks = g.check_event(event, SRC_LECTURE, REF)
    by_field = {c.field: c for c in checks}
    assert set(by_field) == set(g.FIELD_ORDER)
    # На честном примере не должно быть ни одной выдумки.
    assert not [c for c in checks if c.status in (g.INVENTED, g.INVALID)]
