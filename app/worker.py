"""
Воркер очереди: разбирает `ingest_jobs` и прогоняет сообщения через пайплайн.

Запуск:  python -m app.worker

Зачем отдельный процесс. Извлечение одного сообщения занимает 3–6 с на GPU, а
таймаут генерации выставлен в 300 с на случай холодного старта. Если делать это
внутри HTTP-запроса, то при импорте истории все рабочие потоки FastAPI окажутся
заняты инференсом, и `GET /events` для мобильного приложения перестанет
отвечать. Поэтому API только кладёт задачу в очередь и отвечает 202, а тяжёлую
часть выполняет этот процесс — его можно перезапускать, останавливать и
масштабировать независимо от API.

Цикл:
    вернуть в очередь зависшее (requeue_stale)
      → забрать пачку (claim, FOR UPDATE SKIP LOCKED)
      → обработать каждую задачу пайплайном
      → mark_done / mark_failed (с паузой перед повтором)
      → пауза worker_poll_interval_s

Воркеров можно запустить несколько (`docker compose up --scale worker=2`):
SKIP LOCKED гарантирует, что одну задачу не возьмут двое. Смысл в этом
появляется, только если Ollama обслуживает параллельные запросы — иначе они
всё равно выстроятся в очередь на GPU.

Остановка: SIGINT/SIGTERM (`docker compose stop`) — воркер дорабатывает
текущую задачу и выходит, не бросая её в processing.
"""

from __future__ import annotations

import logging
import signal
import threading
import time

from app.config import settings
from app.db import jobs as jobs_repo
from app.db.database import SessionLocal
from app.llm import ollama_status
from app.llm.extractor import EventExtractor, make_ollama_chat_fn
from app.models.db import IngestJob
from app.services.pipeline import Pipeline, ProcessResult

logger = logging.getLogger("worker")

# Событие взводится обработчиком сигнала; используется и как прерываемый sleep.
_shutdown = threading.Event()


def _install_signal_handlers() -> None:
    def _handle(signum, _frame):
        logger.info("Получен сигнал %s — завершаюсь после текущей задачи.", signum)
        _shutdown.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handle)
        except (ValueError, AttributeError, OSError):
            # Не главный поток или сигнала нет на этой платформе — не критично.
            pass


def _serialize_skipped(result: ProcessResult) -> list[dict]:
    """
    Отсеянные события — в JSON для колонки `skipped`.

    Это не отладочный шум: именно здесь видно, сколько сообщений и по какой
    причине не дошли до модерации (индивидуальные консультации, отчёты о
    прошедшем, отменённые события). Клиент читает их через GET /jobs/{id}.
    """
    out: list[dict] = []
    for item in result.skipped:
        out.append(
            {
                "event_title": item.event.event_title,
                "scope": item.scope,
                "reason": item.reason,
                "event": item.event.model_dump(mode="json"),
            }
        )
    return out


def process_job(session, job: IngestJob, pipeline: Pipeline) -> None:
    """Обрабатывает одну захваченную задачу и проставляет ей итог."""
    started = time.perf_counter()
    try:
        result = pipeline.process(
            session,
            job.text,
            source_channel=job.source_channel,
            source_message_id=job.source_message_id,
            reference_date=job.reference_date,
        )
    except Exception as exc:
        # Сюда попадают недоступная Ollama (httpx.ConnectError), таймауты и
        # ошибки БД. Все они лечатся повтором, поэтому задача не теряется.
        session.rollback()
        jobs_repo.mark_failed(
            session,
            job,
            error=f"{type(exc).__name__}: {exc}",
            max_attempts=settings.worker_max_attempts,
            backoff_s=settings.worker_retry_backoff_s,
        )
        return

    duration_ms = int((time.perf_counter() - started) * 1000)
    jobs_repo.mark_done(
        session,
        job,
        event_ids=[ev.id for ev in result.saved],
        skipped=_serialize_skipped(result),
        duration_ms=duration_ms,
    )
    logger.info(
        "Задача %s готова за %s мс: сохранено %s, отсеяно %s",
        job.id, duration_ms, len(result.saved), len(result.skipped),
    )


def run_once(pipeline: Pipeline, *, worker_id: str) -> int:
    """Один проход цикла. Возвращает число обработанных задач."""
    session = SessionLocal()
    try:
        jobs_repo.requeue_stale(
            session,
            lease_timeout_s=settings.worker_lease_timeout_s,
            max_attempts=settings.worker_max_attempts,
        )
        batch = jobs_repo.claim(
            session, worker_id=worker_id, batch_size=settings.worker_batch_size
        )
        for job in batch:
            if _shutdown.is_set():
                # Дорабатываем только то, что уже начали: остаток пачки
                # возвращаем в очередь, чтобы он не завис в processing.
                # Именно release, а не mark_failed: остановка по нашей команде
                # не должна тратить лимит попыток задачи.
                jobs_repo.release(
                    session, job, reason="Воркер остановлен до начала обработки"
                )
                continue
            process_job(session, job, pipeline)
        return len(batch)
    finally:
        session.close()


def _warmup(pipeline: Pipeline) -> None:
    """Прогрев модели до первой задачи — иначе её оплатит первое сообщение."""
    if not settings.ollama_warmup_on_startup:
        return
    try:
        load_s = ollama_status.warmup()
        logger.info("Ollama прогрета за %.1f с (%s)", load_s, settings.ollama_model)
    except Exception as exc:
        logger.warning(
            "Прогрев Ollama не удался (%s: %s). Воркер продолжит работу: "
            "задачи будут повторяться, пока модель не станет доступна.",
            type(exc).__name__, exc,
        )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    _install_signal_handlers()

    worker_id = settings.resolved_worker_id
    pipeline = Pipeline(extractor=EventExtractor(make_ollama_chat_fn()))

    logger.info(
        "Воркер %s запущен. БД: %s. Модель: %s @ %s",
        worker_id,
        settings.database_url.split("@")[-1],  # без пароля в логе
        settings.ollama_model,
        settings.ollama_base_url,
    )
    _warmup(pipeline)

    while not _shutdown.is_set():
        try:
            handled = run_once(pipeline, worker_id=worker_id)
        except Exception:
            # Сюда доходит только сбой самой машинерии очереди (например, БД
            # недоступна). Логируем и продолжаем — контейнер не должен падать
            # в рестарт-петлю из-за временной недоступности Postgres.
            logger.exception("Сбой цикла воркера; повтор через паузу")
            handled = 0
        if handled == 0:
            # Пусто — ждём. Событие вместо time.sleep, чтобы сигнал
            # останавливал воркер мгновенно, а не через интервал опроса.
            _shutdown.wait(settings.worker_poll_interval_s)

    logger.info("Воркер %s остановлен.", worker_id)


if __name__ == "__main__":
    main()
