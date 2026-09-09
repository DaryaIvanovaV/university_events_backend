"""
Моделирование данных: прогон набора примеров через систему.

Три режима:

  # 1) Быстрая проверка датасета БЕЗ сервера и Ollama — только пре-фильтр:
  python -m scripts.simulate --dry-run

  # 2) Полный прогон через пайплайн (нужен запущенный backend + Ollama).
  #    События попадают в очередь модерации (pending):
  python -m scripts.simulate

  # 3) То же, но сразу approved (для демо — наполнить фид без модерации).
  #    Push для будущих дат при этом НЕ шлётся (notify=false):
  python -m scripts.simulate --auto-approve

По умолчанию берётся data/sample_messages.jsonl; можно указать свой файл.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import httpx

from app.config import settings
from app.ingestion.samples import load_messages
from app.services.prefilter import prefilter
from scripts._api import wait_for_job

# Консоль Windows по умолчанию cp1252 и падает на кириллице в логах.
# Принудительно переводим вывод в UTF-8 (на Linux/macOS — no-op).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("simulate")

DEFAULT_FILE = Path("data/sample_messages.jsonl")


def run_dry(messages) -> None:
    """Только пре-фильтр: показать, что пройдёт к LLM, а что отсеется."""
    passed = 0
    for msg in messages:
        pf = prefilter(msg.text)
        mark = "✅ PASS" if pf.is_candidate else "⛔ SKIP"
        if pf.is_candidate:
            passed += 1
        preview = msg.text[:60] + ("…" if len(msg.text) > 60 else "")
        logger.info("%s  score=%.1f  %s", mark, pf.score, preview)
    logger.info("\nПройдёт к LLM: %s из %s сообщений", passed, len(messages))


def run_full(messages, api: str, auto_approve: bool) -> None:
    """
    Полный прогон через backend (очередь → воркер → БД → [approve]).

    Нужен запущенный воркер (`python -m app.worker`): API только принимает
    сообщения в очередь. Скрипт ставит задачу и дожидается её результата
    по GET /jobs/{id}, чтобы вывод остался последовательным и читаемым.
    """
    sent = extracted = approved = skipped = errors = 0
    logger.info(
        "Прогоняю через backend. Первое сообщение прогревает модель — "
        "первый ответ может занять минуты (cold start), это нормально.\n"
        "Обработку ведёт воркер: если он не запущен, задачи так и останутся "
        "в очереди (python -m app.worker).\n"
    )
    with httpx.Client(
        base_url=api, timeout=60.0, headers=settings.api_headers
    ) as http:
        for msg in messages:
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
            sent += 1

            job = wait_for_job(http, resp.json()["job_id"], settings.job_poll_timeout_s)
            if job is None:
                errors += 1
                logger.warning("msg %s: воркер не ответил вовремя", msg.message_id)
                continue
            if job["status"] == "failed":
                errors += 1
                logger.warning("msg %s: %s", msg.message_id, job.get("last_error"))
                continue

            for item in job.get("skipped", []):
                skipped += 1
                logger.info("  ✗ %s — %s", item["event_title"], item["reason"])
            for event_id in job.get("event_ids", []):
                ev = http.get(f"/events/{event_id}")
                if ev.status_code != 200:
                    continue
                event = ev.json()
                extracted += 1
                logger.info(
                    "  → [%s] %s (%s)",
                    event["status"], event["event_title"], event.get("date"),
                )
                if auto_approve and event["status"] == "pending":
                    ok = http.post(
                        f"/events/{event_id}/approve", params={"notify": "false"}
                    )
                    if ok.status_code == 200:
                        approved += 1
    logger.info(
        "\nГотово: обработано %s, событий %s, отсеяно фильтрами %s, "
        "подтверждено %s, ошибок %s",
        sent, extracted, skipped, approved, errors,
    )
    if not auto_approve:
        logger.info("События в pending — подтвердите их в боті або з --auto-approve.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Моделирование данных")
    parser.add_argument("file", type=Path, nargs="?", default=DEFAULT_FILE)
    parser.add_argument("--api", default=settings.api_base_url)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="только пре-фильтр локально (без сервера и Ollama)",
    )
    parser.add_argument(
        "--auto-approve", action="store_true",
        help="сразу approved (без push); наполнить фид для демо",
    )
    args = parser.parse_args()

    # load_messages сам определяет формат: JSONL, JSON-массив или экспорт
    # Telegram Desktop — чтобы simulate и check_prompt ели одни и те же файлы.
    messages, warnings = load_messages(args.file)
    for warning in warnings:
        logger.warning("  ! %s", warning)
    logger.info("Загружено сообщений: %s (%s)\n", len(messages), args.file)

    if args.dry_run:
        run_dry(messages)
    else:
        run_full(messages, args.api, args.auto_approve)


if __name__ == "__main__":
    main()
