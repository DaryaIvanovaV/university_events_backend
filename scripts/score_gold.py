"""
Польова оцінка промта на синтетичному наборі з еталоном.

На відміну від `scripts.bench_llm`, який рахує події («знайшла / не знайшла»),
цей скрипт звіряє КОЖНЕ з 11 полів КОЖНОЇ події з еталоном `gold` і розкладає
їх по категоріях correct / invented_ok / wrong / fabricated / missed. Саме ці
числа йдуть у статтю: 35 повідомлень наукового керівника не переконали, а тут
1540 порівнянь полів на 118 анонсах.

Набір held-out: його повідомлення НЕ використовуються як few-shot і не
підбираються під промт. Якщо промт змінився, `prompt_sha256` у звіті
зміниться теж, і старі числа не можна порівнювати з новими.

Видобування робиться тим самим кодом, що й у `bench_llm` (вивантаження моделі,
прогрів, `make_ollama_chat_fn`, ті самі опції), тож латентність і відповіді
збігаються з уже опублікованими прогонами.

Приклади (PowerShell):
    python -m scripts.score_gold data\\synthetic\\synthetic_messages.json `
        --models gemma3:4b --limit 5
    python -m scripts.score_gold data\\synthetic\\synthetic_messages.json `
        --models gemma3:4b llama3.1:8b-instruct-q4_K_M --pipeline
    python -m scripts.score_gold data\\synthetic\\synthetic_messages.json `
        --from-run reports\\synthetic\\20260922_101500\\runs.jsonl
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import time
from collections import defaultdict
from pathlib import Path

from app.eval.gold_scoring import (
    BOOTSTRAP_RESAMPLES,
    BOOTSTRAP_SEED,
    CI_METRICS,
    FIELDS,
    MISSED,
    MISSED_EVENT,
    PAIRED,
    GoldMessage,
    MessageScore,
    PipelineView,
    aggregate,
    bootstrap_cis,
    by_half,
    load_gold,
    paired_bootstrap_diff,
    per_field_errors,
    score_message,
    trap_breakdown,
    without_ambiguous,
    without_boundary,
)
from app.ingestion.samples import load_messages
from app.llm import ollama_status
from app.llm.extractor import EventExtractor, ExtractionError
from app.llm.extractor import make_ollama_chat_fn
from app.llm.prompts import FEWSHOT, SYSTEM_PROMPT
from app.models.schemas import ExtractedEvent
from app.services.audience import classify_scope
from app.services.language import unsupported_reason
from app.services.meeting import normalize_meeting
from app.services.prefilter import prefilter
from app.services.relevance import stale_reason
from app.services.schedule import normalize_schedule
from scripts._console import force_utf8
from scripts.bench_llm import kind, unload, vram_share

force_utf8()

DEFAULT_DATASET = Path("data/synthetic/synthetic_messages.json")
DEFAULT_CORRECTIONS = Path("data/synthetic/corrections.json")
DEFAULT_OUT = Path("reports/synthetic")

# Особливості окремих моделей, заміряні 2026-09-06 (виноски до
# data/baselines/model_comparison_20260906.md). Застосовуються автоматично,
# бо без них прогін цих двох моделей просто не має сенсу:
#   * qwen3:4b-instruct під граматикою з нашої схеми зривається в нескінченні
#     повтори — її неможливо навіть заміряти;
#   * qwen3:8b за замовчуванням «міркує» сотні токенів ДО відповіді, на цьому
#     залізі це 60-125 с на повідомлення, і довгі анонси не встигають у таймаут.
# Обидві позначки доїжджають у runs.jsonl і в шапку звіту: числа таких
# прогонів не порівнюються з рештою напряму.
MODEL_QUIRKS: dict[str, dict[str, object]] = {
    "qwen3:4b-instruct-2507-q4_K_M": {"use_schema": False},
    "qwen3:8b-q4_K_M": {"think": False},
}


def quirks_for(model: str) -> dict[str, object]:
    """Особливості за тегом моделі; теги звіряються в обидва боки."""
    for key, quirks in MODEL_QUIRKS.items():
        if model == key or model.startswith(key + "-") or key.startswith(model + "-"):
            return dict(quirks)
    return {}


def prompt_hash() -> str:
    """
    Відбиток промта: системна інструкція + few-shot.

    Прив'язує числа до конкретної версії промта. Звіти з різними відбитками
    не можна класти в одну таблицю — це вже різні системи, і скрипт на цьому
    падає, а не мовчки змішує.
    """
    payload = SYSTEM_PROMPT + json.dumps(FEWSHOT, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# --- Видобування ------------------------------------------------------------


def extract_all(
    messages, model: str, num_ctx: int, prompt_sha: str,
    *, quirks: dict[str, object] | None = None,
) -> list[dict]:
    """
    Прогін датасету однією моделлю. Один рядок runs.jsonl на повідомлення.

    Повторює послідовність `bench_llm.run_one`: вивантажити модель, прогріти,
    той самий chat_fn. Різниця лише в тому, що тут зберігаються САМІ події
    (для звірки з еталоном), а не агрегати.
    """
    quirks = dict(quirks or {})
    unload(model)
    ctx_label = str(num_ctx) if num_ctx else "дефолт"
    note = ""
    if quirks.get("use_schema") is False:
        note += "  format=json (БЕЗ схеми!)"
    if quirks.get("think") is False:
        note += "  think=false"
    print(f"\n=== {model}, num_ctx={ctx_label}{note} ===")
    warm_s = ollama_status.warmup(model=model, num_ctx=num_ctx or None)
    share, total_gb = vram_share(model)
    print(f"  прогрів {warm_s:.1f} с; у VRAM {share:.0%} з {total_gb:.2f} ГБ")

    extractor = EventExtractor(
        make_ollama_chat_fn(model=model, num_ctx=num_ctx, **quirks)  # type: ignore[arg-type]
    )
    records: list[dict] = []
    for i, msg in enumerate(messages, 1):
        started = time.perf_counter()
        error: str | None = None
        events: list[dict] = []
        try:
            result = extractor.extract(msg.text, msg.date)
            events = [e.model_dump(mode="json") for e in result.events]
        except ExtractionError as exc:
            error = type(exc).__name__
        except Exception as exc:                       # мережа лягла посеред прогону
            error = f"{type(exc).__name__}: {exc}"[:200]
        seconds = time.perf_counter() - started
        records.append({
            "model": model,
            "message_id": msg.message_id,
            "events": events,
            "error": error,
            "seconds": round(seconds, 2),
            "prompt_sha256": prompt_sha,
            "quirks": quirks or None,
        })
        mark = "ЗБІЙ" if error else f"подій: {len(events)}"
        # flush обов'язковий: прогін на 200 повідомлень триває годину, і без
        # цього перенаправлений у файл вивід мовчить до кінця — не відрізнити
        # «працює» від «зависло».
        print(f"  [{i:3}/{len(messages)}] {seconds:5.1f} с  {kind(msg.note):12} {mark}",
              flush=True)
    return records


# --- Погляд «після детермінованих фільтрів» ---------------------------------


def pipeline_view(
    raw_events: list[dict], text: str, reference_date: dt.date,
    *, threshold: float = 1.0,
) -> PipelineView:
    """
    Проганяє події тими самими кроками, що й `app/services/pipeline.py`.

    Порядок і самі функції — ті, що в пайплайні; БД не потрібна, бо крок
    `crud.upsert_event` до фільтрів стосунку не має. Сервіси НЕ змінюються:
    нам потрібні саме продові рішення, а не їх копія.
    """
    view = PipelineView()
    pf = prefilter(text, threshold=threshold)
    if not pf.is_candidate:
        view.prefiltered = f"пре-фільтр не пропустив (score={pf.score:.1f})"
        return view
    wrong_language = unsupported_reason(text)
    if wrong_language is not None:
        view.prefiltered = wrong_language
        return view

    seen: set[tuple[str, dt.date | None]] = set()
    for raw in raw_events:
        try:
            event = ExtractedEvent.model_validate(raw)
        except Exception as exc:                        # збережений runs.jsonl зіпсовано
            view.dropped.append((str(raw.get("event_title", "?")), f"не валідується: {exc}"))
            continue
        event = normalize_meeting(event, text)
        event = normalize_schedule(event, text, reference_date)

        key = (event.event_title.strip().casefold(), event.date)
        if key in seen:
            view.dropped.append((event.event_title, "дубль у межах повідомлення"))
            continue
        seen.add(key)

        lang_issue = unsupported_reason(text, event.language)
        if lang_issue is not None:
            view.dropped.append((event.event_title, lang_issue))
            continue
        verdict = classify_scope(event, text)
        if verdict.is_individual:
            view.dropped.append((event.event_title, f"індивідуальний формат: {verdict.reason}"))
            continue
        stale = stale_reason(event, reference_date, text)
        if stale is not None:
            view.dropped.append((event.event_title, stale))
            continue
        view.kept_events.append(event.model_dump(mode="json"))
    return view


# --- Оцінка -----------------------------------------------------------------


def score_records(
    records: list[dict],
    gold: dict[int, GoldMessage],
    *,
    with_pipeline: bool = False,
) -> tuple[dict[str, list[MessageScore]], dict[str, list[MessageScore]]]:
    """
    Записи прогону → оцінки, окремо «сира LLM» і «після фільтрів».

    Другий словник порожній без `--pipeline`.
    """
    raw_scores: dict[str, list[MessageScore]] = defaultdict(list)
    pipe_scores: dict[str, list[MessageScore]] = defaultdict(list)
    for record in records:
        message_id = int(record["message_id"])
        gold_message = gold.get(message_id)
        if gold_message is None:
            continue
        events = list(record.get("events") or [])
        view = None
        if with_pipeline:
            view = pipeline_view(
                events, gold_message.text, dt.date.fromisoformat(gold_message.date)
            )
        raw_scores[record["model"]].append(score_message(
            gold_message, events,
            error=record.get("error"), seconds=record.get("seconds"), pipeline=view,
        ))
        if view is not None:
            pipe_scores[record["model"]].append(score_message(
                gold_message, view.kept_events,
                error=record.get("error"), seconds=record.get("seconds"), pipeline=view,
            ))
    return dict(raw_scores), dict(pipe_scores)


# --- Рендер звіту -----------------------------------------------------------


def pct(value: float | None) -> str:
    return "—" if value is None else f"{value:.1%}"


def num(value: float | None, digits: int = 1) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def _table(header: list[str], rows: list[list[str]]) -> list[str]:
    out = ["| " + " | ".join(header) + " |",
           "|" + "|".join("---" for _ in header) + "|"]
    out += ["| " + " | ".join(row) + " |" for row in rows]
    out.append("")
    return out


def render_model_tables(
    scores_by_model: dict[str, list[MessageScore]], title: str
) -> list[str]:
    """Таблиці 1 і 2 статті + пастки + багатоподійні — для одного зрізу."""
    lines = [f"### {title}", ""]
    models = list(scores_by_model)

    lines.append("**Поля видобутих подій.** Знаменник `точність (усі)` — усі "
                 "еталонні поля анонсів: поля НЕ знайденої події теж рахуються "
                 "як пропущені.")
    lines.append("")
    rows = []
    for model in models:
        m = aggregate(scores_by_model[model])
        rows.append([
            f"`{model}`",
            str(m["correct"]), str(m["invented_ok"]), str(m["invented_bad"]),
            str(m["missed"]), str(m["missed_event_fields"]),
            str(m["gold_fields_total"]),
            pct(m["field_accuracy_all"]), pct(m["field_accuracy_paired"]),
        ])
    lines += _table(
        ["модель", "correct", "invented_ok", "invented_bad", "missed",
         "поля втрачених подій", "усього полів", "точність (усі)",
         "точність (спарені)"],
        rows,
    )

    lines.append("**Частки помилок у спарених подіях.** `fabrication_rate` — "
                 "головна метрика галюцинацій: частка вигаданих значень там, де "
                 "еталон вимагає null.")
    lines.append("")
    rows = []
    for model in models:
        m = aggregate(scores_by_model[model])
        rows.append([
            f"`{model}`",
            pct(m["fabrication_rate"]), f"{m['fabricated']}/{m['paired_gold_null']}",
            pct(m["wrong_rate"]), f"{m['wrong']}/{m['paired_gold_nonnull']}",
            pct(m["omission_rate"]), f"{m['missed']}/{m['paired_gold_nonnull']}",
            str(m["extra_events"]), str(m["trap_false_events"]),
        ])
    lines += _table(
        ["модель", "fabrication_rate", "вигадано/null", "wrong_rate",
         "невірно/непорожніх", "omission_rate", "пропущено/непорожніх",
         "зайвих подій", "вигаданих подій на пастках"],
        rows,
    )

    lines.append("**Помилки за полями.** Рядок на кожне з 11 полів схеми.")
    lines.append("")
    for model in models:
        table = per_field_errors(scores_by_model[model])
        rows = []
        for name in FIELDS:
            row = table[name]
            total = row["total"]
            good = row["correct"] + row["invented_ok"]
            rows.append([
                f"`{name}`", str(row["correct"]), str(row["correct_null"]),
                str(row["invented_ok"]), str(row["wrong"]), str(row["fabricated"]),
                str(row[MISSED]), str(total),
                pct(good / total if total else None),
            ])
        lines.append(f"`{model}`:")
        lines.append("")
        lines += _table(
            ["поле", "correct", "з них null", "invented_ok", "wrong",
             "fabricated", "missed", "усього", "точність"],
            rows,
        )

    lines.append("**Виявлення подій і пастки.**")
    lines.append("")
    rows = []
    for model in models:
        scores = scores_by_model[model]
        m = aggregate(scores)
        no_boundary = aggregate(without_boundary(s for s in scores if s.kind == "trap"))
        rows.append([
            f"`{model}`",
            f"{m['announcements_detected']}/{m['announcements']}",
            pct(m["detection_rate"]), str(m["missed_events"]),
            f"{m['traps_clean']}/{m['traps']}", pct(m["correct_reject_rate"]),
            f"{no_boundary['traps_clean']}/{no_boundary['traps']}",
            pct(no_boundary["correct_reject_rate"]),
            str(m["errors"]),
        ])
    lines += _table(
        ["модель", "анонси з подією", "частка", "втрачених подій",
         "пастки чисто", "частка", "пастки без boundary", "частка", "збоїв"],
        rows,
    )

    lines.append("**Пастки за підтипами.** Рядки `boundary:*` винесені окремо: "
                 "їхня «пастковість» тримається на угоді календаря факультету "
                 "(дедлайн без заходу, зміна іспиту, графік захистів), а не на "
                 "безспірній помилці моделі.")
    lines.append("")
    for model in models:
        table = trap_breakdown(scores_by_model[model])
        ordinary = sorted(k for k, v in table.items() if not v["boundary"])
        boundary = sorted(k for k, v in table.items() if v["boundary"])
        rows = []
        for name in ordinary + boundary:
            row = table[name]
            mark = " *(boundary)*" if row["boundary"] else ""
            rows.append([
                f"`{name}`{mark}", str(row["traps"]), str(row["clean"]),
                pct(row["clean"] / row["traps"] if row["traps"] else None),
                str(row["false_events"]),
            ])
        lines.append(f"`{model}`:")
        lines.append("")
        lines += _table(
            ["підтип", "пасток", "чисто", "частка", "вигаданих подій"], rows
        )

    lines.append("**Багатоподійні повідомлення та індивідуальні консультації.**")
    lines.append("")
    rows = []
    for model in models:
        m = aggregate(scores_by_model[model])
        rows.append([
            f"`{model}`",
            f"{m['multi_all_paired']}/{m['multi']}", str(m["multi_merged"]),
            f"{m['individual_extracted']}/{m['individual']}",
            f"{m['individual_dropped']}/{m['individual_extracted']}"
            if m["individual_extracted"] else "—",
        ])
    lines += _table(
        ["модель", "усі події знайдено", "злито в одну (merged)",
         "індивідуальні видобуто", "з них відсіяв audience.py"],
        rows,
    )
    lines.append("«Індивідуальні видобуто» — це НЕ помилка моделі: відсікати їх "
                 "зобов'язаний детермінований фільтр `app/services/audience.py`. "
                 "Остання колонка заповнюється лише з `--pipeline`.")
    lines.append("")
    return lines


def render_summary(
    scores_by_model: dict[str, list[MessageScore]],
    pipe_by_model: dict[str, list[MessageScore]],
    meta: dict,
) -> str:
    """Повний `summary.md`."""
    lines = ["# Польова оцінка промта на синтетичному наборі", ""]
    lines.append(f"- Датасет: `{meta['dataset']}`")
    lines.append(f"- Оцінено повідомлень: **{meta['messages']}** "
                 f"({meta['kinds']})")
    lines.append(f"- Моделі: {', '.join('`' + m + '`' for m in scores_by_model)}")
    lines.append(f"- `prompt_sha256`: `{meta['prompt_sha256']}`")
    lines.append(f"- Дата прогону: {meta['measured_at']}")
    lines.append(f"- Опції: {meta['options']}")
    if meta.get("corrections"):
        lines.append(f"- Правки еталона: {len(meta['corrections'])} — "
                     + "; ".join(meta["corrections"][:5]))
    else:
        lines.append("- Правки еталона: не застосовано")
    lines.append(f"- Бутстреп: {BOOTSTRAP_RESAMPLES} перевибірок, сід {BOOTSTRAP_SEED}")
    for model, quirks in (meta.get("quirks") or {}).items():
        notes = []
        if quirks.get("use_schema") is False:
            notes.append("`format=json` БЕЗ constrained decoding за нашою схемою "
                         "— числа не порівнюються з рештою напряму, у продовому "
                         "режимі модель не працює")
        if quirks.get("think") is False:
            notes.append("`think=false` — з увімкненими міркуваннями модель "
                         "витрачає сотні токенів до відповіді")
        lines.append(f"- ⚠ `{model}`: " + "; ".join(notes))
    lines.append("")
    lines.append("> Набір синтетичний і held-out. Числа НЕ можна змішувати з "
                 "прогонами на `data/test_messages.json` (35 реальних "
                 "повідомлень) — це різні набори й різні знаменники.")
    lines.append("")

    lines.append("## 1. Сира відповідь моделі")
    lines.append("")
    lines += render_model_tables(scores_by_model, "Усі повідомлення")
    lines += render_model_tables(
        {m: without_ambiguous(s) for m, s in scores_by_model.items()},
        "Без `ambiguous:next_weekday` (5 повідомлень)",
    )

    if pipe_by_model:
        lines.append("## 2. Після детермінованих фільтрів (`--pipeline`)")
        lines.append("")
        lines.append("Ті самі функції, що й у `app/services/pipeline.py`: "
                     "`normalize_meeting` → `normalize_schedule` → зняття "
                     "дублів → `classify_scope` → `stale_reason`.")
        lines.append("")
        lines += render_model_tables(pipe_by_model, "Усі повідомлення")
        lines += render_model_tables(
            {m: without_ambiguous(s) for m, s in pipe_by_model.items()},
            "Без `ambiguous:next_weekday`",
        )

    lines.append("## 3. Довірчі інтервали (бутстреп по повідомленнях)")
    lines.append("")
    rows = []
    for model, scores in scores_by_model.items():
        cis = bootstrap_cis(scores)
        m = aggregate(scores)
        row = [f"`{model}`"]
        for name in CI_METRICS:
            value = m[name]
            low, high = cis.get(name, (None, None))
            row.append(
                pct(value) + (f" [{pct(low)}; {pct(high)}]" if low is not None else "")
            )
        rows.append(row)
    lines += _table(
        ["модель", "точність полів (усі)", "fabrication_rate", "пастки чисто"],
        rows,
    )

    if len(scores_by_model) > 1:
        lines.append("**Попарна різниця точності полів** (та сама перевибірка "
                     "для обох моделей). Якщо інтервал накриває 0, різниця на "
                     "цьому наборі не розрізняється.")
        lines.append("")
        models = list(scores_by_model)
        rows = []
        for i, left in enumerate(models):
            for right in models[i + 1:]:
                diff, low, high = paired_bootstrap_diff(
                    scores_by_model[left], scores_by_model[right]
                )
                verdict = "не розрізняються" if low <= 0 <= high else "різниця значуща"
                rows.append([
                    f"`{left}` − `{right}`",
                    f"{diff:+.1%}", f"[{low:+.1%}; {high:+.1%}]", verdict,
                ])
        lines += _table(["пара", "різниця", "95% ДІ", "висновок"], rows)

    lines.append("## 4. Стабільність за половинами набору")
    lines.append("")
    lines.append("Половини згенеровані різними сідами й пулами тем "
                 "(3001–3100 — сід 20260921, 3101–3200 — сід 20260922). "
                 "Великий розрив означає, що результат залежить від "
                 "генератора, а не від моделі.")
    lines.append("")
    rows = []
    for model, scores in scores_by_model.items():
        halves = by_half(scores)
        row = [f"`{model}`"]
        values = []
        for half in sorted(halves):
            m = aggregate(halves[half])
            row.append(f"{half}: {pct(m['field_accuracy_all'])}")
            if m["field_accuracy_all"] is not None:
                values.append(m["field_accuracy_all"])
        gap = (max(values) - min(values)) if len(values) > 1 else None
        row.append(pct(gap))
        row.append("⚠ перевірити генератор" if gap is not None and gap > 0.05 else "ок")
        rows.append(row)
    width = max(len(r) for r in rows) if rows else 4
    header = ["модель"] + [f"половина {i}" for i in range(1, width - 2)] + ["розрив", "висновок"]
    lines += _table(header, rows)

    lines.append("## 5. Латентність")
    lines.append("")
    rows = []
    for model, scores in scores_by_model.items():
        times = sorted(s.seconds for s in scores if s.seconds is not None)
        if not times:
            continue
        median = times[len(times) // 2]
        rows.append([
            f"`{model}`", num(median), num(sum(times) / len(times)),
            num(times[0]), num(times[-1]), num(sum(times) / 60, 1) + " хв",
        ])
    lines += _table(
        ["модель", "медіана, с", "середнє, с", "мінімум, с", "максимум, с",
         "увесь прогін"],
        rows,
    )
    return "\n".join(lines) + "\n"


# --- Запис таблиць ----------------------------------------------------------


def write_fields_csv(path: Path, scores_by_model: dict[str, list[MessageScore]]) -> None:
    """Рядок на модель × повідомлення × еталонну подію × поле."""
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow([
            "model", "message_id", "kind", "category", "gold_event", "field",
            "field_category", "gold_value", "predicted_value",
        ])
        for model, scores in scores_by_model.items():
            for score in scores:
                for event in score.events:
                    if event.status == PAIRED:
                        for field_score in event.fields:
                            writer.writerow([
                                model, score.message_id, score.kind, score.category,
                                event.gold_index, field_score.name,
                                field_score.category, field_score.gold_repr,
                                field_score.pred_repr,
                            ])
                    elif event.status == MISSED_EVENT:
                        # Подію не знайдено — усі 11 полів пропущені.
                        for name in FIELDS:
                            writer.writerow([
                                model, score.message_id, score.kind, score.category,
                                event.gold_index, name, MISSED, "", "",
                            ])


def write_events_csv(path: Path, scores_by_model: dict[str, list[MessageScore]]) -> None:
    """Результати зіставлення подій."""
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow([
            "model", "message_id", "kind", "category", "tags", "status",
            "pair_score", "gold_index", "pred_index", "gold_title", "pred_title",
            "n_gold", "n_pred", "merged", "error",
        ])
        for model, scores in scores_by_model.items():
            for score in scores:
                for event in score.events:
                    writer.writerow([
                        model, score.message_id, score.kind, score.category,
                        " ".join(score.tags), event.status, event.score,
                        "" if event.gold_index is None else event.gold_index,
                        "" if event.pred_index is None else event.pred_index,
                        event.gold_title, event.pred_title,
                        score.n_gold, score.n_pred, int(score.merged),
                        score.error or "",
                    ])


# --- CLI --------------------------------------------------------------------


def load_run_records(path: Path) -> tuple[list[dict], str]:
    """Читає збережений runs.jsonl і звіряє відбиток промта."""
    records: list[dict] = []
    hashes: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        record = json.loads(line)
        records.append(record)
        hashes.add(str(record.get("prompt_sha256", "")))
    if len(hashes) > 1:
        raise SystemExit(
            f"У {path} змішані прогони з різними prompt_sha256: {sorted(hashes)}. "
            "Це різні версії промта — в один звіт вони не зводяться."
        )
    return records, hashes.pop() if hashes else ""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", nargs="?", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--models", nargs="+", default=None,
                        help="теги моделей Ollama (мають бути завантажені)")
    parser.add_argument("--ctx", type=int, default=0,
                        help="num_ctx; 0 = дефолт Ollama")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--only", choices=["trap", "announcement", "individual"],
                        default=None)
    parser.add_argument("--pipeline", action="store_true",
                        help="додати погляд «після детермінованих фільтрів»")
    parser.add_argument("--no-schema", nargs="*", default=(), metavar="МОДЕЛЬ",
                        help="моделі, яким слати format=json замість схеми")
    parser.add_argument("--no-think", nargs="*", default=(), metavar="МОДЕЛЬ",
                        help="моделі, яким вимкнути блок міркувань")
    parser.add_argument("--from-run", type=Path, default=None,
                        help="перерахувати збережений runs.jsonl без Ollama")
    parser.add_argument("--corrections", type=Path, default=DEFAULT_CORRECTIONS,
                        help="правки еталона (за замовчуванням — якщо файл існує)")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--run-id", default=None,
                        help="ім'я теки звіту; типово — позначка часу")
    args = parser.parse_args()

    if args.from_run is None and not args.models:
        parser.error("вкажіть --models або --from-run")

    gold, applied = load_gold(args.dataset, args.corrections)
    messages, warnings = load_messages(args.dataset)
    for warning in warnings:
        print("warning:", warning)
    if args.only:
        messages = [m for m in messages if kind(m.note) == args.only]
    if args.limit:
        messages = messages[: args.limit]
    selected = {m.message_id for m in messages}
    gold = {k: v for k, v in gold.items() if k in selected}
    print(f"Датасет: {args.dataset} — {len(messages)} повідомлень")
    if applied:
        print(f"Правок еталона застосовано: {len(applied)}")

    sha = prompt_hash()
    used_quirks: dict[str, dict] = {}
    if args.from_run is not None:
        records, saved_sha = load_run_records(args.from_run)
        records = [r for r in records if int(r["message_id"]) in selected]
        if saved_sha and saved_sha != sha:
            print(f"warning: промт змінився з часу прогону "
                  f"({saved_sha[:12]}… → {sha[:12]}…) — числа стосуються старого")
        sha = saved_sha or sha
        for record in records:                 # особливості зберігає сам прогін
            if record.get("quirks"):
                used_quirks[record["model"]] = record["quirks"]
    else:
        records = []
        for model in args.models:
            quirks = quirks_for(model)
            if model in args.no_schema:
                quirks["use_schema"] = False
            if model in args.no_think:
                quirks["think"] = False
            if quirks:
                used_quirks[model] = quirks
            records += extract_all(messages, model, args.ctx, sha, quirks=quirks)

    raw_scores, pipe_scores = score_records(records, gold, with_pipeline=args.pipeline)
    if not raw_scores:
        raise SystemExit("жодного оціненого повідомлення — перевірте фільтри")

    run_id = args.run_id or dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = args.out / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    # Шапка має описувати те, що СПРАВДІ оцінено: при --from-run у файлі
    # прогону може бути менше повідомлень, ніж у датасеті.
    scored_ids = {s.message_id for scores in raw_scores.values() for s in scores}
    kinds: dict[str, int] = {}
    for message_id in scored_ids:
        kind_name = gold[message_id].kind
        kinds[kind_name] = kinds.get(kind_name, 0) + 1
    options = (f"--ctx={args.ctx} --limit={args.limit} --only={args.only} "
               f"--pipeline={args.pipeline}"
               + (f" --from-run={args.from_run}" if args.from_run else ""))
    meta = {
        "run_id": run_id,
        "dataset": str(args.dataset),
        "corrections_path": str(args.corrections),
        "corrections": applied,
        "messages": len(scored_ids),
        "kinds": kinds,
        "models": list(raw_scores),
        "prompt_sha256": sha,
        "measured_at": dt.datetime.now().isoformat(timespec="seconds"),
        "options": options,
        "pipeline": args.pipeline,
        "quirks": used_quirks,
    }

    (out_dir / "runs.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records),
        encoding="utf-8",
    )
    (out_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / "summary.md").write_text(
        render_summary(raw_scores, pipe_scores, meta), encoding="utf-8"
    )
    write_fields_csv(out_dir / "fields.csv", raw_scores)
    write_events_csv(out_dir / "events.csv", raw_scores)

    print(f"\nЗвіт: {out_dir / 'summary.md'}")
    for model, scores in raw_scores.items():
        m = aggregate(scores)
        print(f"  {model}: точність полів {pct(m['field_accuracy_all'])}, "
              f"вигадано {pct(m['fabrication_rate'])}, "
              f"пастки чисто {m['traps_clean']}/{m['traps']}")
    print(f"Ручний розгляд: python -m scripts.synthetic.review_export {out_dir}")


if __name__ == "__main__":
    main()
