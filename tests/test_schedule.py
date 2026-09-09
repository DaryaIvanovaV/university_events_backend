"""
Тесты детерминированной правки даты и времени.

Кейсы взяты из ручной разметки: модель системно ошибается на днях недели
(«наступної п'ятниці» от понедельника дала воскресенье) и возвращает диапазон
там, где схема требует "HH:MM".
"""

import datetime as dt

from app.models.schemas import ExtractedEvent
from app.services.schedule import (
    normalize_schedule,
    normalize_time,
    resolve_relative_date,
)


# --- Время ---


def test_time_range_collapses_to_start():
    # Аудитория приходит к началу; диапазон остаётся в описании.
    assert normalize_time("11:00-16:00") == "11:00"
    assert normalize_time("9.00 – 17.00") == "09:00"


def test_time_is_zero_padded():
    assert normalize_time("9:00") == "09:00"


def test_valid_time_untouched():
    assert normalize_time("14:30") == "14:30"


def test_garbage_time_becomes_null():
    # Схема обещает HH:MM, но валидаторов нет — чиним здесь.
    assert normalize_time("25:99") is None
    assert normalize_time("скоро") is None
    assert normalize_time("") is None


# --- Относительные даты ---


def test_next_friday_is_next_week():
    # Реальная ошибка модели: от понедельника 02.11 она дала 08.11 (воскресенье).
    assert resolve_relative_date(
        "Наступної п'ятниці о 15:00 засідання гуртка", dt.date(2026, 11, 2)
    ) == dt.date(2026, 11, 13)


def test_bare_weekday_is_nearest_ahead():
    # Модель давала 17.12 (четверг) вместо ближайшего понедельника.
    assert resolve_relative_date(
        "Консультація відбудеться у понеділок о 11:00", dt.date(2026, 12, 16)
    ) == dt.date(2026, 12, 21)


def test_same_weekday_moves_a_week_ahead():
    # Сообщение в четверг про «у четвер» — имеется в виду следующий.
    assert resolve_relative_date(
        "У четвер о 12:00 вебінар", dt.date(2026, 10, 22)
    ) == dt.date(2026, 10, 29)


def test_tomorrow_and_today():
    ref = dt.date(2026, 9, 21)
    assert resolve_relative_date("Завтра о 14:30 лекція", ref) == dt.date(2026, 9, 22)
    assert resolve_relative_date("Сьогодні о 18:00 зустріч", ref) == ref
    assert resolve_relative_date("Післязавтра екзамен", ref) == dt.date(2026, 9, 23)


def test_no_relative_hint():
    assert resolve_relative_date("Конференція відбудеться у травні", dt.date(2026, 4, 1)) is None


# --- Сборка ---


def test_explicit_date_is_not_overridden():
    # Явные даты модель извлекает верно — не мешаем ей.
    event = ExtractedEvent(event_title="Лекція", date=dt.date(2026, 12, 3))
    fixed = normalize_schedule(
        event, "3 грудня 2026 року о 13:00 відбудеться лекція", dt.date(2026, 11, 24)
    )
    assert fixed.date == dt.date(2026, 12, 3)


def test_relative_date_is_corrected():
    event = ExtractedEvent(event_title="Семінар", date=dt.date(2026, 11, 8))
    fixed = normalize_schedule(
        event, "Наступної п'ятниці о 15:00 засідання гуртка", dt.date(2026, 11, 2)
    )
    assert fixed.date == dt.date(2026, 11, 13)


def test_time_and_date_fixed_together():
    event = ExtractedEvent(
        event_title="Ярмарок", date=dt.date(2026, 11, 9), time="11:00-16:00"
    )
    fixed = normalize_schedule(
        event, "Завтра ярмарок вакансій з 11:00 до 16:00", dt.date(2026, 11, 8)
    )
    assert fixed.time == "11:00"
    assert fixed.date == dt.date(2026, 11, 9)


def test_untouched_event_returns_same_object():
    event = ExtractedEvent(event_title="Лекція", date=dt.date(2026, 12, 3), time="13:00")
    assert normalize_schedule(event, "3 грудня о 13:00", dt.date(2026, 11, 1)) == event


# --- Прошедшие смещения и «через N» ---


def test_past_shifts():
    ref = dt.date(2026, 10, 19)
    assert resolve_relative_date("вчора завершилася конференція", ref) == dt.date(2026, 10, 18)
    assert resolve_relative_date("позавчора був семінар", ref) == dt.date(2026, 10, 17)


def test_in_n_days_and_weeks():
    ref = dt.date(2026, 10, 19)
    assert resolve_relative_date("через 3 дні захід", ref) == dt.date(2026, 10, 22)
    assert resolve_relative_date("через тиждень зустріч", ref) == dt.date(2026, 10, 26)
    assert resolve_relative_date("через два тижні школа", ref) == dt.date(2026, 11, 2)


def test_mixed_past_and_future_is_ambiguous():
    # «Вчора завершився X, а завтра стартує Y» — какое смещение относится к
    # событию, по тексту не решить. Молча выбрать одно хуже, чем не трогать.
    assert resolve_relative_date(
        "Вчора завершився форум, а завтра стартує хакатон", dt.date(2026, 10, 19)
    ) is None


def test_before_yesterday_not_caught_by_yesterday():
    # «позавчора» содержит «вчора» — порядок проверки должен это учитывать.
    ref = dt.date(2026, 10, 19)
    assert resolve_relative_date("позавчора", ref) == ref - dt.timedelta(days=2)
