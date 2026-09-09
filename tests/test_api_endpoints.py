"""
Тесты HTTP-слоя после перехода на очередь.

Главное отличие от прежнего поведения: POST /events/ingest больше не выполняет
извлечение и не возвращает события — он отвечает 202 с номером задачи, а
результат забирается из GET /jobs/{id}.
"""

from __future__ import annotations

import datetime as dt

from app.db import crud, jobs as jobs_repo
from app.models.schemas import EventType, ExtractedEvent

MESSAGE = "Запрошуємо на конференцію AI 1 вересня о 10:00, аудиторія 101"


def _event(title="Конференція AI", date=dt.date(2099, 9, 1)):
    return ExtractedEvent(
        event_title=title, date=date, event_type=EventType.conference
    )


# --- Приём сообщений ---


def test_ingest_returns_202_with_job_id(client, db_session):
    resp = client.post(
        "/events/ingest",
        json={"text": MESSAGE, "source_channel": "@cs", "source_message_id": 1},
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "queued"
    assert body["duplicate"] is False
    # Задача действительно легла в очередь, а не потерялась.
    assert jobs_repo.get_job(db_session, body["job_id"]) is not None


def test_ingest_does_not_call_llm(client, monkeypatch):
    # Инференс переехал в воркер: HTTP-запрос обязан вернуться, даже если
    # Ollama вообще не запущена. Раньше он бы там и завис.
    import app.llm.extractor as extractor

    def explode(*args, **kwargs):
        raise AssertionError("API не должен обращаться к модели")

    monkeypatch.setattr(extractor.EventExtractor, "extract", explode)
    assert client.post("/events/ingest", json={"text": MESSAGE}).status_code == 202


def test_ingest_twice_reports_duplicate(client):
    payload = {"text": MESSAGE, "source_channel": "@cs", "source_message_id": 5}
    first = client.post("/events/ingest", json=payload).json()
    second = client.post("/events/ingest", json=payload).json()
    assert second["duplicate"] is True
    assert second["job_id"] == first["job_id"]


def test_ingest_rejects_empty_text(client):
    assert client.post("/events/ingest", json={"text": ""}).status_code == 422


def test_ingest_rejects_oversized_text(client):
    resp = client.post("/events/ingest", json={"text": "а" * 40000})
    assert resp.status_code == 422


# --- Статус задачи ---


def test_get_job_reports_result(client, db_session):
    job_id = client.post("/events/ingest", json={"text": MESSAGE}).json()["job_id"]
    (claimed,) = jobs_repo.claim(db_session, worker_id="w1")
    jobs_repo.mark_done(
        db_session,
        claimed,
        event_ids=[42],
        skipped=[
            {"event_title": "Консультації", "scope": "individual", "reason": "особисті"}
        ],
        duration_ms=3300,
    )

    body = client.get(f"/jobs/{job_id}").json()
    assert body["status"] == "done"
    assert body["event_ids"] == [42]
    assert body["skipped"][0]["scope"] == "individual"
    assert body["duration_ms"] == 3300


def test_get_missing_job_returns_404(client):
    assert client.get("/jobs/9999").status_code == 404


def test_list_jobs_filters_by_status(client, db_session):
    client.post("/events/ingest", json={"text": MESSAGE})
    assert len(client.get("/jobs", params={"status": "queued"}).json()) == 1
    assert client.get("/jobs", params={"status": "done"}).json() == []


def test_list_jobs_rejects_unknown_status(client):
    assert client.get("/jobs", params={"status": "wat"}).status_code == 422


# --- Фид ---


def test_events_feed_returns_total_count_header(client, db_session):
    for i in range(3):
        ev, _ = crud.upsert_event(
            db_session, _event(title=f"Подія {i}"), source_message_id=i,
            source_channel="@cs",
        )
        crud.set_event_status(db_session, ev.id, "approved")

    resp = client.get("/events", params={"limit": 2})
    assert resp.status_code == 200
    assert len(resp.json()) == 2
    assert resp.headers["X-Total-Count"] == "3"


def test_events_feed_paginates_without_gaps(client, db_session):
    # У всех событий одна дата: без вторичной сортировки по id страницы
    # могли бы пересечься или потерять запись.
    for i in range(5):
        ev, _ = crud.upsert_event(
            db_session, _event(title=f"Подія {i}"), source_message_id=i,
            source_channel="@cs",
        )
        crud.set_event_status(db_session, ev.id, "approved")

    first = client.get("/events", params={"limit": 2, "offset": 0}).json()
    second = client.get("/events", params={"limit": 2, "offset": 2}).json()
    third = client.get("/events", params={"limit": 2, "offset": 4}).json()

    ids = [e["id"] for e in first + second + third]
    assert len(ids) == 5
    assert len(set(ids)) == 5, "страницы не должны пересекаться"


def test_events_feed_hides_pending(client, db_session):
    crud.upsert_event(db_session, _event(), source_channel="@cs", source_message_id=1)
    assert client.get("/events").json() == []
    assert client.get("/events", params={"status": "pending"}).json() != []


def test_event_detail_returns_raw_text(client, db_session):
    ev, _ = crud.upsert_event(
        db_session, _event(), raw_text="Оригінал", source_channel="@cs",
        source_message_id=1,
    )
    body = client.get(f"/events/{ev.id}").json()
    assert body["raw_text"] == "Оригінал"


def test_event_detail_404(client):
    assert client.get("/events/424242").status_code == 404


# --- Модерация ---


def test_approve_and_reject(client, db_session):
    ev, _ = crud.upsert_event(
        db_session, _event(), source_channel="@cs", source_message_id=1
    )
    approved = client.post(
        f"/events/{ev.id}/approve", params={"notify": "false"}
    ).json()
    assert approved["status"] == "approved"

    rejected = client.post(f"/events/{ev.id}/reject").json()
    assert rejected["status"] == "rejected"


def test_patch_rejects_too_long_value(client, db_session):
    # Колонка event_title — VARCHAR(512). На PostgreSQL перебор вызвал бы 500;
    # схема ловит его раньше и отвечает понятным 422.
    ev, _ = crud.upsert_event(
        db_session, _event(), source_channel="@cs", source_message_id=1
    )
    resp = client.patch(f"/events/{ev.id}", json={"event_title": "я" * 600})
    assert resp.status_code == 422


def test_patch_updates_field(client, db_session):
    ev, _ = crud.upsert_event(
        db_session, _event(), source_channel="@cs", source_message_id=1
    )
    body = client.patch(f"/events/{ev.id}", json={"location": "Ауд. 305"}).json()
    assert body["location"] == "Ауд. 305"


# --- Пробы и статистика ---


def test_health_live_does_not_touch_db(client):
    assert client.get("/health/live").json() == {"status": "ok"}
    assert client.get("/health").json() == {"status": "ok"}


def test_health_ready_ok_when_ollama_up(client, monkeypatch):
    from app.llm import ollama_status

    monkeypatch.setattr(ollama_status, "check_server", lambda *a, **kw: "0.31.2")
    body = client.get("/health/ready").json()
    assert body["status"] == "ok"
    assert body["checks"] == {"database": "ok", "ollama": "ok"}


def test_health_ready_degraded_without_ollama(client, monkeypatch):
    # Фид и модерация работают без модели — проба не должна падать 503,
    # иначе перезапуск Ollama выводил бы из строя весь API.
    import httpx

    from app.llm import ollama_status

    def down(*args, **kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(ollama_status, "check_server", down)
    resp = client.get("/health/ready")
    assert resp.status_code == 200
    assert resp.json()["status"] == "degraded"


def test_health_ready_503_without_database(client, monkeypatch):
    from app import main

    class DeadEngine:
        def connect(self):
            raise RuntimeError("no database")

    monkeypatch.setattr(main, "engine", DeadEngine())
    resp = client.get("/health/ready")
    assert resp.status_code == 503
    assert resp.json()["status"] == "unavailable"


def test_stats_reports_events_and_queue(client, db_session):
    ev, _ = crud.upsert_event(
        db_session, _event(), source_channel="@cs", source_message_id=1
    )
    crud.set_event_status(db_session, ev.id, "approved")
    client.post("/events/ingest", json={"text": MESSAGE})

    body = client.get("/stats").json()
    assert body["events"]["approved"] == 1
    assert body["events"]["total"] == 1
    assert body["queue"]["queued"] == 1
