"""
Импорт истории канала из JSON-экспорта Telegram Desktop в пайплайн.

Нужны запущенные backend (`python -m uvicorn app.main:app`) И воркер
(`python -m app.worker`): API только ставит сообщения в очередь, извлечение
выполняет воркер. Каждое сообщение проходит тот же путь, что и живой пост:
prefilter → LLM → pending.

Импорт идёт в два прохода. Сначала все сообщения ставятся в очередь — это
быстро, десятки запросов в секунду. Потом скрипт ждёт результаты, опрашивая
GET /jobs/{id}. Задачи обрабатываются по порядку, поэтому достаточно ждать
каждую по очереди: когда готова N-я, все предыдущие тоже готовы.

По умолчанию события остаются в pending — подтвердить их можно кнопками
в боте. Флаг --auto-approve сразу переводит их в approved БЕЗ push
(старые анонсы не спамят студентов; push для будущих дат тоже отключён).

Примеры:
  python -m scripts.import_history result.json
  python -m scripts.import_history result.json --auto-approve
  python -m scripts.import_history result.json --limit 200
  python -m scripts.import_history result.json --no-wait   # только поставить в очередь
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import httpx

from app.config import settings
from app.ingestion.tg_export import iter_export_messages
from scripts._api import wait_for_job

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("import")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Импорт истории канала из result.json (Telegram Desktop)"
    )
    parser.add_argument(
        "export_file", type=Path, help="result.json из Telegram Desktop"
    )
    parser.add_argument("--api", default=settings.api_base_url)
    parser.add_argument(
        "--auto-approve",
        action="store_true",
        help="сразу approved, без push-уведомлений",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="обработать только последние N сообщений",
    )
    parser.add_argument(
        "--no-wait",
        action="store_true",
        help="только поставить в очередь и выйти, не дожидаясь воркера",
    )
    args = parser.parse_args()
    if args.no_wait and args.auto_approve:
        parser.error("--auto-approve требует ожидания результатов: уберите --no-wait")

    export = json.loads(args.export_file.read_text(encoding="utf-8"))
    messages = list(iter_export_messages(export))
    if args.limit:
        messages = messages[-args.limit:]  # последние N — самые свежие
    logger.info(
        "Сообщений к обработке: %s (канал: %s)",
        len(messages),
        messages[0].channel if messages else "—",
    )

    queued = extracted = approved = skipped = errors = failed = 0
    seen_ids: set[int] = set()
    job_ids: list[int] = []

    with httpx.Client(
        base_url=args.api, timeout=60.0, headers=settings.api_headers
    ) as http:
        # Проход 1: поставить всё в очередь. Быстро — инференса здесь нет.
        for i, msg in enumerate(messages, 1):
            try:
                resp = http.post(
                    "/events/ingest",
                    json={
                        "text": msg.text,
                        "source_channel": msg.channel,
                        "source_message_id": msg.message_id,
                        "reference_date": msg.date.isoformat(),
                    },
                )
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                errors += 1
                logger.warning("msg %s: ошибка (%s)", msg.message_id, exc)
                continue
            queued += 1
            job_ids.append(resp.json()["job_id"])
            if i % 100 == 0:
                logger.info("...поставлено в чергу %s/%s", i, len(messages))

        logger.info("В очередь поставлено %s сообщений, ошибок %s.", queued, errors)
        if args.no_wait:
            logger.info(
                "Ожидание отключено (--no-wait). Прогресс: GET /jobs, GET /stats."
            )
            return

        # Проход 2: дождаться воркера. Задачи берутся по порядку, поэтому
        # ждём последовательно — когда готова N-я, предыдущие тоже готовы.
        logger.info("Жду воркера (%s задач)...", len(job_ids))
        for i, job_id in enumerate(job_ids, 1):
            job = wait_for_job(http, job_id, settings.job_poll_timeout_s)
            if job is None:
                errors += 1
                logger.warning("job %s: не дождался результата", job_id)
                continue
            if job["status"] == "failed":
                failed += 1
                logger.warning("job %s: провалена (%s)", job_id, job.get("last_error"))
                continue
            skipped += len(job.get("skipped", []))
            for event_id in job.get("event_ids", []):
                if event_id in seen_ids:
                    continue
                seen_ids.add(event_id)
                extracted += 1
                if args.auto_approve:
                    ok = http.post(
                        f"/events/{event_id}/approve", params={"notify": "false"}
                    )
                    if ok.status_code == 200:
                        approved += 1
            if i % 25 == 0:
                logger.info("...обработано %s/%s", i, len(job_ids))

    tail = (
        ""
        if args.auto_approve
        else " События в pending — подтвердите картки в боті або запустіть "
        "з --auto-approve."
    )
    logger.info(
        "Готово: в очередь %s, событий %s, отсеяно фильтрами %s, "
        "подтверждено %s, провалено задач %s, ошибок %s.%s",
        queued, extracted, skipped, approved, failed, errors, tail,
    )


if __name__ == "__main__":
    main()
