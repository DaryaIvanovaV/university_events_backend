"""
Тесты защиты от переполнения строковых колонок и от гонок при вставке.

Ловушка, которую эти тесты закрывают: SQLite длину VARCHAR игнорирует, а
PostgreSQL на превышении бросает StringDataRightTruncation и роняет запрос.
То есть баг, невидимый в тестах на SQLite, проявился бы только в проде.
Поэтому проверяем не поведение БД, а то, что crud обрезает значения сам.
"""

from __future__ import annotations

import datetime as dt

from app.db import crud
from app.models.schemas import EventType, ExtractedEvent


def test_long_description_is_truncated(db_session):
    ev = ExtractedEvent(
        event_title="Конференція AI",
        date=dt.date(2099, 9, 1),
        description="я" * 5000,  # колонка — VARCHAR(2048)
    )
    saved, is_new = crud.upsert_event(
        db_session, ev, source_channel="@cs", source_message_id=1
    )
    assert is_new
    assert len(saved.description) == 2048


def test_long_title_is_truncated_not_lost(db_session):
    # Событие важнее аккуратности поля: терять его целиком из-за длины нельзя.
    ev = ExtractedEvent(event_title="Д" * 900, date=dt.date(2099, 9, 1))
    saved, _ = crud.upsert_event(db_session, ev, source_channel="@cs",
                                 source_message_id=2)
    assert len(saved.event_title) == 512


def test_long_raw_text_is_truncated(db_session):
    saved, _ = crud.upsert_event(
        db_session,
        ExtractedEvent(event_title="Подія", date=dt.date(2099, 9, 1)),
        raw_text="т" * 20000,
        source_channel="@cs",
        source_message_id=3,
    )
    assert len(saved.raw_text) == 8192


def test_normal_values_are_untouched(db_session):
    ev = ExtractedEvent(
        event_title="Конференція AI",
        date=dt.date(2099, 9, 1),
        description="Коротко про подію.",
        event_type=EventType.conference,
    )
    saved, _ = crud.upsert_event(db_session, ev, source_channel="@cs",
                                 source_message_id=4)
    assert saved.event_title == "Конференція AI"
    assert saved.description == "Коротко про подію."


# --- Постраничная выдача и счётчики ---


def _approved(session, title, date=dt.date(2099, 9, 1), message_id=0):
    ev, _ = crud.upsert_event(
        session,
        ExtractedEvent(event_title=title, date=date),
        source_channel="@cs",
        source_message_id=message_id,
    )
    crud.set_event_status(session, ev.id, "approved")
    return ev


def test_offset_and_limit_walk_the_whole_list(db_session):
    for i in range(5):
        _approved(db_session, f"Подія {i}", message_id=i)

    seen: list[int] = []
    for offset in (0, 2, 4):
        seen += [e.id for e in crud.list_events(db_session, limit=2, offset=offset)]
    assert len(seen) == 5
    assert len(set(seen)) == 5


def test_count_events_matches_filters(db_session):
    _approved(db_session, "Майбутня", dt.date(2099, 1, 1), message_id=1)
    _approved(db_session, "Минула", dt.date(2020, 1, 1), message_id=2)

    assert crud.count_events(db_session) == 2
    assert crud.count_events(db_session, upcoming_only=True) == 1
    assert crud.count_events(db_session, status="pending") == 0


def test_status_counts_fills_missing_statuses(db_session):
    _approved(db_session, "Подія", message_id=1)
    counts = crud.status_counts(db_session)
    assert counts["approved"] == 1
    assert counts["pending"] == 0
    assert counts["rejected"] == 0
    assert counts["total"] == 1
