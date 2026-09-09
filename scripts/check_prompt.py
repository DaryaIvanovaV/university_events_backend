"""
Проверка промта на пачке сообщений: не выдумывает ли модель данные.

Гоняет каждое сообщение через НАСТОЯЩИЙ экстрактор (Ollama + constrained
decoding + Pydantic), сверяет каждое извлечённое поле с исходным текстом и
собирает HTML-отчёт для просмотра глазами.

Отличия от соседних скриптов:
  * demo_extract — одно захардкоженное сообщение (к тому же из few-shot);
  * simulate     — либо только пре-фильтр (--dry-run), либо через HTTP и с
                   поднятым backend + БД, и печатает лишь три поля.
Здесь: пачка сообщений, БЕЗ backend и БЕЗ БД, с полным JSON, сырым ответом
модели, таймингами и автоматической разметкой подозрительных значений.

Запуск:
    python -m scripts.check_prompt                        # data/sample_messages.jsonl
    python -m scripts.check_prompt my_messages.json --open
    python -m scripts.check_prompt --model gemma3:1b --limit 10

Формат входного файла определяется автоматически: JSONL, JSON-массив или
экспорт Telegram Desktop. Обязательное поле одно — "text"; "date" нужна для
разрешения относительных дат («завтра»), "source_message_id" и
"source_channel" опциональны.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
import webbrowser
from pathlib import Path

import httpx

from app.config import settings
from app.eval import grounding, report
from app.eval.pipeline_sim import PipelineSimulator, summarize_destiny
from app.ingestion.samples import load_messages
from app.llm import ollama_status
from app.llm.extractor import EventExtractor, ExtractionError, make_ollama_chat_fn
from app.llm.prompts import FEWSHOT, SYSTEM_PROMPT, build_user_message
from app.services.meeting import normalize_meeting
from app.services.schedule import normalize_schedule
from app.services.prefilter import prefilter
from scripts._console import Heartbeat, force_utf8

DEFAULT_FILE = Path("data/sample_messages.jsonl")
REPORTS_DIR = Path("reports")


class RecordingChat:
    """
    Обёртка над chat_fn: пропускает вызов дальше и запоминает сырой ответ.

    Экстрактор отдаёт только провалидированный EventList — сырая строка,
    число попыток и latency теряются, а для проверки промта нужны именно они.
    chat_fn — единственный обязательный аргумент EventExtractor, поэтому
    перехват тут не требует правок в app/llm/extractor.py.
    """

    def __init__(self, inner) -> None:
        self.inner = inner
        self.responses: list[str] = []
        self.elapsed = 0.0

    def reset(self) -> None:
        self.responses = []
        self.elapsed = 0.0

    def __call__(self, messages: list[dict], schema: dict) -> str:
        start = time.perf_counter()
        try:
            content = self.inner(messages, schema)
        finally:
            self.elapsed += time.perf_counter() - start
        self.responses.append(content)
        return content


def preflight(model: str, base_url: str, *, warmup: bool) -> float | None:
    """Проверяет сервер и модель, при необходимости греет. None — если нельзя."""
    print(f"[1/3] Сервер Ollama на {base_url} …", flush=True)
    try:
        version = ollama_status.check_server(base_url)
    except Exception as exc:
        print(f"  ✗ Сервер недоступен ({type(exc).__name__}).")
        print("    Запустите приложение Ollama (значок в трее) или проверьте:")
        print(f"      Invoke-RestMethod {base_url}/api/version")
        return None
    print(f"  ✓ Работает (версия {version}).")

    print(f"[2/3] Модель {model} …", flush=True)
    try:
        have = ollama_status.model_available(model, base_url)
    except Exception as exc:
        print(f"  ✗ Не удалось получить список моделей ({type(exc).__name__}).")
        return None
    if not have:
        print(f"  ✗ Модель не скачана. Выполните:  ollama pull {model}")
        return None
    print("  ✓ Доступна.")

    if not warmup:
        print("[3/3] Прогрев пропущен (--no-warmup).")
        return None
    print("[3/3] Загружаю модель в память (первый раз дольше — не прерывайте) …",
          flush=True)
    try:
        with Heartbeat(interval=20.0, prefix="  "):
            load_s = ollama_status.warmup(model, base_url)
    except Exception as exc:
        print(f"  ✗ Прогрев не удался ({type(exc).__name__}: {exc}).")
        return None
    print(f"  ✓ Загружена за {load_s:.1f} с.")
    return load_s


def process_message(
    extractor: EventExtractor,
    recorder: RecordingChat,
    message,
    index: int,
    *,
    respect_prefilter: bool,
    simulator: PipelineSimulator | None = None,
) -> dict:
    """Прогоняет одно сообщение и собирает запись для отчёта."""
    pf = prefilter(message.text)
    simulator = simulator or PipelineSimulator()
    record: dict = {
        "index": index,
        "message_id": message.message_id,
        "channel": message.channel,
        "reference_date": message.date.isoformat(),
        "text": message.text,
        "expect": message.note,
        "prefilter": {
            "is_candidate": pf.is_candidate,
            "score": pf.score,
            "matched": list(pf.matched),
        },
        "attempts": 0,
        "raw_responses": [],
        "valid_json": False,
        "error": None,
        "latency_s": 0.0,
        "events": [],
        "events_destiny": [],
        "event_checks": [],
        "message_checks": [],
        "worst": grounding.INFO,
    }

    if respect_prefilter and not pf.is_candidate:
        record["message_checks"] = [
            grounding.FieldCheck(
                "events", [], grounding.INFO,
                "отсеяно пре-фильтром (--respect-prefilter) — LLM не вызывалась",
            ).to_dict()
        ]
        return record

    recorder.reset()
    events = []
    try:
        with Heartbeat(interval=15.0, prefix="    "):
            events = list(extractor.extract(message.text, message.date).events)
        record["valid_json"] = True
    except ExtractionError as exc:
        record["error"] = f"невалидный JSON после всех попыток: {exc}"
    except httpx.HTTPError as exc:
        record["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        record["attempts"] = len(recorder.responses)
        record["raw_responses"] = list(recorder.responses)
        record["latency_s"] = recorder.elapsed

    event_checks = [
        grounding.check_event(ev, message.text, message.date) for ev in events
    ]
    message_checks = grounding.check_omissions(
        events, message.text, prefilter_candidate=pf.is_candidate
    )
    flat = [c for group in event_checks for c in group] + message_checks

    # Пайплайн раскладывает ссылку/код встречи по полям до сохранения —
    # отчёт должен показывать то же, что реально доедет до БД.
    events = [
        normalize_schedule(normalize_meeting(ev, message.text), message.text, message.date)
        for ev in events
    ]

    record["events"] = [ev.model_dump(mode="json") for ev in events]
    # Что стало бы с каждым событием в настоящем пайплайне: БД и фид.
    record["events_destiny"] = [
        simulator.decide(
            ev, index=index,
            prefilter_candidate=pf.is_candidate,
            reference_date=message.date,
            source_text=message.text,
        ).to_dict()
        for ev in events
    ]
    record["event_checks"] = [[c.to_dict() for c in group] for group in event_checks]
    record["message_checks"] = [c.to_dict() for c in message_checks]
    record["worst"] = (
        grounding.INVALID if record["error"] else grounding.worst_status(flat)
    )
    return record


def describe(record: dict) -> str:
    """Короткая строка итога сообщения для консоли."""
    if record["error"]:
        return "✗ ошибка извлечения"
    counts: dict[str, int] = {}
    for group in record["event_checks"] + [record["message_checks"]]:
        for check in group:
            counts[check["status"]] = counts.get(check["status"], 0) + 1
    bits = [f'{len(record["events"])} соб.']
    if counts.get(grounding.INVENTED) or counts.get(grounding.INVALID):
        bits.append(
            f'⚠ выдумок: {counts.get(grounding.INVENTED, 0) + counts.get(grounding.INVALID, 0)}'
        )
    if counts.get(grounding.SUSPECT):
        bits.append(f'подозр.: {counts[grounding.SUSPECT]}')
    if counts.get(grounding.MISSED):
        bits.append(f'пропусков: {counts[grounding.MISSED]}')
    return ", ".join(bits)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.check_prompt",
        description="Прогон промта по пачке сообщений с проверкой на выдумки.",
    )
    parser.add_argument("file", type=Path, nargs="?", default=DEFAULT_FILE,
                        help=f"JSONL / JSON-массив / экспорт Telegram (по умолчанию {DEFAULT_FILE})")
    parser.add_argument("--limit", type=int, default=None, help="взять только первые N сообщений")
    parser.add_argument("--model", default=None, help=f"модель Ollama (по умолчанию {settings.ollama_model})")
    parser.add_argument("--temperature", type=float, default=None, help="температура генерации")
    parser.add_argument("--no-schema", action="store_true",
                        help="слати format=\"json\" замість схеми (лише для моделей, "
                             "які зриваються під граматикою — див. extractor)")
    parser.add_argument("--no-think", action="store_true",
                        help="вимкнути блок міркувань (лише для моделей із "
                             "capability thinking, напр. qwen3:8b)")
    parser.add_argument("--respect-prefilter", action="store_true",
                        help="не звать LLM на сообщениях, которые отсёк бы пре-фильтр (как в проде)")
    parser.add_argument("--out", type=Path, default=None, help="путь к HTML-отчёту")
    parser.add_argument("--open", action="store_true", help="открыть отчёт в браузере")
    parser.add_argument("--no-warmup", action="store_true", help="пропустить прогрев модели")
    parser.add_argument("--sort", choices=("source", "flags"), default="source",
                        help="порядок карточек: как в файле или худшие сверху")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    force_utf8()
    args = parse_args(argv)

    if not args.file.exists():
        print(f"Файл не найден: {args.file}")
        return 1

    messages, warnings = load_messages(args.file)
    if args.limit:
        messages = messages[: args.limit]
    if not messages:
        print(f"В файле {args.file} нет сообщений.")
        for warning in warnings:
            print("  !", warning)
        return 1

    model = args.model or settings.ollama_model
    base_url = settings.ollama_base_url
    temperature = settings.ollama_temperature if args.temperature is None else args.temperature

    print("=" * 72)
    print(f"Проверка промта: {len(messages)} сообщений из {args.file}")
    print(f"Модель: {model}  temperature={temperature}  retries={settings.llm_max_retries}"
          f"{'  format=json (БЕЗ схеми!)' if args.no_schema else ''}"
          f"{'  think=false' if args.no_think else ''}")
    print("=" * 72)
    for warning in warnings:
        print("  !", warning)
    print()

    warmup_s = preflight(model, base_url, warmup=not args.no_warmup)
    if warmup_s is None and not args.no_warmup:
        return 1

    recorder = RecordingChat(
        make_ollama_chat_fn(
            base_url=base_url, model=model, temperature=temperature,
            use_schema=not args.no_schema,
            think=False if args.no_think else None,
        )
    )
    extractor = EventExtractor(recorder)

    started = dt.datetime.now()
    run_id = started.strftime("%Y%m%d_%H%M%S")
    run_start = time.perf_counter()
    records: list[dict] = []

    # Один симулятор на прогон: дедупликация в настоящем пайплайне сквозная,
    # тот же анонс из другого сообщения отсекается по content_hash.
    simulator = PipelineSimulator()

    print(f"\nПрогон ({len(messages)} сообщений):")
    interrupted = False
    for i, message in enumerate(messages, 1):
        head = message.text[:52].replace("\n", " ")
        print(f"  [{i}/{len(messages)}] id={message.message_id} «{head}…»", flush=True)
        try:
            record = process_message(
                extractor, recorder, message, i,
                respect_prefilter=args.respect_prefilter,
                simulator=simulator,
            )
        except KeyboardInterrupt:
            print("\n  Прервано пользователем — сохраняю отчёт по уже обработанным.")
            interrupted = True
            break
        except httpx.HTTPError as exc:
            print(f"\n  ✗ Связь с Ollama потеряна: {type(exc).__name__}: {exc}")
            print("    Сохраняю отчёт по уже обработанным сообщениям.")
            interrupted = True
            break
        records.append(record)
        print(f"        {record['latency_s']:.1f} с — {describe(record)}")

    if not records:
        print("Ни одного сообщения обработать не удалось.")
        return 1

    duration = time.perf_counter() - run_start

    meta = {
        "run_id": run_id,
        "model": model,
        "temperature": temperature,
        "max_retries": settings.llm_max_retries,
        "base_url": base_url,
        "dataset": str(args.file),
        "started_at": started.strftime("%Y-%m-%d %H:%M:%S"),
        "duration_s": duration,
        "warmup_s": warmup_s,
        "respect_prefilter": args.respect_prefilter,
        "use_schema": not args.no_schema,
        "think": False if args.no_think else None,
        "interrupted": interrupted,
        "warnings": warnings,
        "prompt": {
            "system": SYSTEM_PROMPT,
            "fewshot": FEWSHOT,
            "user_example": build_user_message(messages[0].text, messages[0].date)["content"],
        },
    }

    ordered = records
    if args.sort == "flags":
        rank = {
            grounding.INVENTED: 0, grounding.INVALID: 1, grounding.MISSED: 2,
            grounding.SUSPECT: 3, grounding.OK: 4, grounding.INFO: 5, grounding.EMPTY: 6,
        }
        ordered = sorted(records, key=lambda r: (rank.get(r["worst"], 9), r["index"]))

    html_path = args.out or REPORTS_DIR / f"prompt_check_{run_id}.html"
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(report.render(meta, ordered), encoding="utf-8")

    json_path = html_path.with_suffix(".json")
    json_path.write_text(
        json.dumps({"meta": meta, "records": records}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    review_path = html_path.with_suffix(".review.jsonl")
    review_path.write_text(
        "".join(
            json.dumps(
                {
                    "run_id": run_id,
                    "index": r["index"],
                    "message_id": r["message_id"],
                    "auto_worst": r["worst"],
                    "verdict": None,
                    "bad_fields": [],
                    "comment": "",
                    "expect": r.get("expect", ""),
                    "text": r["text"][:160],
                },
                ensure_ascii=False,
            )
            + "\n"
            for r in records
        ),
        encoding="utf-8",
    )

    stats = report.compute_stats(records)
    print("\n" + "=" * 72)
    print(f"Обработано: {stats['total']}, событий: {stats['events']}")
    print(f"Валидный JSON с 1-й попытки: {stats['first_try']}/{stats['total']} "
          f"({stats['first_try_pct']:.0f}%), ошибок: {stats['errors']}")
    print(f"Выдумок: {stats['invented']}, подозрений: {stats['suspect']}, "
          f"пропусков: {stats['missed']}")
    dest = summarize_destiny(records)
    print(f"Дошло бы до БД (pending): {dest['saved']}")
    print(f"  отсеяно до БД: пре-фильтром {dest['filtered']}, "
          f"не по-украински {dest['language']}, уже прошедших {dest['stale']}, "
          f"индивидуальных консультаций {dest['individual']}, "
          f"дублей {dest['duplicate']}")
    print(f"Из них показалось бы в фиде после approve: {dest['feed']}")
    print(f"Latency: медиана {stats['median_s']:.1f} с, максимум {stats['max_s']:.1f} с")
    print("=" * 72)
    print(f"Отчёт:    {html_path}")
    print(f"Данные:   {json_path}")
    print(f"Вердикты: {review_path}  (или кнопкой в отчёте)")
    print("\nДальше: откройте отчёт, проставьте вердикты, нажмите "
          "«Зберегти verdicts.jsonl», затем:")
    print(f"  python -m scripts.review_stats <verdicts.jsonl> --run {json_path}")
    print("Продолжить разметку на другой машине: кнопка «Завантажити вердикти "
          "з файлу» вносит сохранённый verdicts.jsonl обратно в отчёт.")

    if args.open:
        webbrowser.open(html_path.resolve().as_uri())

    return 0


if __name__ == "__main__":
    sys.exit(main())
