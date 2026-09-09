"""Тесты CRUD (дедупликация), пайплайна и модерации на in-memory SQLite.

Архитектура с модерацией: пайплайн создаёт события в статусе pending и НЕ
шлёт push. Публикация и уведомление происходят при approve (для будущих дат).
"""

import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import crud
from app.llm.extractor import EventExtractor
from app.models.db import Base
from app.models.schemas import EventType, EventUpdate, ExtractedEvent
from app.services import moderation
from app.services.pipeline import Pipeline


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    s = Session()
    yield s
    s.close()


def _event(title="Конференція AI", date=dt.date(2099, 9, 1), etype=EventType.conference):
    return ExtractedEvent(event_title=title, date=date, event_type=etype)


def _chat(json_str):
    return lambda messages, schema: json_str  # noqa: E731


# --- CRUD / дедупликация ---


def test_insert_new_event_is_pending(session):
    ev, is_new = crud.upsert_event(
        session, _event(), source_channel="@cs", source_message_id=1
    )
    assert is_new
    assert ev.id is not None
    assert ev.status == "pending"  # ждёт модерации


def test_exact_dedup_same_message_updates(session):
    crud.upsert_event(session, _event(), source_channel="@cs", source_message_id=1)
    ev2, is_new = crud.upsert_event(
        session,
        _event(title="Конференція AI (оновлено)"),
        source_channel="@cs",
        source_message_id=1,
    )
    assert not is_new
    assert ev2.event_title == "Конференція AI (оновлено)"
    assert len(crud.list_events(session, status="all")) == 1


def test_fuzzy_dedup_same_content(session):
    crud.upsert_event(session, _event(), source_channel="@cs", source_message_id=1)
    ev2, is_new = crud.upsert_event(
        session,
        _event(title="  конференція   AI  "),  # тот же контент, иное сообщение
        source_channel="@cs",
        source_message_id=2,
    )
    assert not is_new
    assert len(crud.list_events(session, status="all")) == 1


def test_list_events_defaults_to_approved(session):
    # pending-событие не должно попадать в публичный фид (дефолт = approved).
    ev, _ = crud.upsert_event(session, _event(), source_channel="@cs", source_message_id=1)
    assert crud.list_events(session) == []  # pending скрыт
    crud.set_event_status(session, ev.id, "approved")
    assert len(crud.list_events(session)) == 1


def test_list_upcoming_filters_past(session):
    past, _ = crud.upsert_event(
        session, _event(date=dt.date(2020, 1, 1)), source_channel="@cs", source_message_id=1
    )
    future, _ = crud.upsert_event(
        session,
        _event(title="Майбутня подія", date=dt.date(2099, 1, 1)),
        source_channel="@cs",
        source_message_id=2,
    )
    crud.set_event_status(session, past.id, "approved")
    crud.set_event_status(session, future.id, "approved")
    upcoming = crud.list_events(session, upcoming_only=True)
    assert len(upcoming) == 1
    assert upcoming[0].event_title == "Майбутня подія"


# --- Пайплайн ---


def test_pipeline_creates_pending(session):
    pipeline = Pipeline(
        EventExtractor(
            _chat('{"events":[{"event_title":"Конференція AI","date":"2099-09-01","event_type":"conference"}]}'),
            max_retries=0,
        )
    )
    saved = pipeline.process_message(
        session,
        "Запрошуємо на конференцію AI 1 вересня о 10:00, аудиторія 101",
        source_channel="@cs",
        source_message_id=1,
    )
    assert len(saved) == 1
    assert saved[0].status == "pending"
    # в публичном фиде пусто, пока не подтвердят
    assert crud.list_events(session) == []


def test_pipeline_skips_chatter(session):
    pipeline = Pipeline(EventExtractor(_chat('{"events":[]}'), max_retries=0))
    saved = pipeline.process_message(session, "привіт як справи")
    assert saved == []  # отсечено пре-фильтром, LLM не вызывалась


# --- Модерация ---


def test_approve_future_event_sends_push(session):
    ev, _ = crud.upsert_event(
        session, _event(date=dt.date(2099, 1, 1)), source_channel="@cs", source_message_id=1
    )
    notified = []
    result = moderation.approve_event(
        session, ev.id, notifier=lambda e: notified.append(e.id)
    )
    assert result.status == "approved"
    assert notified == [ev.id]  # будущая дата → push


