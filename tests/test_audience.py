"""
Тесты фильтра индивидуальных консультаций.

Две стороны одинаково важны:
  * персональные консультации («за попереднім записом») в календарь не идут;
  * массовые консультации перед экзаменом — идут. Пере-фильтрация тут хуже
    недо-фильтрации: лишнее уберёт модератор, а потерянное не увидит никто.
"""

from app.models.schemas import EventType, ExtractedEvent
from app.services.audience import (
    GROUP,
    INDIVIDUAL,
    PUBLIC,
    classify_scope,
    is_individual,
)


def ev(title="Консультація", audience=None, etype=EventType.consultation):
    return ExtractedEvent(
        event_title=title, target_audience=audience, event_type=etype
    )


# --- Отсекаем персональное ---


def test_individual_consultation_is_filtered():
    text = (
        "Доц. Іваненко проводить індивідуальні консультації з дискретної "
        "математики щовівторка з 14:00 до 16:00, каб. 312."
    )
    verdict = classify_scope(ev(), text)
    assert verdict.scope == INDIVIDUAL
    assert verdict.individual_markers


def test_by_appointment_is_filtered():
    text = "Консультації з курсової роботи — за попереднім записом, каб. 210."
    assert is_individual(ev(), text)


def test_office_hours_are_filtered():
    text = "Години прийому завідувача кафедри: середа 13:00-15:00, каб. 201."
    assert is_individual(ev(), text)


def test_marker_in_target_audience_is_seen():
    # Формулировка часто попадает именно в аудиторию, а не в тело объявления.
    text = "Консультація з дипломної роботи, каб. 305, четвер."
    assert is_individual(ev(audience="за попереднім записом"), text)


def test_teacher_schedule_is_filtered():
    text = "Оновлено графік консультацій викладачів кафедри на листопад."
    assert is_individual(ev(), text)


# --- Массовое остаётся ---


def test_group_exam_consultation_is_kept():
    # Реальное сообщение 228 датасета — консультация для потока перед экзаменом.
    text = (
        "Нагадуємо про консультацію перед іспитом з дискретної математики. "
        "Консультація відбудеться у понеділок о 11:00 в аудиторії 409 першого "
        "корпусу. Викладач розбере типові помилки з минулорічних робіт. "
        "Відвідування не обов'язкове."
    )
    assert not is_individual(ev(), text)


def test_lecture_is_public():
    text = (
        "Завтра о 14:30 в 405 аудиторії гостьова лекція про Kotlin. "
        "Явка для 4 курсу обов'язкова!"
    )
    verdict = classify_scope(ev(title="Лекція", etype=EventType.lecture), text)
    assert verdict.scope == PUBLIC


def test_collective_marker_overrides_individual():
    # Есть и «за записом», и «для всіх охочих» — массовость перевешивает.
    text = (
        "Відкрита лекція для всіх охочих, вхід вільний. Для зручності — "
        "реєстрація за записом на сайті."
    )
    verdict = classify_scope(ev(etype=EventType.lecture), text)
    assert verdict.scope == GROUP
    assert not verdict.is_individual
    assert verdict.individual_markers and verdict.collective_markers


def test_consultation_type_alone_does_not_filter():
    # Тип consultation сам по себе не повод отсекать — нужен маркер формата.
    text = "Консультація для групи КН-31 перед захистом, ауд. 210, о 10:00."
    assert not is_individual(ev(), text)


def test_verdict_explains_itself():
    text = "Індивідуальні консультації за попереднім записом."
    verdict = classify_scope(ev(), text)
    assert verdict.reason, "решение должно быть объяснимо в логе"
    assert "запис" in verdict.reason or "індивідуальн" in verdict.reason
