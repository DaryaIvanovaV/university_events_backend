"""
Общие помощники для скриптов, работающих через HTTP-API.

Вынесено сюда, потому что после перехода на очередь и simulate.py, и
import_history.py делают одно и то же: получают от /events/ingest номер задачи
и ждут, пока воркер её выполнит.
"""

from __future__ import annotations

import logging
import time

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

FINAL_STATUSES = ("done", "failed")


def wait_for_job(
    http: httpx.Client, job_id: int, timeout_s: float | None = None
) -> dict | None:
    """
    Опрашивает GET /jobs/{id}, пока задача не завершится.

    Возвращает тело задачи (в т.ч. `event_ids`, `skipped`, `last_error`) либо
    None, если результата не дождались за timeout_s. Сетевые ошибки во время
    опроса не прерывают ожидание: backend мог перезапуститься, а задача
    в БД никуда не делась.
    """
    timeout_s = settings.job_poll_timeout_s if timeout_s is None else timeout_s
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            resp = http.get(f"/jobs/{job_id}")
            resp.raise_for_status()
            job = resp.json()
            if job["status"] in FINAL_STATUSES:
                return job
        except httpx.HTTPError as exc:
            logger.warning("job %s: ошибка чтения (%s)", job_id, exc)
        time.sleep(settings.job_poll_interval_s)
    return None
