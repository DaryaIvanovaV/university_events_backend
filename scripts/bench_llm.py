"""
Сравнение конфигураций LLM: скорость И качество извлечения одновременно.

Мерить одну скорость бессмысленно — самая быстрая конфигурация та, что
ничего не находит. Поэтому каждый прогон оценивается по разметке датасета
`data/test_messages.json` (ключ `_expect`, доезжает до сообщения как `note`):

  * настоящий анонс (22 шт.) — модель ОБЯЗАНА вернуть хотя бы одно событие;
  * ловушка `ПАСТКА` (10 шт.) — модель обязана вернуть `events: []`;
    выдуманное здесь событие и есть цена ошибки;
  * `ІНДИВІДУАЛЬНА` (3 шт.) — модель их извлекает, и это нормально:
    отсекает не она, а детерминированный фильтр `app/services/audience.py`.
    Считаются отдельно, чтобы не портить обе метрики выше.

Оси сравнения — модель и размер контекстного окна. Обычно меняют одну.

БД и backend не нужны: экстрактор вызывается напрямую, как в check_prompt.

Примеры:
    python -m scripts.bench_llm --models gemma3:4b gemma3:1b
    python -m scripts.bench_llm --ctx 0 3072 --limit 10
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import statistics
import time
from pathlib import Path

import httpx

from app.config import settings
from app.ingestion.samples import load_messages
from app.llm import ollama_status
from app.llm.extractor import EventExtractor, ExtractionError, make_ollama_chat_fn
from scripts._console import force_utf8

force_utf8()

DEFAULT_DATASET = Path("data/test_messages.json")

TRAP = "ПАСТКА"
INDIVIDUAL = "ІНДИВІДУАЛЬНА"


def kind(note: str) -> str:
    """Тип сообщения по разметке датасета."""
    upper = note.strip().upper()
    if upper.startswith(TRAP):
        return "trap"
    if upper.startswith(INDIVIDUAL):
        return "individual"
    return "announcement"


def vram_share(model: str) -> tuple[float, float]:
    """(доля модели в VRAM, ГБ всего). (0, 0) — модель не загружена."""
    try:
        data = httpx.get(f"{settings.ollama_base_url}/api/ps", timeout=10).json()
    except httpx.HTTPError:
        return 0.0, 0.0
    for entry in data.get("models", []):
        if entry.get("name") == model:
            total = entry.get("size", 0)
            return (entry.get("size_vram", 0) / total if total else 0.0), total / 1e9
    return 0.0, 0.0


def unload(model: str) -> None:
    """Выгружает модель — следующий прогон честно платит загрузку."""
    try:
        httpx.post(
            f"{settings.ollama_base_url}/api/generate",
            json={"model": model, "keep_alive": 0},
            timeout=60,
        )
    except httpx.HTTPError:
        pass
    time.sleep(2)


def run_one(messages, model: str, num_ctx: int) -> dict:
    """Прогон всего датасета в одной конфигурации."""
    unload(model)
    ctx_label = str(num_ctx) if num_ctx else "дефолт"
    print(f"\n=== {model}, num_ctx={ctx_label} ===")

    warm_s = ollama_status.warmup(model=model, num_ctx=num_ctx or None)
    share, total_gb = vram_share(model)
    print(f"  прогрев {warm_s:.1f} с; в VRAM {share:.0%} из {total_gb:.2f} ГБ")

    extractor = EventExtractor(make_ollama_chat_fn(model=model, num_ctx=num_ctx))
    latencies: list[float] = []
    counts = {"announcement": 0, "trap": 0, "individual": 0}
    hits = {"announcement": 0, "trap": 0, "individual": 0}
    details: list[dict] = []
    total_events = failures = 0

    for i, msg in enumerate(messages, 1):
        group = kind(msg.note)
        counts[group] += 1
        started = time.perf_counter()
        try:
            result = extractor.extract(msg.text, msg.date)
        except ExtractionError as exc:
            failures += 1
            details.append(
                {"id": msg.message_id, "kind": group, "error": type(exc).__name__}
            )
            print(f"  [{i:2}/{len(messages)}] СБОЙ {type(exc).__name__}")
            continue
        latencies.append(time.perf_counter() - started)
        found = len(result.events)
        total_events += found

        # Ловушка засчитывается, если модель НИЧЕГО не выдумала;
        # анонс и индивидуальная консультация — если событие найдено.
        ok = (found == 0) if group == "trap" else (found > 0)
        hits[group] += int(ok)
        # Что именно модель вернула — без этого по агрегату не понять, какие
        # ловушки чинить промтом: нужны id сообщений и выдуманные заголовки.
        details.append(
            {
                "id": msg.message_id,
                "kind": group,
                "ok": ok,
                "found": found,
                "seconds": round(latencies[-1], 1),
                "titles": [e.event_title for e in result.events],
                "expect": msg.note[:120],
            }
        )
        mark = "OK " if ok else "!! "
        print(f"  [{i:2}/{len(messages)}] {mark}{latencies[-1]:5.1f} с  "
              f"{group:12} подій: {found}")

    return {
        "details": details,
        "model": model,
        "num_ctx": num_ctx,
        "warmup_s": round(warm_s, 1),
        "vram_share": round(share, 3),
        "size_gb": round(total_gb, 2),
        "median_s": round(statistics.median(latencies), 1) if latencies else None,
        "max_s": round(max(latencies), 1) if latencies else None,
        "total_s": round(sum(latencies), 1),
        "announcements_ok": hits["announcement"],
        "announcements": counts["announcement"],
        "traps_clean": hits["trap"],
        "traps": counts["trap"],
        "individual_found": hits["individual"],
        "individual": counts["individual"],
        "total_events": total_events,
        "json_failures": failures,
    }


def print_table(runs: list[dict]) -> None:
    print("\n" + "=" * 88)
    print(f"{'конфігурація':>22} {'VRAM':>6} {'медіана':>9} {'макс':>7} "
          f"{'анонси':>9} {'пастки':>9} {'збоїв':>7}")
    print("-" * 88)
    for r in runs:
        ctx = str(r["num_ctx"]) if r["num_ctx"] else "деф"
        label = f"{r['model']} ctx={ctx}"
        print(
            f"{label:>22} {r['vram_share']:>5.0%} {r['median_s']:>8} с "
            f"{r['max_s']:>5} с {r['announcements_ok']:>4}/{r['announcements']:<4} "
            f"{r['traps_clean']:>4}/{r['traps']:<4} {r['json_failures']:>7}"
        )
    print("=" * 88)
    print("«анонси» — у скількох справжніх оголошеннях знайдено подію (більше — краще).")
    print("«пастки» — скільки разів модель НЕ вигадала подію там, де її немає.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", nargs="?", type=Path, default=DEFAULT_DATASET)
    parser.add_argument(
        "--models", nargs="+", default=[settings.ollama_model],
        help="какие модели сравнить (должны быть скачаны: ollama pull ...)",
    )
    parser.add_argument(
        "--ctx", type=int, nargs="+", default=[0],
        help="значения num_ctx; 0 = дефолт Ollama",
    )
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--only", choices=["trap", "announcement", "individual"], default=None,
        help="прогнать только сообщения этого типа (быстрее при работе над промтом)",
    )
    parser.add_argument("--out", type=Path, default=Path("reports/bench_llm.json"))
    args = parser.parse_args()

    messages, warnings = load_messages(args.dataset)
    for w in warnings:
        print("warning:", w)
    if args.only:
        messages = [m for m in messages if kind(m.note) == args.only]
    if args.limit:
        messages = messages[: args.limit]
    print(f"Датасет: {args.dataset} — {len(messages)} сообщений")

    runs = [
        run_one(messages, model, ctx)
        for model in args.models
        for ctx in args.ctx
    ]
    print_table(runs)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(
            {
                "dataset": str(args.dataset),
                "measured_at": dt.datetime.now().isoformat(timespec="seconds"),
                "runs": runs,
            },
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )
    print(f"Подробности: {args.out}")


if __name__ == "__main__":
    main()