def test_approve_past_event_no_push(session):
    ev, _ = crud.upsert_event(
        session, _event(date=dt.date(2020, 1, 1)), source_channel="@cs", source_message_id=1
    )
    notified = []
    moderation.approve_event(session, ev.id, notifier=lambda e: notified.append(e.id))
    assert notified == []  # прошлое → без push (важно для импорта истории)


def test_reject_keeps_record_for_dedup(session):
    ev, _ = crud.upsert_event(session, _event(), source_channel="@cs", source_message_id=1)
    moderation.reject_event(session, ev.id)
    # переотправленный тот же анонс не создаст новую запись
    _, is_new = crud.upsert_event(session, _event(), source_channel="@cs", source_message_id=2)
    assert not is_new
    assert len(crud.list_events(session, status="all")) == 1


def test_edit_updates_fields_and_hash(session):
    ev, _ = crud.upsert_event(session, _event(), source_channel="@cs", source_message_id=1)
    old_hash = ev.content_hash
    edited = moderation.edit_event(
        session, ev.id, EventUpdate(location="Ауд. 305", date=dt.date(2099, 9, 2))
    )
    assert edited.location == "Ауд. 305"
    assert edited.date == dt.date(2099, 9, 2)
    assert edited.content_hash != old_hash  # правка даты меняет хэш


# --- Ссылка (link) сквозь слои ---


def test_upsert_persists_link(session):
    ev = ExtractedEvent(
        event_title="Хакатон DevHack",
        date=dt.date(2099, 5, 30),
        link="https://devhack.example/reg",
    )
    saved, is_new = crud.upsert_event(
        session, ev, source_channel="@cs", source_message_id=9
    )
    assert is_new
    assert saved.link == "https://devhack.example/reg"


def test_edit_updates_link(session):
    ev, _ = crud.upsert_event(session, _event(), source_channel="@cs", source_message_id=1)
    edited = moderation.edit_event(
        session, ev.id, EventUpdate(link="https://example.com/details")
    )
    assert edited.link == "https://example.com/details"


# --- Код встречи (meeting_code) сквозь слои ---


def test_upsert_persists_meeting_code(session):
    ev = ExtractedEvent(
        event_title="Вебінар про ML",
        date=dt.date(2099, 10, 29),
        link="https://zoom.us/j/84523719004",
        meeting_code="ID 845 2371 9004 · код 316742",
    )
    saved, is_new = crud.upsert_event(
        session, ev, source_channel="@cs", source_message_id=21
    )
    assert is_new
    assert saved.meeting_code == "ID 845 2371 9004 · код 316742"
    assert saved.link == "https://zoom.us/j/84523719004"


def test_edit_updates_meeting_code(session):
    ev, _ = crud.upsert_event(session, _event(), source_channel="@cs", source_message_id=1)
    edited = moderation.edit_event(
        session, ev.id, EventUpdate(meeting_code="ID 111 222 333")
    )
    assert edited.meeting_code == "ID 111 222 333"


def test_meeting_code_does_not_change_content_hash(session):
    # Хэш дедупликации считается по названию и дате. Если бы новые поля в него
    # входили, все ранее сохранённые записи продублировались бы.
    plain, _ = crud.upsert_event(session, _event(), source_channel="@cs", source_message_id=1)
    with_code = ExtractedEvent(
        event_title="Конференція AI",
        date=dt.date(2099, 9, 1),
        event_type=EventType.conference,
        meeting_code="ID 845 2371 9004",
    )
    same, is_new = crud.upsert_event(
        session, with_code, source_channel="@cs", source_message_id=2
    )
    assert not is_new, "добавление кода не должно плодить дубликаты"
    assert same.id == plain.id


def test_pipeline_recovers_lost_meeting_code(session):
    # Модель отдала событие без кода (так было на реальном прогоне) —
    # нормализация обязана достать ID и код из исходного текста.
    pipeline = Pipeline(
        EventExtractor(
            _chat(
                '{"events":[{"event_title":"Вебінар про ML","date":"2099-10-29",'
                '"time":"12:00","location":"Онлайн","event_type":"webinar"}]}'
            ),
            max_retries=0,
        )
    )
    saved = pipeline.process_message(
        session,
        "Вебінар «Вступ до ML» відбудеться о 12:00. Підключення через Zoom, "
        "ідентифікатор конференції 845 2371 9004, код доступу 316742.",
        source_channel="@cs",
        source_message_id=30,
    )
    assert len(saved) == 1
    assert saved[0].meeting_code == "ID 845 2371 9004 · код 316742"
    assert saved[0].location == "Онлайн (Zoom)"


