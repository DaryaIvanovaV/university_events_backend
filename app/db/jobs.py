"""
Операции с очередью ingest-задач (таблица `ingest_jobs`).

Роли:
  * HTTP-слой (`POST /events/ingest`) вызывает `enqueue` и сразу отвечает 202;
  * воркер (`app/worker.py`) вызывает `requeue_stale` → `claim` → обработка →
    `mark_done` / `mark_failed`.

Захват задач — `SELECT ... FOR UPDATE SKIP LOCKED`. Строки, уже захваченные
другим воркером, не блокируют читателя, а пропускаются: несколько воркеров
разбирают очередь параллельно и никогда не берут одну задачу дважды.

SKIP LOCKED есть в PostgreSQL, но не в SQLite, на котором идут тесты, поэтому
блокировка добавляется только для PostgreSQL. Для SQLite это безопасно:
конкурентных воркеров там не бывает, а сам файл БД пишется под глобальной
блокировкой.

Все временные отметки очереди — в UTC (см. clock.utc_now): их читает БД, а не
человек, и на SQLite `func.now()` тоже UTC.
"""

from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models.db import (
    JOB_DONE,
    JOB_FAILED,
    JOB_PROCESSING,
    JOB_QUEUED,
    IngestJob,
)
from app.services.clock import utc_now

logger = logging.getLogger(__name__)

# Статусы, в которых задача ещё не доработала: повторно ставить в очередь
# тот же пост не нужно.
ACTIVE_STATUSES = (JOB_QUEUED, JOB_PROCESSING)


def _supports_skip_locked(session: Session) -> bool:
    return session.get_bind().dialect.name == "postgresql"


