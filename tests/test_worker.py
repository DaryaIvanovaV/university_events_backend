"""
Тесты воркера очереди (app/worker.py).

Проверяем то, ради чего он существует: задача доходит до пайплайна, результат
и причины отсева записываются в задачу, а сбой модели не теряет сообщение,
а откладывает его на повтор.
"""

from __future__ import annotations

import datetime as dt

import httpx
import pytest

from app import worker
from app.db import crud, jobs as jobs_repo
from app.llm.extractor import EventExtractor
from app.models.db import JOB_DONE, JOB_FAILED, JOB_QUEUED
from app.services.pipeline import Pipeline

ANNOUNCEMENT = "Запрошуємо на конференцію AI 1 вересня о 10:00, аудиторія 101"
CONSULTATION = (
    "Доц. Іваненко проводить індивідуальні консультації щовівторка "
    "з 14:00 до 16:00, каб. 312, за попереднім записом."
)


def _pipeline(json_str: str) -> Pipeline:
    return Pipeline(EventExtractor(lambda messages, schema: json_str, max_retries=0))


def _failing_pipeline(exc: Exception) -> Pipeline:
    def chat(messages, schema):
        raise exc

    return Pipeline(EventExtractor(chat, max_retries=0))


def _claim_one(session, text=ANNOUNCEMENT, **kw):
    jobs_repo.enqueue(session, text, **kw)
    (job,) = jobs_repo.claim(session, worker_id="test")
    return job


def test_process_job_saves_events_and_timing(db_session):
    job = _claim_one(db_session, source_channel="@cs", source_message_id=1)
    pipeline = _pipeline(
        '{"events":[{"event_title":"Конференція AI","date":"2099-09-01",'
        '"event_type":"conference"}]}'
    )
    worker.process_job(db_session, job, pipeline)

    assert job.status == JOB_DONE
    assert len(job.event_ids) == 1
    assert job.duration_ms is not None
    saved = crud.get_event(db_session, job.event_ids[0])
    assert saved.event_title == "Конференція AI"
    assert saved.status == "pending"  # ждёт модерации, в фид не попал


def test_process_job_records_skip_reason(db_session):
    # Индивидуальная консультация не сохраняется, но причина обязана дойти
    # до клиента: иначе «сообщение пропало» неотличимо от «отфильтровано».
    job = _claim_one(db_session, text=CONSULTATION)
    pipeline = _pipeline(
        '{"events":[{"event_title":"Індивідуальні консультації",'
        '"date":"2099-11-03","time":"14:00","event_type":"consultation"}]}'
    )
    worker.process_job(db_session, job, pipeline)

    assert job.status == JOB_DONE
    assert job.event_ids == []
    assert len(job.skipped) == 1
    assert job.skipped[0]["scope"] == "individual"
    assert job.skipped[0]["reason"]


def test_process_job_requeues_when_ollama_down(db_session):
    # Недоступная модель — не потеря сообщения: задача возвращается в очередь.
    job = _claim_one(db_session)
    worker.process_job(
        db_session, job, _failing_pipeline(httpx.ConnectError("refused"))
    )

    assert job.status == JOB_QUEUED
    assert "ConnectError" in job.last_error
    assert job.attempts == 1


def test_process_job_fails_after_max_attempts(db_session, monkeypatch):
    monkeypatch.setattr(worker.settings, "worker_max_attempts", 1)
    monkeypatch.setattr(worker.settings, "worker_retry_backoff_s", 0)
    job = _claim_one(db_session)
    worker.process_job(
        db_session, job, _failing_pipeline(httpx.ConnectError("refused"))
    )
    assert job.status == JOB_FAILED


def test_invalid_json_from_model_is_not_an_error(db_session):
    # ExtractionError пайплайн гасит сам: модель ответила мусором, событий нет,
    # но задача выполнена — повторять её бессмысленно, ответ детерминирован.
    job = _claim_one(db_session)
    worker.process_job(db_session, job, _pipeline("не json"))
    assert job.status == JOB_DONE
    assert job.event_ids == []


def test_missing_model_requeues_instead_of_silently_finishing(db_session):
    """
    Модель не скачана — Ollama отвечает 404. Это НЕ «событий не найдено».

    Регрессия из реального прогона: стек подняли до `ollama pull`, и каждая
    задача закрывалась как выполненная с нулём событий. Весь входящий поток
    исчезал бесследно. Теперь недоступность модели возвращает задачу в очередь.
    """
    job = _claim_one(db_session)
    response = httpx.Response(404, request=httpx.Request("POST", "http://o/api/chat"))
    error = httpx.HTTPStatusError("404", request=response.request, response=response)

    worker.process_job(db_session, job, _failing_pipeline(error))

    assert job.status == JOB_QUEUED, "сообщение обязано остаться в очереди"
    assert "LLMUnavailableError" in job.last_error
    assert job.event_ids == []


def test_run_once_processes_queue(db_session, monkeypatch):
    monkeypatch.setattr(worker, "SessionLocal", lambda: db_session)
    jobs_repo.enqueue(db_session, ANNOUNCEMENT, source_channel="@cs",
                      source_message_id=3)

    handled = worker.run_once(
        _pipeline(
            '{"events":[{"event_title":"Конференція AI","date":"2099-09-01"}]}'
        ),
        worker_id="test",
    )
    assert handled == 1
    assert worker.run_once(_pipeline('{"events":[]}'), worker_id="test") == 0


def test_run_once_releases_batch_on_shutdown(db_session, monkeypatch):
    monkeypatch.setattr(worker, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(worker.settings, "worker_batch_size", 2)
    jobs_repo.enqueue(db_session, ANNOUNCEMENT, source_message_id=1,
                      source_channel="@cs")
    jobs_repo.enqueue(db_session, ANNOUNCEMENT, source_message_id=2,
                      source_channel="@cs")

    worker._shutdown.set()
    try:
        worker.run_once(_pipeline('{"events":[]}'), worker_id="test")
    finally:
        worker._shutdown.clear()

    # Ничего не должно застрять в processing, и попытки не потрачены.
    stats = jobs_repo.queue_stats(db_session)
    assert stats["processing"] == 0
    assert stats["queued"] == 2
    assert all(j.attempts == 0 for j in jobs_repo.list_jobs(db_session))


def test_serialize_skipped_keeps_full_event(db_session):
    # В задаче сохраняется и сам объект события: по нему видно, что именно
    # выдумала модель, — это материал для разбора ошибок в статье.
    job = _claim_one(db_session, text=CONSULTATION)
    worker.process_job(
        db_session,
        job,
        _pipeline(
            '{"events":[{"event_title":"Індивідуальні консультації",'
            '"date":"2099-11-03","event_type":"consultation"}]}'
        ),
    )
    assert job.skipped[0]["event"]["event_title"] == "Індивідуальні консультації"


@pytest.mark.parametrize("reference", [dt.date(2026, 11, 5), None])
def test_reference_date_reaches_pipeline(db_session, reference):
    job = _claim_one(db_session)
    job.reference_date = reference
    seen: list = []

    class Recording(Pipeline):
        def process(self, session, text, **kwargs):
            seen.append(kwargs["reference_date"])
            return super().process(session, text, **kwargs)

    pipeline = Recording(
        EventExtractor(lambda m, s: '{"events":[]}', max_retries=0)
    )
    worker.process_job(db_session, job, pipeline)
    assert seen == [reference]