# --- Фильтр индивидуальных консультаций ---


def test_pipeline_skips_individual_consultation(session):
    pipeline = Pipeline(
        EventExtractor(
            _chat(
                '{"events":[{"event_title":"Індивідуальні консультації",'
                '"date":"2099-11-03","time":"14:00","location":"Каб. 312",'
                '"event_type":"consultation"}]}'
            ),
            max_retries=0,
        )
    )
    result = pipeline.process(
        session,
        "Доц. Іваненко проводить індивідуальні консультації щовівторка "
        "з 14:00 до 16:00, каб. 312, за попереднім записом.",
        source_channel="@cs",
        source_message_id=40,
    )
    assert result.saved == []
    assert len(result.skipped) == 1
    assert result.skipped[0].scope == "individual"
    assert result.skipped[0].reason  # причина уходит в лог
    # В базе не осталось ничего — событие не сохраняется вовсе.
    assert crud.list_events(session, status="all") == []


def test_pipeline_keeps_group_consultation(session):
    pipeline = Pipeline(
        EventExtractor(
            _chat(
                '{"events":[{"event_title":"Консультація перед іспитом",'
                '"date":"2099-12-21","time":"11:00","location":"Ауд. 409",'
                '"event_type":"consultation"}]}'
            ),
            max_retries=0,
        )
    )
    result = pipeline.process(
        session,
        "Нагадуємо про консультацію перед іспитом з дискретної математики "
        "у понеділок о 11:00 в аудиторії 409. Запрошуємо всіх студентів 2 курсу.",
        source_channel="@cs",
        source_message_id=41,
    )
    assert len(result.saved) == 1
    assert result.skipped == []


def test_pipeline_skips_past_event(session):
    # Отчёт о прошедшем: пост 19 октября, конференция завершилась 18-го.
    # Такое не должно занимать очередь модерации.
    pipeline = Pipeline(
        EventExtractor(
            _chat(
                '{"events":[{"event_title":"Щорічна конференція",'
                '"date":"2026-10-18","event_type":"conference"}]}'
            ),
            max_retries=0,
        )
    )
    result = pipeline.process(
        session,
        "Колеги, вчора завершилася наша щорічна конференція «Цифрова "
        "трансформація освіти». Дякуємо всім, хто долучився! Прийняли "
        "340 учасників, заслухали 52 доповіді у 5 секціях.",
        source_channel="@cs",
        source_message_id=50,
        reference_date=dt.date(2026, 10, 19),
    )
    assert result.saved == []
    assert len(result.skipped) == 1
    assert result.skipped[0].scope == "stale"
    # Срабатывает более сильное правило: формулировка «вчора завершилася»
    # надёжнее даты — её модель как раз ставит неверно.
    assert "звіт про минуле" in result.skipped[0].reason
    assert crud.list_events(session, status="all") == []


def test_pipeline_keeps_future_event(session):
    pipeline = Pipeline(
        EventExtractor(
            _chat(
                '{"events":[{"event_title":"Конференція AI","date":"2026-11-26",'
                '"event_type":"conference"}]}'
            ),
            max_retries=0,
        )
    )
    result = pipeline.process(
        session,
        "Запрошуємо на конференцію AI 26 листопада о 10:00, актова зала",
        source_channel="@cs",
        source_message_id=51,
        reference_date=dt.date(2026, 11, 5),
    )
    assert len(result.saved) == 1
    assert result.skipped == []


def test_history_import_still_saves(session):
    # Событие в прошлом относительно сегодня, но в будущем относительно своего
    # объявления — импорт истории не должен обнулиться.
    pipeline = Pipeline(
        EventExtractor(
            _chat(
                '{"events":[{"event_title":"Стара конференція","date":"2024-03-15",'
                '"event_type":"conference"}]}'
            ),
            max_retries=0,
        )
    )
    result = pipeline.process(
        session,
        "Запрошуємо на конференцію 15 березня о 10:00, актова зала",
        source_channel="@cs",
        source_message_id=52,
        reference_date=dt.date(2024, 3, 1),
    )
    assert len(result.saved) == 1


