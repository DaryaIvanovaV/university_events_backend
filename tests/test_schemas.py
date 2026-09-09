"""Тесты Pydantic-схем."""

import datetime as dt

import pytest
from pydantic import ValidationError

from app.models.schemas import (
    EventDetail,
    EventList,
    EventOut,
    EventType,
    ExtractedEvent,
)


def test_valid_event_parses():
    raw = (
        '{"events":[{"event_title":"Гостьова лекція: Kotlin",'
        '"date":"2026-04-03","time":"14:30","location":"Ауд. 405",'
        '"organizer":null,"target_audience":"4 курс","event_type":"lecture",'
        '"description":null,"language":"uk"}]}'
    )
    parsed = EventList.model_validate_json(raw)
    assert len(parsed.events) == 1
    ev = parsed.events[0]
    assert ev.event_title == "Гостьова лекція: Kotlin"
    assert ev.date == dt.date(2026, 4, 3)
    assert ev.event_type is EventType.lecture
    assert ev.organizer is None


def test_empty_event_list():
    parsed = EventList.model_validate_json('{"events":[]}')
    assert parsed.events == []


def test_invalid_date_rejected():
    # 2026-13-45 синтаксически строка, но НЕ валидная дата → ValidationError.
    raw = '{"events":[{"event_title":"X","date":"2026-13-45"}]}'
    with pytest.raises(ValidationError):
        EventList.model_validate_json(raw)


def test_missing_title_rejected():
    with pytest.raises(ValidationError):
        ExtractedEvent.model_validate({"date": "2026-04-03"})


def test_defaults_fill_nulls():
    ev = ExtractedEvent(event_title="Конференція")
    assert ev.date is None
    assert ev.event_type is EventType.other


def test_schema_is_generatable():
    # Схема должна строиться — её мы передаём в Ollama format=.
    schema = EventList.model_json_schema()
    assert "events" in schema["properties"]


def test_event_detail_exposes_raw_text_but_list_does_not():
    # Оригинал (raw_text) доступен в детали события, но НЕ в публичном списке.
    assert "raw_text" in EventDetail.model_fields
    assert "raw_text" not in EventOut.model_fields


def test_link_parses_and_defaults():
    # По умолчанию ссылки нет.
    assert ExtractedEvent(event_title="Конференція").link is None
    # Если модель вернула URL — он сохраняется как есть.
    raw = (
        '{"events":[{"event_title":"Хакатон DevHack","date":"2026-05-30",'
        '"link":"https://devhack.example/reg"}]}'
    )
    parsed = EventList.model_validate_json(raw)
    assert parsed.events[0].link == "https://devhack.example/reg"
