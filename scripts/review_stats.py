"""
Сводка по проверке промта: превращает проставленные вердикты в таблицу цифр.

Вход — verdicts.jsonl (кнопка «Зберегти verdicts.jsonl» в HTML-отчёте либо
заготовка prompt_check_*.review.jsonl, заполненная вручную). Тот же файл
кнопка «Завантажити вердикти з файлу» вносит в отчёт обратно. Опционально —
машинный дамп того же прогона (prompt_check_*.json): из него берутся latency,
число попыток и разбивка по языкам.

Вывод — готовая Markdown-таблица для вставки в статью.

Запуск:
    python -m scripts.review_stats reports\\verdicts_20260819_143012.jsonl
    python -m scripts.review_stats reports\\verdicts.jsonl --run reports\\prompt_check_20260819_143012.json
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter
from pathlib import Path

from app.eval.grounding import FIELD_ORDER, INVALID, INVENTED, MISSED, SUSPECT  # noqa: F401
from app.eval.report import FIELD_LABEL
from scripts._console import force_utf8

VERDICT_LABEL = {
    "ok": "все верно",
    "hallucination": "есть выдумка",
    "miss": "есть пропуск",
    "both": "выдумка + пропуск",
}


def load_jsonl(path: Path) -> list[dict]:
    out = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(json.loads(line))
    return out


def pct(part: int, whole: int) -> str:
    return f"{100.0 * part / whole:.1f}%" if whole else "—"


def table(rows: list[tuple[str, str]], header: tuple[str, str]) -> str:
    """Markdown-таблица из пар (показатель, значение)."""
    width = max([len(header[0])] + [len(r[0]) for r in rows])
    lines = [
        f"| {header[0].ljust(width)} | {header[1]} |",
        f"|{'-' * (width + 2)}|---|",
    ]
    lines += [f"| {name.ljust(width)} | {value} |" for name, value in rows]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    force_utf8()
    parser = argparse.ArgumentParser(
        prog="python -m scripts.review_stats",
        description="Метрики по проставленным вердиктам проверки промта.",
    )
    parser.add_argument("verdicts", type=Path, help="verdicts*.jsonl из HTML-отчёта")
    parser.add_argument("--run", type=Path, default=None,
                        help="машинный дамп прогона prompt_check_*.json (latency, языки)")
    parser.add_argument("--out", type=Path, default=None, help="сохранить Markdown в файл")
    args = parser.parse_args(argv)

    if not args.verdicts.exists():
        print(f"Файл не найден: {args.verdicts}")
        return 1

    verdicts = load_jsonl(args.verdicts)
    if not verdicts:
        print("В файле нет строк.")
        return 1

    run = None
    if args.run:
        if not args.run.exists():
            print(f"Файл прогона не найден: {args.run}")
            return 1
        run = json.loads(args.run.read_text(encoding="utf-8"))

    total = len(verdicts)
    # Размеченным считаем сообщение, у которого оценено хотя бы одно событие
    # либо проставлена хоть одна ручная пометка поля.
    graded = [
        v for v in verdicts
        if any(e.get("send") for e in (v.get("events") or []))
        or v.get("field_marks")
        or v.get("verdict")
    ]
    counts = Counter(v["verdict"] for v in graded if v.get("verdict"))
    n = len(graded)

    clean = counts.get("ok", 0)
    with_hallucination = counts.get("hallucination", 0) + counts.get("both", 0)
    with_miss = counts.get("miss", 0) + counts.get("both", 0)

    print(f"\nПроверка промта — сводка")
    print(f"Вердикты: {args.verdicts}")
    if args.run:
        print(f"Прогон:   {args.run}")
    if n < total:
        print(f"\n!  Вердикт проставлен у {n} из {total} сообщений — "
              f"проценты считаются по {n}.")
    if n == 0:
        print("\nНи одного вердикта не проставлено. Откройте HTML-отчёт, "
              "отметьте сообщения и выгрузите файл заново.")
        return 1

    rows: list[tuple[str, str]] = [
        ("Сообщений размечено", f"{n}"),
        ("Полностью верных", f"{clean} ({pct(clean, n)})"),
        ("С выдумкой (галлюцинация)", f"{with_hallucination} ({pct(with_hallucination, n)})"),
        ("С пропуском данных", f"{with_miss} ({pct(with_miss, n)})"),
    ]

    # Разбивка полей по четырём категориям.
    fields = Counter()
    for v in graded:
        for key, value in (v.get("field_counts") or {}).items():
            fields[key] += value
    total_fields = sum(fields.values())
    if total_fields:
        rows += [
            ("— Полей оценено", f"{total_fields}"),
            ("Правильных (из текста)",
             f"{fields['correct']} ({pct(fields['correct'], total_fields)})"),
            ("Выдуманных, но верных",
             f"{fields['invented_ok']} ({pct(fields['invented_ok'], total_fields)})"),
            ("Выдуманных и неверных",
             f"{fields['invented_bad']} ({pct(fields['invented_bad'], total_fields)})"),
            ("Пропущенных",
             f"{fields['missed']} ({pct(fields['missed'], total_fields)})"),
        ]

    # Главная практическая цифра: сколько мусора дойдёт до модератора.
    events = [e for v in graded for e in (v.get("events") or [])]
    rated = [e for e in events if e.get("send")]
    to_db = [e for e in events if e.get("in_db")]
    rejected = [e for e in to_db if e.get("send") == "no"]
    approved = [e for e in to_db if e.get("send") == "yes"]
    if events:
        rows += [
            ("— Событий оценено", f"{len(rated)} из {len(events)}"),
            ("Система отправит в БД", f"{len(to_db)}"),
            ("…вы одобрили", f"{len(approved)} ({pct(len(approved), len(to_db))})"),
            ("…вы отклонили (мусор)",
             f"{len(rejected)} ({pct(len(rejected), len(to_db))})"),
        ]

    # Насколько эвристике можно верить: сколько её флагов пришлось снять руками
    # и сколько выдумок она не увидела вовсе.
    cleared = sum(v.get("cleared_flags", 0) for v in graded)
    added = sum(
        1
        for v in graded
        for mark in (v.get("field_marks") or {}).values()
        if mark == "invented_bad"
    )
    if cleared or added:
        rows += [
            ("Авто-флагов снято вручную", f"{cleared} (ложные тревоги эвристики)"),
            ("Ошибок отмечено вручную", f"{added} (полей)"),
        ]

    # --- Ошибки по полям ---
    bad_fields = Counter()
    for v in graded:
        for field in v.get("bad_fields") or []:
            bad_fields[field] += 1

    # --- Данные из машинного дампа ---
    field_rows: list[tuple[str, str]] = []
    if run:
        records = {r["index"]: r for r in run["records"]}
        graded_records = [records[v["index"]] for v in graded if v["index"] in records]

        events_total = sum(len(r["events"]) for r in graded_records)
        first_try = sum(1 for r in graded_records if r["valid_json"] and r["attempts"] == 1)
        after_retry = sum(1 for r in graded_records if r["valid_json"])
        latencies = [r["latency_s"] for r in graded_records if r["latency_s"]]

        rows += [
            ("Событий извлечено", f"{events_total}"),
            ("Валидный JSON с 1-й попытки", f"{first_try} ({pct(first_try, len(graded_records))})"),
            ("Валидный JSON после retry", f"{after_retry} ({pct(after_retry, len(graded_records))})"),
        ]
        if latencies:
            ordered = sorted(latencies)
            p95 = ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))]
            rows += [
                ("Latency, медиана", f"{statistics.median(latencies):.1f} с"),
                ("Latency, p95", f"{p95:.1f} с"),
                ("Latency, максимум", f"{max(latencies):.1f} с"),
            ]
        rows.append(("Модель", run["meta"]["model"]))

        # Разбивка по языку сообщения.
        langs = Counter()
        for record in graded_records:
            for event in record["events"]:
                langs[event.get("language") or "—"] += 1
        if langs:
            rows.append(
                ("Языки событий", ", ".join(f"{k}: {v}" for k, v in langs.most_common()))
            )

        # Насколько эвристике можно верить без человека. Разделяем два уровня:
        # жёсткий «выдумка» и мягкий «есть на что посмотреть» (вкл. подозрения),
        # иначе цифры врут: подозрение — это тоже пойманный случай.
        hard = {v["index"] for v in graded if v.get("auto_worst") in (INVENTED, INVALID)}
        any_flag = {v["index"] for v in graded
                    if v.get("auto_worst") in (INVENTED, INVALID, SUSPECT, MISSED)}
        human_bad = {v["index"] for v in graded if v["verdict"] in ("hallucination", "both")}
        rows += [
            ("Авто-флаг «выдумка» (жёсткий)",
             f"{len(hard)}, из них подтверждено: {len(hard & human_bad)}"),
            ("Авто-флаг любого уровня",
             f"{len(any_flag)}, из них подтверждено: {len(any_flag & human_bad)}"),
            ("Выдумок, не замеченных вообще",
             f"{len(human_bad - any_flag)} из {len(human_bad)} "
             f"({pct(len(human_bad - any_flag), len(human_bad))})"),
            ("Ложных тревог авто-флага",
             f"{len(any_flag - human_bad)} из {len(any_flag)} "
             f"({pct(len(any_flag - human_bad), len(any_flag))})"),
        ]

        # Автоматические статусы по полям — независимо от ручной разметки.
        auto_field = Counter()
        for record in graded_records:
            for group in record["event_checks"]:
                for check in group:
                    if check["status"] in (INVENTED, INVALID, SUSPECT, MISSED):
                        auto_field[check["field"]] += 1
        for field in FIELD_ORDER:
            if bad_fields.get(field) or auto_field.get(field):
                field_rows.append(
                    (
                        FIELD_LABEL.get(field, field),
                        f"вручную: {bad_fields.get(field, 0)} · авто-флагов: {auto_field.get(field, 0)}",
                    )
                )
    else:
        for field in FIELD_ORDER:
            if bad_fields.get(field):
                field_rows.append((FIELD_LABEL.get(field, field), str(bad_fields[field])))

    md = ["## Результаты проверки промта\n", table(rows, ("Показатель", "Значение"))]

    if field_rows:
        md.append("\n### Проблемные поля\n")
        md.append(table(field_rows, ("Поле", "Ошибок")))

    comments = [(v["index"], v["comment"]) for v in graded if v.get("comment")]
    if comments:
        md.append("\n### Комментарии\n")
        md += [f"- **#{idx}** — {text}" for idx, text in comments]

    output = "\n".join(md) + "\n"
    print()
    print(output)

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(output, encoding="utf-8")
        print(f"Сохранено: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
