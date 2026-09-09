"""
Тесты фильтра устаревших событий.

Отчёт о прошедшем («вчора завершилася наша конференція») модель извлекает как
обычное событие. Смысла класть его в очередь модерации нет: администратор всё
равно отклонит, а в календаре оно не покажется никогда.

Сравнение идёт с датой ПУБЛИКАЦИИ, а не с сегодняшней — иначе сломался бы
импорт истории, где все события «в прошлом» относительно сегодня.
"""

import datetime as dt

from app.models.schemas import ExtractedEvent
from app.services.relevance import (
    cancelled_reason,
    is_stale,
    past_report_reason,
    publication_reason,
    stale_reason,
)

REF = dt.date(2026, 10, 19)


def ev(date):
    return ExtractedEvent(event_title="Конференція", date=date)


def test_yesterday_event_is_stale():
    # Ровно случай из сообщения 202: пост 19-го, конференция закончилась 18-го.
    assert is_stale(ev(dt.date(2026, 10, 18)), REF)


def test_reason_is_explainable():
    reason = stale_reason(ev(dt.date(2026, 10, 12)), REF)
    assert reason and "7 дн" in reason and "2026-10-12" in reason


def test_future_event_is_kept():
    assert not is_stale(ev(dt.date(2026, 10, 20)), REF)


def test_same_day_event_is_kept():
    # «Сьогодні о 18:00» — объявление в день события, всё ещё актуально.
    assert not is_stale(ev(REF), REF)


def test_event_without_date_is_kept():
    # Дату может проставить модератор через PATCH — это чинится, прошедшее нет.
    assert not is_stale(ev(None), REF)


def test_history_import_not_broken():
    # Прошлогодний экспорт: событие в прошлом относительно СЕГОДНЯ, но
    # в будущем относительно своего объявления — должно сохраниться.
    old_post = dt.date(2024, 3, 1)
    assert not is_stale(ev(dt.date(2024, 3, 15)), old_post)


def test_defaults_to_today_without_reference():
    assert is_stale(ev(dt.date(2000, 1, 1)))
    assert not is_stale(ev(dt.date.today() + dt.timedelta(days=1)))


# --- Отчёт о прошедшем распознаётся по формулировке, а не по дате ---


def test_past_report_detected_regardless_of_date():
    # Сообщение 202: модель ставила дату публикации, а «20 листопада» в том
    # же тексте — выход сборника, а не сам заход. Дате верить нельзя.
    text = (
        "Колеги, вчора завершилася наша щорічна конференція «Цифрова "
        "трансформація освіти». Дякуємо всім, хто долучився! Збірник тез "
        "буде опубліковано до 20 листопада."
    )
    assert past_report_reason(text)
    # Дата события «сегодня» — обычная проверка бы не сработала.
    assert is_stale(ev(REF), REF, text)


def test_thanks_to_participants_is_a_report():
    assert past_report_reason("Дякуємо всім, хто долучився до заходу!")


def test_real_announcement_is_not_a_report():
    text = (
        "Запрошуємо на конференцію 26 листопада 2026 року, початок о 10:00 "
        "в актовій залі. Реєстрація триває до 15 листопада."
    )
    assert past_report_reason(text) is None
    assert not is_stale(ev(dt.date(2026, 11, 26)), dt.date(2026, 11, 5), text)


def test_future_verb_not_confused_with_past():
    # «відбудеться» не должно ловиться правилом для «відбулася».
    assert past_report_reason("Захід відбудеться завтра о 14:00") is None


# --- Отменённое событие ---


def test_cancelled_without_new_date_is_dropped():
    text = (
        "Колеги, семінар з методики викладання, що був запланований на "
        "12 листопада, скасовано у зв'язку з відрядженням доповідача. "
        "Наразі узгоджуємо нову дату, орієнтовно друга половина грудня."
    )
    assert cancelled_reason(text)
    assert is_stale(ev(dt.date(2026, 11, 12)), dt.date(2026, 11, 9), text)


def test_reschedule_with_new_date_is_kept():
    # Сообщение 208: перенос — обычный анонс, отсекать нельзя.
    text = (
        "Іспит з дисципліни «Бази даних» для груп КН-31 та КН-32 перенесено "
        "з 20 грудня на 23 грудня 2026 року, ауд. 210, початок о 09:00."
    )
    assert cancelled_reason(text) is None
    assert not is_stale(ev(dt.date(2026, 12, 23)), dt.date(2026, 12, 10), text)


def test_cancelled_but_replacement_announced_is_kept():
    text = "Лекцію скасовано, натомість зустріч відбудеться 20 листопада о 15:00"
    assert cancelled_reason(text) is None


# --- Анонс публикации ---


def test_publication_announcement_is_dropped():
    text = (
        "Вийшов друком збірник наукових праць за матеріалами конференції "
        "«Інформаційні технології 2025». Видання містить 78 статей."
    )
    assert publication_reason(text)
    assert is_stale(ev(dt.date(2026, 9, 30)), dt.date(2026, 9, 30), text)


def test_deadline_for_a_collection_is_not_a_publication():
    # Сообщение 207: тоже про «збірник наукових праць», но это дедлайн подачи
    # тез — настоящее событие. Прошедшего времени издания тут нет.
    text = (
        "Нагадуємо про дедлайн подання тез до збірника наукових праць "
        "факультету. Останній день прийому — 25 вересня 2026 року до 18:00."
    )
    assert publication_reason(text) is None
    assert not is_stale(ev(dt.date(2026, 9, 25)), dt.date(2026, 9, 14), text)