def test_pipeline_skips_russian_before_llm(session):
    # Канал украиноязычный. Русское сообщение отсекается ДО инференса —
    # проверяем, что chat_fn вообще не вызывался.
    calls = []

    def chat_fn(messages, schema):
        calls.append(1)
        return '{"events":[{"event_title":"Конференция","date":"2099-09-01"}]}'

    pipeline = Pipeline(EventExtractor(chat_fn, max_retries=0))
    result = pipeline.process(
        session,
        "Коллеги, приглашаем на конференцию 1 сентября в 10:00, аудитория 101. "
        "Регистрация обязательна, всех желающих ждём!",
        source_channel="@cs",
        source_message_id=60,
    )
    assert calls == [], "LLM не должна вызываться на русском тексте"
    assert result.saved == []
    assert crud.list_events(session, status="all") == []


def test_pipeline_keeps_ukrainian(session):
    pipeline = Pipeline(
        EventExtractor(
            _chat(
                '{"events":[{"event_title":"Конференція AI","date":"2099-09-01",'
                '"language":"uk"}]}'
            ),
            max_retries=0,
        )
    )
    result = pipeline.process(
        session,
        "Запрошуємо на конференцію AI 1 вересня о 10:00, аудиторія 101",
        source_channel="@cs",
        source_message_id=61,
    )
    assert len(result.saved) == 1


def test_process_message_stays_compatible(session):
    # Старый API возвращает только сохранённые события — main.py не менялся.
    pipeline = Pipeline(
        EventExtractor(
            _chat('{"events":[{"event_title":"Конференція AI","date":"2099-09-01"}]}'),
            max_retries=0,
        )
    )
    saved = pipeline.process_message(
        session, "Запрошуємо на конференцію AI 1 вересня о 10:00, аудиторія 101",
        source_channel="@cs", source_message_id=42,
    )
    assert isinstance(saved, list)
    assert saved[0].status == "pending"


def test_get_event_returns_raw_text(session):
    # Оригинал сохраняется при upsert и доступен через get_event —
    # на этом строится GET /events/{id} и кнопка «Оригінал» в боте.
    ev, _ = crud.upsert_event(
        session,
        _event(),
        raw_text="Оригінальний текст оголошення",
        source_channel="@cs",
        source_message_id=1,
    )
    got = crud.get_event(session, ev.id)
    assert got.raw_text == "Оригінальний текст оголошення"


def test_pipeline_drops_duplicate_events_from_one_message(session):
    # Модель иногда возвращает одно событие дважды (наблюдалось на реальном
    # прогоне). Без отсева администратор получил бы две одинаковые карточки.
    pipeline = Pipeline(
        EventExtractor(
            _chat(
                '{"events":['
                '{"event_title":"Тренінг з академічної доброчесності",'
                '"date":"2099-10-06","time":"10:00","event_type":"workshop"},'
                '{"event_title":"Тренінг з академічної доброчесності",'
                '"date":"2099-10-06","time":"10:00","event_type":"workshop"}]}'
            ),
            max_retries=0,
        )
    )
    result = pipeline.process(
        session,
        "Тренінг з академічної доброчесності 6 жовтня о 10:00, ауд. 115",
        source_channel="@cs",
        source_message_id=70,
    )
    assert len(result.saved) == 1


def test_pipeline_keeps_two_distinct_events_from_one_message(session):
    # Дедлайн і сам захід — це ДВІ різні події, дробити їх правильно.
    pipeline = Pipeline(
        EventExtractor(
            _chat(
                '{"events":['
                '{"event_title":"Дедлайн реєстрації","date":"2099-10-30",'
                '"event_type":"deadline"},'
                '{"event_title":"Хакатон DevHack","date":"2099-11-07",'
                '"time":"10:00","event_type":"hackathon"}]}'
            ),
            max_retries=0,
        )
    )
    result = pipeline.process(
        session,
        "Реєстрація на хакатон DevHack триває до 30 жовтня. Сам хакатон "
        "відбудеться 7 листопада о 10:00 у головному корпусі.",
        source_channel="@cs",
        source_message_id=71,
    )
    assert len(result.saved) == 2