def enqueue(
    session: Session,
    text: str,
    *,
    source_channel: str | None = None,
    source_message_id: int | None = None,
    reference_date: dt.date | None = None,
) -> tuple[IngestJob, bool]:
    """
    Ставит сообщение в очередь. Возвращает (задача, is_new).

    Если этот же пост уже ждёт обработки (тот же source_channel +
    source_message_id в статусе queued/processing), новая задача НЕ создаётся —
    отдаём существующую. Дубликаты и так отсеются на upsert, но платить за
    них инференсом незачем: повторная доставка одного channel_post — штатная
    ситуация для Telegram.
    """
    if source_channel is not None and source_message_id is not None:
        existing = session.scalar(
            select(IngestJob)
            .where(
                IngestJob.source_channel == source_channel,
                IngestJob.source_message_id == source_message_id,
                IngestJob.status.in_(ACTIVE_STATUSES),
            )
            .order_by(IngestJob.id.desc())
            .limit(1)
        )
        if existing is not None:
            return existing, False

    job = IngestJob(
        text=text,
        source_channel=source_channel,
        source_message_id=source_message_id,
        reference_date=reference_date,
        status=JOB_QUEUED,
        available_at=utc_now(),
        event_ids=[],
        skipped=[],
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    return job, True


def claim(
    session: Session, *, worker_id: str, batch_size: int = 1
) -> list[IngestJob]:
    """
    Забирает до `batch_size` готовых задач и переводит их в processing.

    Захват и смена статуса — одна транзакция: если процесс умрёт между ними,
    откатится всё, и задача останется в очереди.
    """
    now = utc_now()
    stmt = (
        select(IngestJob)
        .where(
            IngestJob.status == JOB_QUEUED,
            IngestJob.available_at <= now,
        )
        .order_by(IngestJob.id)
        .limit(batch_size)
    )
    if _supports_skip_locked(session):
        stmt = stmt.with_for_update(skip_locked=True)

    jobs = list(session.scalars(stmt))
    for job in jobs:
        job.status = JOB_PROCESSING
        job.locked_by = worker_id
        job.locked_at = now
        job.started_at = now
        job.attempts += 1
    session.commit()
    return jobs


def requeue_stale(
    session: Session, *, lease_timeout_s: float, max_attempts: int
) -> int:
    """
    Возвращает в очередь задачи, зависшие в processing.

    Воркера могли убить (`docker compose down`, OOM) прямо во время инференса —
    тогда задача навсегда осталась бы в processing. Считаем брошенной всё, что
    держит блокировку дольше lease-таймаута. Исчерпавшие попытки уходят в
    failed, а не крутятся вечно.

    Возвращает число тронутых задач.
    """
    cutoff = utc_now() - dt.timedelta(seconds=lease_timeout_s)
    base = (
        IngestJob.status == JOB_PROCESSING,
        IngestJob.locked_at.is_not(None),
        IngestJob.locked_at < cutoff,
    )

    failed = session.execute(
        update(IngestJob)
        .where(*base, IngestJob.attempts >= max_attempts)
        .values(
            status=JOB_FAILED,
            finished_at=utc_now(),
            last_error="Воркер не завершил задачу за отведённое время",
            locked_by=None,
            locked_at=None,
        )
    ).rowcount
    requeued = session.execute(
        update(IngestJob)
        .where(*base)
        .values(
            status=JOB_QUEUED,
            available_at=utc_now(),
            locked_by=None,
            locked_at=None,
            last_error="Задача возвращена в очередь: воркер не отчитался",
        )
    ).rowcount
    session.commit()
    if failed or requeued:
        logger.warning(
            "Зависшие задачи: возвращено в очередь %s, помечено failed %s",
            requeued, failed,
        )
    return failed + requeued


def mark_done(
    session: Session,
    job: IngestJob,
    *,
    event_ids: list[int],
    skipped: list[dict],
    duration_ms: int,
) -> IngestJob:
    """Успешное завершение: что сохранили, что отсеяли и за сколько."""
    job.status = JOB_DONE
    job.event_ids = event_ids
    job.skipped = skipped
    job.duration_ms = duration_ms
    job.finished_at = utc_now()
    job.locked_by = None
    job.locked_at = None
    job.last_error = None
    session.commit()
    session.refresh(job)
    return job


def mark_failed(
    session: Session,
    job: IngestJob,
    *,
    error: str,
    max_attempts: int,
    backoff_s: float,
) -> IngestJob:
    """
    Обработка не удалась: либо повтор с паузой, либо окончательный failed.

    Пауза растёт с числом попыток (backoff * 2^(attempts-1)): если лежит
    Ollama, бессмысленно ломиться в неё каждые две секунды.
    """
    job.last_error = error[:4000]
    job.locked_by = None
    job.locked_at = None
    if job.attempts >= max_attempts:
        job.status = JOB_FAILED
        job.finished_at = utc_now()
        logger.error(
            "Задача %s провалена после %s попыток: %s", job.id, job.attempts, error
        )
    else:
        delay = backoff_s * (2 ** (job.attempts - 1))
        job.status = JOB_QUEUED
        job.available_at = utc_now() + dt.timedelta(seconds=delay)
        logger.warning(
            "Задача %s: попытка %s/%s не удалась (%s); повтор через %.0f с",
            job.id, job.attempts, max_attempts, error, delay,
        )
    session.commit()
    session.refresh(job)
    return job


def release(session: Session, job: IngestJob, *, reason: str) -> IngestJob:
    """
    Возвращает захваченную задачу в очередь, НЕ тратя попытку.

    Нужно при штатной остановке воркера: он не успел начать обработку, задача
    ни в чём не виновата, и списывать ей ретрай было бы неверно — иначе
    несколько перезапусков подряд отправили бы её в failed на пустом месте.
    """
    job.status = JOB_QUEUED
    job.attempts = max(0, job.attempts - 1)  # claim увеличил — откатываем
    job.available_at = utc_now()
    job.locked_by = None
    job.locked_at = None
    job.started_at = None
    job.last_error = reason
    session.commit()
    session.refresh(job)
    return job


def get_job(session: Session, job_id: int) -> IngestJob | None:
    return session.get(IngestJob, job_id)


def list_jobs(
    session: Session, *, status: str | None = None, limit: int = 50
) -> list[IngestJob]:
    stmt = select(IngestJob)
    if status is not None:
        stmt = stmt.where(IngestJob.status == status)
    return list(session.scalars(stmt.order_by(IngestJob.id.desc()).limit(limit)))


def queue_stats(session: Session) -> dict:
    """
    Сводка по очереди: сколько задач в каждом статусе и медленно ли идёт
    обработка. Питает GET /stats и таблицу производительности в статье.
    """
    counts = {
        status: count
        for status, count in session.execute(
            select(IngestJob.status, func.count()).group_by(IngestJob.status)
        )
    }
    timing = session.execute(
        select(
            func.avg(IngestJob.duration_ms),
            func.max(IngestJob.duration_ms),
            func.count(IngestJob.id),
        ).where(IngestJob.status == JOB_DONE, IngestJob.duration_ms.is_not(None))
    ).one()
    avg_ms, max_ms, done_with_timing = timing
    return {
        "queued": counts.get(JOB_QUEUED, 0),
        "processing": counts.get(JOB_PROCESSING, 0),
        "done": counts.get(JOB_DONE, 0),
        "failed": counts.get(JOB_FAILED, 0),
        "avg_duration_ms": round(float(avg_ms)) if avg_ms is not None else None,
        "max_duration_ms": int(max_ms) if max_ms is not None else None,
        "measured_jobs": int(done_with_timing or 0),
    }
