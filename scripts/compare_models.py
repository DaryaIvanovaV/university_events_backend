"""
Порівняння моделей за готовими звітами `scripts.check_prompt`.

Кожен прогін `check_prompt` зберігає JSON із повними записами по кожному
повідомленню. Тут вони зводяться в одну таблицю, щоб порівняти моделі на
ТОМУ САМОМУ датасеті. Інференс повторно не запускається — це чистий
агрегатор, його можна ганяти скільки завгодно разів.

Метрики розділені навмисно, бо міряють різне:
  * «пастки чисто» — скільки з 10 пасток модель сама повернула `events: []`;
  * «пастки → БД» — скільки пасток пережили ще й детерміновані фільтри
    (пре-фільтр, `audience.py`, `relevance.py`) і дійшли б до модератора.
    Саме ця цифра — ціна помилок моделі для живої людини;
  * «анонси» — у скількох справжніх оголошеннях подія знайдена;
  * «анонси → БД» — скільки з них реально збереглося б. Фільтри інколи
    з'їдають і правдиві події (пере-фільтрація), і видно це тільки тут.

Запуск:
    python -m scripts.compare_models reports/model_gemma3_4b_it.json ...
    python -m scripts.compare_models reports/model_*.json --out reports/models.md
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.eval.pipeline_sim import SAVED, summarize_destiny
from app.eval.report import compute_stats
from scripts.bench_llm import kind
from scripts._console import force_utf8

force_utf8()

GROUPS = ("announcement", "trap", "individual")


def reaches_db(record: dict) -> bool:
    """Чи дійшла б із цього повідомлення хоч одна подія до черги модерації."""
    return any(d["db"] == SAVED for d in record.get("events_destiny", []))


def summarize(path: Path) -> dict:
    """Зводить один звіт check_prompt до набору чисел."""
    data = json.loads(path.read_text(encoding="utf-8"))
    meta, records = data["meta"], data["records"]
    stats = compute_stats(records)
    destiny = summarize_destiny(records)

    counts = dict.fromkeys(GROUPS, 0)
    found = dict.fromkeys(GROUPS, 0)  # модель повернула хоч одну подію
    to_db = dict.fromkeys(GROUPS, 0)  # подія пережила ще й фільтри
    # Пропуск поля і порожня відповідь — різні речі, і зводити їх в одне
    # число не можна: `events: []` на пастці grounding теж помічає як
    # пропуск, хоча це САМЕ ТЕ, чого ми домагаємося промтом.
    missed_fields = 0
    for record in records:
        group = kind(record.get("expect", ""))
        counts[group] += 1
        if record["events"]:
            found[group] += 1
        if reaches_db(record):
            to_db[group] += 1
        for chunk in record["event_checks"]:
            missed_fields += sum(1 for c in chunk if c["status"] == "missed")
        # На рівні повідомлення grounding теж помічає пропуски (час і
        # посилання, яких немає в жодній події) — вони справжні. А ось
        # `events` тут означає «модель не знайшла події взагалі», і для
        # пастки це правильна відповідь, тому цей ключ пропускаємо.
        missed_fields += sum(
            1 for c in record["message_checks"]
            if c["status"] == "missed" and c["field"] != "events"
        )

    return {
        "path": path,
        "model": meta["model"],
        # Прогін без схеми не порівнюваний з рештою напряму: JSON там тримає
        # не граматика, а лише Pydantic. Позначаємо зірочкою в таблиці.
        "use_schema": meta.get("use_schema", True),
        # Так само з вимкненими міркуваннями: у продовому режимі модель
        # думає, і це коштує сотні токенів до першого символу відповіді.
        "think_off": meta.get("think") is False,
        "dataset": meta["dataset"],
        "messages": stats["total"],
        "duration_s": meta.get("duration_s") or 0.0,
        "warmup_s": meta.get("warmup_s") or 0.0,
        "median_s": stats["median_s"],
        "max_s": stats["max_s"],
        "first_try": stats["first_try"],
        "errors": stats["errors"],
        "events": stats["events"],
        "invented": stats["invented"],
        "suspect": stats["suspect"],
        "missed_fields": missed_fields,
        "counts": counts,
        # пастка «чиста», якщо модель НЕ вигадала на ній подію
        "traps_clean": counts["trap"] - found["trap"],
        "traps_to_db": to_db["trap"],
        "ann_found": found["announcement"],
        "ann_to_db": to_db["announcement"],
        "individual_found": found["individual"],
        "individual_to_db": to_db["individual"],
        "saved": destiny[SAVED],
        "records": records,
    }


def table(rows: list[dict]) -> list[str]:
    """Головна таблиця порівняння (Markdown)."""
    head = (
        "| модель | медіана | макс | JSON 1-ша спроба | збоїв | подій | "
        "анонси | анонси → БД | пастки чисто | пастки → БД | інд. | "
        "вигаданих полів | пропущених полів |"
    )
    lines = [head, "|" + "---|" * 13]
    for r in rows:
        c = r["counts"]
        star = ("" if r["use_schema"] else " *") + (" †" if r["think_off"] else "")
        lines.append(
            f"| `{r['model']}`{star} | {r['median_s']:.1f} с | {r['max_s']:.1f} с | "
            f"{r['first_try']}/{r['messages']} | {r['errors']} | {r['events']} | "
            f"{r['ann_found']}/{c['announcement']} | {r['ann_to_db']}/{c['announcement']} | "
            f"{r['traps_clean']}/{c['trap']} | **{r['traps_to_db']}**/{c['trap']} | "
            f"{r['individual_to_db']}/{c['individual']} | {r['invented']} | "
            f"{r['missed_fields']} |"
        )
    if any(not r["use_schema"] for r in rows):
        lines += [
            "",
            "\\* прогін у режимі `format=\"json\"` — БЕЗ constrained decoding за нашою",
            "схемою. Числа не порівнюються з рештою рядків напряму: у продовому",
            "режимі ця модель узагалі не працює (див. `--no-schema` в check_prompt).",
        ]
    if any(r["think_off"] for r in rows):
        lines += [
            "",
            "† прогін із `think=false`. За замовчуванням модель спершу «міркує», і",
            "на цьому залізі це десятки секунд до першого символу відповіді.",
        ]
    return lines


def per_message(rows: list[dict]) -> list[str]:
    """Таблиця «повідомлення × модель»: скільки подій і чи доїде до БД."""
    by_id: dict[int, dict] = {}
    for r in rows:
        for record in r["records"]:
            slot = by_id.setdefault(
                record["message_id"],
                {"kind": kind(record.get("expect", "")),
                 "expect": record.get("expect", ""), "cells": {}},
            )
            mark = "—" if not record["events"] else str(len(record["events"]))
            if record["error"]:
                mark = "збій"
            elif record["events"]:
                mark += " → БД" if reaches_db(record) else " (відс.)"
            slot["cells"][r["model"]] = mark

    names = [r["model"] for r in rows]
    lines = [
        "| id | тип | " + " | ".join(f"`{n}`" for n in names) + " | очікується |",
        "|" + "---|" * (len(names) + 3),
    ]
    label = {"trap": "ПАСТКА", "individual": "індивід.", "announcement": "анонс"}
    for msg_id in sorted(by_id):
        slot = by_id[msg_id]
        cells = " | ".join(slot["cells"].get(n, "—") for n in names)
        note = slot["expect"].replace("|", "/")[:70]
        lines.append(f"| {msg_id} | {label[slot['kind']]} | {cells} | {note} |")
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", nargs="+", type=Path,
                        help="JSON-файли прогонів scripts.check_prompt")
    parser.add_argument("--out", type=Path, default=Path("reports/model_comparison.md"))
    args = parser.parse_args()

    rows = [summarize(p) for p in args.reports]

    datasets = {r["dataset"] for r in rows}
    if len(datasets) > 1:
        print(f"! УВАГА: прогони на різних датасетах {datasets} — числа не порівнянні")

    lines = [
        "# Порівняння моделей на датасеті анонсів",
        "",
        f"Датасет: `{'`, `'.join(sorted(datasets))}` — "
        f"{rows[0]['messages']} повідомлень "
        f"({rows[0]['counts']['announcement']} анонсів, "
        f"{rows[0]['counts']['trap']} пасток, "
        f"{rows[0]['counts']['individual']} індивідуальних консультацій).",
        "",
        "«→ БД» — скільки повідомлень дали б запис у черзі модерації після",
        "детермінованих фільтрів. Для анонсів більше — краще, для пасток — гірше.",
        "«Пропущених полів» рахує ТІЛЬКИ порожні поля всередині видобутих подій:",
        "порожня відповідь `events: []` на пастці сюди не потрапляє.",
        "",
        *table(rows),
        "",
        "## Час прогону",
        "",
        "| модель | прогрів | увесь датасет |",
        "|---|---|---|",
        *[f"| `{r['model']}` | {r['warmup_s']:.1f} с | {r['duration_s'] / 60:.1f} хв |"
          for r in rows],
        "",
        "## Кожне повідомлення окремо",
        "",
        "Число — скільки подій видобуто; «→ БД» — доїде до модерації, "
        "«(відс.)» — з'їли фільтри.",
        "",
        *per_message(rows),
        "",
        "## Звіти для перегляду очима",
        "",
        *[f"- `{r['model']}` — `{r['path'].with_suffix('.html')}`" for r in rows],
    ]

    text = "\n".join(lines)
    print(text)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text + "\n", encoding="utf-8")
    print(f"\nЗбережено: {args.out}")


if __name__ == "__main__":
    main()
