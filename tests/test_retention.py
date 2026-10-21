"""
Тести автоочищення минулих подій (`app/db/retention.py`).

Перевіряється те, що легко зламати назад:
  * відлік ведеться від СТАРТУ події (дата + час), а не від дати;
  * подія без дати не прибирається ніколи — дату проставить модератор;
  * `rejected` не чіпаємо (живе заради дедуплікації), `pending` прибираємо;
  * протермінована подія доїжджає до клієнта в `removed` у GET /events/sync;
  * фізичне видалення працює ТІЛЬКИ по `expired` і тільки за старою датою.

«Зараз» у тестах задається явним аргументом `moment`: прив'язуватися до
справжньої поточної дати не можна — тест ставав би залежним від дня прогону.
"""

from __future__ import annotations

import datetime as dt

from app.db import retention
from app.models.db import Event
from app.models.schemas import EventType, ExtractedEvent
from app.db import crud

NOW = dt.datetime(2026, 10, 21, 12, 0)


def _event(session, *, title, date, time=None, status="approved", message_id=1):
    ev, _ = crud.upsert_event(
        session,
        ExtractedEvent(
            event_title=title, date=date, time=time,
            event_type=EventType.conference,
        ),
        source_channel="@cs",
        source_message_id=message_id,
    )
    crud.set_event_status(session, ev.id, status)
    return ev


def _status(session, event_id: int) -> str | None:
    ev = session.get(Event, event_id)
    return None if ev is None else ev.status


# --- Протермінування: відлік від старту події ---


def test_expires_event_more_than_day_after_start(db_session):
    ev = _event(db_session, title="Минула подія", date=dt.date(2026, 10, 19))

    assert retention.expire_due_events(
        db_session, after_days=1, moment=NOW
    ) == [ev.id]
    assert _status(db_session, ev.id) == "expired"


def test_keeps_todays_event(db_session):
    ev = _event(db_session, title="Сьогодні", date=NOW.date(), time="18:00")

    assert retention.expire_due_events(db_session, after_days=1, moment=NOW) == []
    assert _status(db_session, ev.id) == "approved"


def test_time_of_start_counts_not_only_date(db_session):
    # Вчора о 18:00 — доба від старту спливе лише сьогодні о 18:00, а
    # «зараз» у тесті 12:00. За датою подія вже виглядає минулою, тому
    # помилка тут непомітна без явної перевірки.
    ev = _event(
        db_session, title="Вчора ввечері",
        date=NOW.date() - dt.timedelta(days=1), time="18:00",
    )

    assert retention.expire_due_events(db_session, after_days=1, moment=NOW) == []

    later = NOW.replace(hour=18, minute=1)
    assert retention.expire_due_events(
        db_session, after_days=1, moment=later
    ) == [ev.id]


def test_unparsable_time_treated_as_start_of_day(db_session):
    ev = _event(
        db_session, title="Зламаний час",
        date=NOW.date() - dt.timedelta(days=1), time="вранці",
    )

    assert retention.expire_due_events(
        db_session, after_days=1, moment=NOW
    ) == [ev.id]


# --- Що не прибирається ---


def test_event_without_date_is_never_expired(db_session):
    ev = _event(db_session, title="Без дати", date=None)

    assert retention.expire_due_events(db_session, after_days=1, moment=NOW) == []
    assert _status(db_session, ev.id) == "approved"


def test_pending_is_expired_too(db_session):
    ev = _event(
        db_session, title="Минула в черзі", date=dt.date(2026, 10, 1),
        status="pending",
    )

    assert retention.expire_due_events(
        db_session, after_days=1, moment=NOW
    ) == [ev.id]


def test_rejected_is_left_alone(db_session):
    ev = _event(
        db_session, title="Відхилена", date=dt.date(2026, 10, 1),
        status="rejected",
    )

    assert retention.expire_due_events(db_session, after_days=1, moment=NOW) == []
    assert _status(db_session, ev.id) == "rejected"


def test_zero_days_disables_expiry(db_session):
    ev = _event(db_session, title="Минула подія", date=dt.date(2026, 1, 1))

    assert retention.expire_due_events(db_session, after_days=0, moment=NOW) == []
    assert _status(db_session, ev.id) == "approved"


# --- Фізичне видалення ---


def test_purge_deletes_only_old_expired(db_session):
    old = _event(
        db_session, title="Давня", date=dt.date(2026, 1, 1), message_id=1,
    )
    recent = _event(
        db_session, title="Свіжа минула", date=dt.date(2026, 10, 10),
        message_id=2,
    )
    rejected = _event(
        db_session, title="Відхилена", date=dt.date(2026, 1, 1),
        status="rejected", message_id=3,
    )
    retention.expire_due_events(db_session, after_days=1, moment=NOW)

    assert retention.purge_expired_events(
        db_session, after_days=90, moment=NOW
    ) == 1
    assert db_session.get(Event, old.id) is None
    assert _status(db_session, recent.id) == "expired"
    assert _status(db_session, rejected.id) == "rejected"


def test_purge_disabled_by_zero(db_session):
    ev = _event(db_session, title="Давня", date=dt.date(2020, 1, 1))
    retention.expire_due_events(db_session, after_days=1, moment=NOW)

    assert retention.purge_expired_events(
        db_session, after_days=0, moment=NOW
    ) == 0
    assert _status(db_session, ev.id) == "expired"


def test_cleanup_runs_both_steps(db_session):
    old = _event(db_session, title="Давня", date=dt.date(2026, 1, 1), message_id=1)
    recent = _event(
        db_session, title="Свіжа минула", date=dt.date(2026, 10, 10),
        message_id=2,
    )

    result = retention.cleanup(
        db_session, expire_after_days=1, purge_after_days=90, moment=NOW
    )

    assert sorted(result.expired_ids) == sorted([old.id, recent.id])
    assert result.purged == 1
    assert result.touched is True
    assert db_session.get(Event, old.id) is None
    assert _status(db_session, recent.id) == "expired"


# --- Контракт мобільного застосунку ---


def test_expired_event_reaches_client_as_removed(client, db_session):
    ev = _event(db_session, title="Минула подія", date=dt.date(2026, 10, 1))
    retention.expire_due_events(db_session, after_days=1, moment=NOW)

    body = client.get("/events/sync").json()

    assert body["removed"] == [ev.id]
    assert body["changed"] == []


def test_expired_event_is_out_of_public_feed(client, db_session):
    _event(db_session, title="Минула подія", date=dt.date(2026, 10, 1))
    retention.expire_due_events(db_session, after_days=1, moment=NOW)

    assert client.get("/events").json() == []
    # Але адміністратор має змогу подивитися прибране.
    assert len(client.get("/events?status=expired").json()) == 1


def test_stats_counts_expired(client, db_session):
    _event(db_session, title="Минула подія", date=dt.date(2026, 10, 1))
    retention.expire_due_events(db_session, after_days=1, moment=NOW)

    events = client.get("/stats").json()["events"]
    assert events["expired"] == 1
    assert events["approved"] == 0
    assert events["total"] == 1
