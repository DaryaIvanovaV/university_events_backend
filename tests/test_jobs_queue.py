"""
Тесты очереди ingest_jobs (app/db/jobs.py).

Проверяется машинерия, на которой держится вариант с отдельным воркером:
постановка в очередь, захват, повторы с паузой, возврат зависших задач.
"""

from __future__ import annotations

import datetime as dt

from app.db import jobs as jobs_repo
from app.models.db import (
    JOB_DONE,
    JOB_FAILED,
    JOB_PROCESSING,
    JOB_QUEUED,
    IngestJob,
)
from app.services.clock import utc_now


def _enqueue(session, text="Конференція AI 1 вересня о 10:00", **kw):
    return jobs_repo.enqueue(session, text, **kw)


# --- Постановка в очередь ---


def test_enqueue_creates_queued_job(db_session):
    job, is_new = _enqueue(db_session, source_channel="@cs", source_message_id=1)
    assert is_new
    assert job.id is not None
    assert job.status == JOB_QUEUED
    assert job.attempts == 0


def test_enqueue_keeps_reference_date(db_session):
    # Без даты публикации не разрешить «завтра» — она обязана доехать до воркера.
    job, _ = _enqueue(db_session, reference_date=dt.date(2026, 11, 5))
    assert job.reference_date == dt.date(2026, 11, 5)


def test_enqueue_same_post_twice_reuses_job(db_session):
    first, is_new = _enqueue(db_session, source_channel="@cs", source_message_id=7)
    second, is_new_again = _enqueue(
        db_session, source_channel="@cs", source_message_id=7
    )
    assert is_new and not is_new_again
    assert second.id == first.id, "повторная доставка поста не должна плодить задачи"


def test_enqueue_after_completion_creates_new_job(db_session):
    # Задача доработала — значит пост можно обработать заново (переобработка
    # штатна: upsert обновит запись). Антидубль действует только на активные.
    first, _ = _enqueue(db_session, source_channel="@cs", source_message_id=8)
    jobs_repo.mark_done(
        db_session, first, event_ids=[], skipped=[], duration_ms=10
    )
    second, is_new = _enqueue(db_session, source_channel="@cs", source_message_id=8)
    assert is_new
    assert second.id != first.id


def test_enqueue_without_source_always_creates(db_session):
    # У ручных прогонов нет message_id — дедуплицировать не по чему.
    first, _ = _enqueue(db_session)
    second, is_new = _enqueue(db_session)
    assert is_new and second.id != first.id


# --- Захват ---


def test_claim_marks_processing_and_counts_attempt(db_session):
    job, _ = _enqueue(db_session)
    claimed = jobs_repo.claim(db_session, worker_id="w1", batch_size=5)
    assert [j.id for j in claimed] == [job.id]
    assert claimed[0].status == JOB_PROCESSING
    assert claimed[0].locked_by == "w1"
    assert claimed[0].attempts == 1
    assert claimed[0].started_at is not None


def test_claim_does_not_return_same_job_twice(db_session):
    _enqueue(db_session)
    assert len(jobs_repo.claim(db_session, worker_id="w1")) == 1
    assert jobs_repo.claim(db_session, worker_id="w2") == []


def test_claim_respects_batch_size_and_order(db_session):
    ids = [_enqueue(db_session)[0].id for _ in range(3)]
    claimed = jobs_repo.claim(db_session, worker_id="w1", batch_size=2)
    assert [j.id for j in claimed] == ids[:2]  # FIFO по id


def test_claim_skips_jobs_scheduled_for_later(db_session):
    job, _ = _enqueue(db_session)
    job.available_at = utc_now() + dt.timedelta(minutes=5)
    db_session.commit()
    assert jobs_repo.claim(db_session, worker_id="w1") == []


# --- Завершение ---


def test_mark_done_records_result(db_session):
    job, _ = _enqueue(db_session)
    (claimed,) = jobs_repo.claim(db_session, worker_id="w1")
    jobs_repo.mark_done(
        db_session,
        claimed,
        event_ids=[11, 12],
        skipped=[{"event_title": "X", "scope": "stale", "reason": "минуле"}],
        duration_ms=4200,
    )
    assert claimed.status == JOB_DONE
    assert claimed.event_ids == [11, 12]
    assert claimed.skipped[0]["scope"] == "stale"
    assert claimed.duration_ms == 4200
    assert claimed.finished_at is not None
    assert claimed.locked_by is None


def test_mark_failed_requeues_with_backoff(db_session):
    job, _ = _enqueue(db_session)
    (claimed,) = jobs_repo.claim(db_session, worker_id="w1")
    jobs_repo.mark_failed(
        db_session, claimed, error="ConnectError", max_attempts=3, backoff_s=30
    )
    assert claimed.status == JOB_QUEUED
    assert claimed.last_error == "ConnectError"
    # Пауза перед повтором: задача не должна забираться немедленно.
    assert jobs_repo.claim(db_session, worker_id="w1") == []


