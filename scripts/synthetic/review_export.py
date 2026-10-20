"""
Вивантаження розбіжностей прогону на ручний розгляд.

Дивитися очима всі 200 повідомлень × 11 полів × кожну модель неможливо, та й
не потрібно: там, де модель збіглася з еталоном, розглядати нема чого. Сюди
потрапляють ЛИШЕ клітинки, де хоч одна модель не згодна з еталоном.

Порядок навмисний: угорі повідомлення, де ДВІ або більше моделей дали ОДНЕ Й
ТЕ САМЕ значення, і воно розходиться з еталоном. Незалежні моделі рідко
помиляються однаково — це найімовірніші помилки самої розмітки, і починати
треба з них.

Модель перехресної перевірки (`--cross-check`) виноситься в окремий розділ і
в жодну метрику статті не входить: її завдання — підсвітити спірний еталон,
а не змагатися з рештою.

Запуск:  python -m scripts.synthetic.review_export reports\\synthetic\\<run_id>
         python -m scripts.synthetic.review_export <тека> --cross-check gemma3:12b
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

from app.eval.gold_scoring import (
    CORRECT,
    EXTRA_EVENT,
    FALSE_EVENT,
    FIELDS,
    INVENTED_OK,
    MISSED_EVENT,
    PAIRED,
    GoldMessage,
    MessageScore,
    load_gold,
    norm,
)
from scripts._console import force_utf8

force_utf8()

GOOD = {CORRECT, INVENTED_OK}


def _load_run(run_dir: Path) -> tuple[dict, list[dict]]:
    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    records = [
        json.loads(line)
        for line in (run_dir / "runs.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return meta, records


def gold_summary(gold: GoldMessage) -> list[str]:
    """Компактний опис еталона — щоб рецензент не гортав датасет."""
    lines = []
    for i, event in enumerate(gold.events, 1):
        parts = [f"«{event['event_title']['value']}»", event["date"]["value"]]
        if event["date"].get("inferred"):
            parts[-1] += f" (з «{event['date']['phrase']}»)"
        for name in ("time", "location", "organizer", "target_audience"):
            value = event[name].get("value")
            parts.append(f"{name}={value if value is not None else 'null'}")
        parts.append(f"type={event['event_type']['value']}")
        if event["link"]["value"]:
            parts.append(f"link={event['link']['value']}")
        if event["meeting_code"]["value"]:
            parts.append(f"code={event['meeting_code']['value']}")
        lines.append(f"- подія {i}: " + " | ".join(parts))
    if not lines:
        lines.append("- еталонних подій немає (пастка)")
    return lines


def consensus_cells(
    per_model: dict[str, MessageScore], models: list[str]
) -> int:
    """
    Скільки клітинок, де ≥2 моделі дали однакову відповідь ПРОТИ еталона.

    Для пастки те саме рахується на рівні події: дві моделі, що вигадали
    подію на одному й тому самому тексті, — сильний сигнал, що текст усе-таки
    схожий на анонс.
    """
    count = 0
    any_score = next(iter(per_model.values()), None)
    if any_score is None:
        return 0
    if any_score.kind == "trap":
        wrong_models = [m for m in models
                        if m in per_model and per_model[m].n_false_events > 0]
        return 1 if len(wrong_models) >= 2 else 0

    by_cell: dict[tuple[int, str], list[str]] = defaultdict(list)
    for model in models:
        score = per_model.get(model)
        if score is None:
            continue
        for event in score.events:
            if event.status != PAIRED or event.gold_index is None:
                continue
            for field_score in event.fields:
                if field_score.category in GOOD:
                    continue
                by_cell[(event.gold_index, field_score.name)].append(
                    norm(field_score.pred_repr)
                )
    for values in by_cell.values():
        if len(values) < 2:
            continue
        for value in set(values):
            if values.count(value) >= 2:
                count += 1
                break
    return count


def _field_rows(
    per_model: dict[str, MessageScore], models: list[str]
) -> list[list[str]]:
    """Рядки таблиці розбіжностей: подія × поле × значення кожної моделі."""
    cells: dict[tuple[int, str], dict[str, str]] = defaultdict(dict)
    gold_values: dict[tuple[int, str], str] = {}
    for model in models:
        score = per_model.get(model)
        if score is None:
            continue
        for event in score.events:
            if event.status != PAIRED or event.gold_index is None:
                continue
            for field_score in event.fields:
                key = (event.gold_index, field_score.name)
                gold_values.setdefault(key, field_score.gold_repr or "null")
                if field_score.category in GOOD:
                    cells[key][model] = "ok"
                else:
                    shown = field_score.pred_repr or "null"
                    cells[key][model] = f"{shown} ({field_score.category})"
    rows = []
    for key in sorted(cells):
        values = cells[key]
        if all(v == "ok" for v in values.values()):
            continue
        gold_index, name = key
        rows.append([
            str(gold_index + 1), f"`{name}`", gold_values.get(key, "") or "null",
            *[values.get(m, "—") for m in models],
        ])
    return rows


def _event_notes(per_model: dict[str, MessageScore], models: list[str]) -> list[str]:
    """Події, які не зіставилися: зайві, вигадані на пастці, пропущені."""
    notes = []
    for model in models:
        score = per_model.get(model)
        if score is None:
            continue
        if score.error:
            notes.append(f"- `{model}`: ЗБІЙ видобування — {score.error}")
        for event in score.events:
            if event.status == EXTRA_EVENT:
                notes.append(f"- `{model}`: зайва подія «{event.pred_title}»")
            elif event.status == FALSE_EVENT:
                notes.append(f"- `{model}`: вигадана подія на пастці «{event.pred_title}»")
            elif event.status == MISSED_EVENT:
                notes.append(f"- `{model}`: не знайдено еталонну подію «{event.gold_title}»")
    return notes


def render_message(
    gold: GoldMessage, per_model: dict[str, MessageScore], models: list[str],
    consensus: int,
) -> list[str] | None:
    """Блок одного повідомлення. None — розбіжностей немає, показувати нічого."""
    rows = _field_rows(per_model, models)
    notes = _event_notes(per_model, models)
    if not rows and not notes:
        return None

    lines = [f"## {gold.message_id} · {gold.kind} · `{gold.category}`"]
    if gold.tags:
        lines.append(f"теги: {', '.join('`' + t + '`' for t in gold.tags)}")
    if consensus:
        lines.append(f"**⚠ згода моделей проти еталона у {consensus} клітинках** "
                     "— найімовірніше, помилка розмітки")
    lines.append("")
    lines.append(f"Дата публікації: `{gold.date}`")
    lines.append("")
    lines.append("Текст:")
    lines.append("")
    for paragraph in gold.text.splitlines():
        lines.append(f"> {paragraph}")
    lines.append("")
    lines.append(f"Очікується (`_expect`): {gold.expect}")
    lines.append("")
    lines.append("Еталон:")
    lines += gold_summary(gold)
    lines.append("")
    if rows:
        header = ["подія", "поле", "еталон"] + [f"`{m}`" for m in models]
        lines.append("| " + " | ".join(header) + " |")
        lines.append("|" + "|".join("---" for _ in header) + "|")
        lines += ["| " + " | ".join(cell.replace("|", "\\|") for cell in row) + " |"
                  for row in rows]
        lines.append("")
    if notes:
        lines.append("Події:")
        lines += notes
        lines.append("")
    lines.append("verdict: gold wrong / model wrong / both acceptable")
    lines.append("")
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--cross-check", nargs="*", default=[],
                        help="моделі перехресної перевірки — окремий розділ")
    parser.add_argument("--out", type=Path, default=None,
                        help="куди писати; типово <run_dir>/review.md")
    args = parser.parse_args()

    meta, records = _load_run(args.run_dir)
    gold, _ = load_gold(meta["dataset"], meta.get("corrections_path"))

    # Перерахунок замість читання CSV: так огляд і метрики беруться з одного
    # коду, і розбіжність між review.md і summary.md неможлива за побудовою.
    from scripts.score_gold import score_records

    raw_scores, _ = score_records(records, gold, with_pipeline=False)
    by_message: dict[int, dict[str, MessageScore]] = defaultdict(dict)
    for model, scores in raw_scores.items():
        for score in scores:
            by_message[score.message_id][model] = score

    cross = [m for m in args.cross_check if m in raw_scores]
    main_models = [m for m in raw_scores if m not in cross]
    missing = [m for m in args.cross_check if m not in raw_scores]

    def build(models: list[str]) -> tuple[list[list[str]], int]:
        blocks: list[tuple[int, int, list[str]]] = []
        for message_id in sorted(by_message):
            per_model = {m: s for m, s in by_message[message_id].items() if m in models}
            if not per_model:
                continue
            consensus = consensus_cells(per_model, models) if len(models) > 1 else 0
            block = render_message(gold[message_id], per_model, models, consensus)
            if block is not None:
                blocks.append((-consensus, message_id, block))
        blocks.sort()
        return [b for _, _, b in blocks], len(blocks)

    lines = ["# Розбіжності з еталоном — ручний розгляд", ""]
    lines.append(f"- Прогін: `{meta['run_id']}`, датасет `{meta['dataset']}`")
    lines.append(f"- `prompt_sha256`: `{meta['prompt_sha256']}`")
    lines.append(f"- Моделі звіту: {', '.join('`' + m + '`' for m in main_models) or '—'}")
    if cross:
        lines.append(f"- Перехресна перевірка: {', '.join('`' + m + '`' for m in cross)} "
                     "(у метрики статті НЕ входить)")
    if missing:
        lines.append(f"- ⚠ моделей перехресної перевірки немає в прогоні: "
                     f"{', '.join(missing)}")
    lines.append("")
    lines.append("Для кожного повідомлення заповніть рядок `verdict:` — "
                 "`gold wrong` (помилка розмітки), `model wrong` (помилка "
                 "моделі) або `both acceptable` (обидва варіанти прийнятні). "
                 "Правки еталона після розгляду кладуться в "
                 "`data/synthetic/corrections.json`, сам датасет не редагується.")
    lines.append("")

    main_blocks, main_count = build(main_models) if main_models else ([], 0)
    lines.append(f"Повідомлень із розбіжностями: **{main_count}** з "
                 f"{len(by_message)}.")
    lines.append("")
    for block in main_blocks:
        lines += block

    if cross:
        cross_blocks, cross_count = build(cross)
        lines.append("---")
        lines.append("")
        lines.append("# Можливі помилки еталона (перехресна перевірка)")
        lines.append("")
        lines.append(f"Модель {', '.join('`' + m + '`' for m in cross)} не звітується "
                     "у статті. Її розбіжності з еталоном — підказка, де розмітка "
                     f"може бути хибною. Повідомлень: **{cross_count}**.")
        lines.append("")
        for block in cross_blocks:
            lines += block

    out_path = args.out or (args.run_dir / "review.md")
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Розбіжності: {out_path} ({main_count} повідомлень)")


if __name__ == "__main__":
    main()