def test_mark_failed_gives_up_after_max_attempts(db_session):
    _enqueue(db_session)
    for attempt in range(3):
        (claimed,) = jobs_repo.claim(db_session, worker_id="w1")
        jobs_repo.mark_failed(
            db_session, claimed, error="boom", max_attempts=3, backoff_s=0
        )
        if attempt < 2:
            assert claimed.status == JOB_QUEUED
    assert claimed.status == JOB_FAILED
    assert claimed.attempts == 3
    assert claimed.finished_at is not None


def test_release_returns_job_without_spending_attempt(db_session):
    # Штатная остановка воркера не должна приближать задачу к failed.
    _enqueue(db_session)
    (claimed,) = jobs_repo.claim(db_session, worker_id="w1")
    assert claimed.attempts == 1
    jobs_repo.release(db_session, claimed, reason="стоп")
    assert claimed.status == JOB_QUEUED
    assert claimed.attempts == 0
    assert len(jobs_repo.claim(db_session, worker_id="w2")) == 1


# --- Зависшие задачи ---


def test_requeue_stale_returns_abandoned_job(db_session):
    _enqueue(db_session)
    (claimed,) = jobs_repo.claim(db_session, worker_id="w1")
    # Имитируем убитый воркер: блокировка взята давно, отчёта нет.
    claimed.locked_at = utc_now() - dt.timedelta(hours=2)
    db_session.commit()

    touched = jobs_repo.requeue_stale(
        db_session, lease_timeout_s=900, max_attempts=3
    )
    assert touched == 1
    db_session.refresh(claimed)
    assert claimed.status == JOB_QUEUED
    assert len(jobs_repo.claim(db_session, worker_id="w2")) == 1


def test_requeue_stale_ignores_fresh_lock(db_session):
    _enqueue(db_session)
    jobs_repo.claim(db_session, worker_id="w1")
    # Воркер работает прямо сейчас — отбирать у него задачу нельзя.
    assert jobs_repo.requeue_stale(
        db_session, lease_timeout_s=900, max_attempts=3
    ) == 0


def test_requeue_stale_fails_job_out_of_attempts(db_session):
    _enqueue(db_session)
    (claimed,) = jobs_repo.claim(db_session, worker_id="w1")
    claimed.attempts = 3
    claimed.locked_at = utc_now() - dt.timedelta(hours=2)
    db_session.commit()

    jobs_repo.requeue_stale(db_session, lease_timeout_s=900, max_attempts=3)
    db_session.refresh(claimed)
    assert claimed.status == JOB_FAILED


# --- Сводка ---


def test_queue_stats_counts_and_timing(db_session):
    _enqueue(db_session)  # останется queued
    _enqueue(db_session)
    (claimed,) = jobs_repo.claim(db_session, worker_id="w1")
    jobs_repo.mark_done(
        db_session, claimed, event_ids=[1], skipped=[], duration_ms=5000
    )

    stats = jobs_repo.queue_stats(db_session)
    assert stats["queued"] == 1
    assert stats["done"] == 1
    assert stats["failed"] == 0
    assert stats["avg_duration_ms"] == 5000
    assert stats["measured_jobs"] == 1


def test_queue_stats_on_empty_queue(db_session):
    stats = jobs_repo.queue_stats(db_session)
    assert stats["queued"] == 0
    assert stats["avg_duration_ms"] is None


def test_list_jobs_filters_by_status(db_session):
    _enqueue(db_session)
    (claimed,) = jobs_repo.claim(db_session, worker_id="w1")
    jobs_repo.mark_done(db_session, claimed, event_ids=[], skipped=[], duration_ms=1)
    _enqueue(db_session)

    assert len(jobs_repo.list_jobs(db_session)) == 2
    assert len(jobs_repo.list_jobs(db_session, status=JOB_QUEUED)) == 1
    assert len(jobs_repo.list_jobs(db_session, status=JOB_DONE)) == 1


def test_skip_locked_used_only_on_postgres(db_session):
    # На SQLite FOR UPDATE SKIP LOCKED не поддерживается; захват обязан
    # работать и без него, иначе все тесты шли бы мимо реального кода.
    assert jobs_repo._supports_skip_locked(db_session) is False
    _enqueue(db_session)
    assert len(jobs_repo.claim(db_session, worker_id="w1")) == 1


def test_job_defaults_are_json_lists(db_session):
    job, _ = jobs_repo.enqueue(db_session, "текст")
    fresh = db_session.get(IngestJob, job.id)
    assert fresh.event_ids == []
    assert fresh.skipped == []
