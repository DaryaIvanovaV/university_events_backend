# Task: synthetic held-out benchmark (200 messages) + gold-aware field-level scorer

## Context

- Repo: `C:\Users\HuGox\Desktop\files (2)\conference-events`. Windows 11, PowerShell, venv, Python 3.14. Always run modules as `python -m ...`. Ollama at `http://localhost:11434`.
- My scientific supervisor rejected the 35-message evaluation as too small. I now have a synthetic held-out set of 200 Ukrainian messages built "gold first, text second": event parameters were sampled randomly with a fixed seed, dates were computed by the prompt's own rules, and only then an LLM (Claude, not one of the tested models) wrote a message around each record. A deterministic checker confirms that every gold value is present in its text.
- Goal of this task: evaluate the CURRENT prompt on this set, frozen, with automatic field-level scoring, and send to manual review only the disagreements.
- Reuse, do not duplicate: `app/ingestion/samples.py::load_messages` (reads `text`, `date`, `source_channel`, `source_message_id`, `_expect` → `ExportMessage.note`), `scripts/bench_llm.py` (`kind()` by `_expect` prefix, `run_one()` warmup/unload/extract loop), `app/llm/extractor.py` (`EventExtractor`, `make_ollama_chat_fn`), `app/llm/prompts.py` (`SYSTEM_PROMPT`, `FEWSHOT`), `app/services/pipeline.py` and the services it calls, `scripts/_console.force_utf8`.
- Code comments in Russian (repo style). Keep the existing test suite green.

## Files I am giving you — place them exactly here

| file | destination |
|---|---|
| `synthetic_messages.json` | `data/synthetic/synthetic_messages.json` |
| `check_synthetic.py` | `scripts/synthetic/check_synthetic.py` (also create empty `scripts/synthetic/__init__.py`) |
| `audit_sample.md`, `audit_sample_2.md` | `data/synthetic/` (store only; my manual audit, 25 + 15 messages) |
| `generation_sources.zip` | `data/synthetic/generation_sources.zip` (provenance; store, do not unpack into packages) |

## Dataset format (the scorer depends on it)

JSON array of 200 objects (ids 3001–3200):

```json
{
  "source_message_id": 3043,
  "date": "2026-10-12",
  "source_channel": "@synthetic_faculty",
  "text": "…Ukrainian message…",
  "_expect": "1 подія: …",
  "gold": {
    "kind": "announcement | trap | individual",
    "category": "single | multi:<kind> | postponed | mass_consultation | individual | trap:<subtype>",
    "tags": ["relative:next_weekday", "ambiguous:next_weekday", "online", "boundary", "deadline_mention", "..."],
    "reaches_db": true,
    "events": [ { "...11 fields, below..." } ]
  }
}
```

- `date` is the publication date = the reference date passed to the extractor.
- `_expect` follows the `data/test_messages.json` convention (`ПАСТКА…` = trap, `ІНДИВІДУАЛЬНА…` = individual, otherwise announcement), so `scripts.bench_llm` works on this file unchanged.
- Every gold event has exactly the 11 schema fields, each an object:
  - `event_title`: `{value, keys}`
  - `date`: `{value: "YYYY-MM-DD", inferred: bool, phrase, range_end?}` — `inferred=true` only for dates computed from relative expressions
  - `time`: `{value: "HH:MM" | null, phrase, span?}`
  - `location`, `organizer`, `target_audience`: `{value | null, keys | null}`
  - `event_type`: `{value, accept: [acceptable alternative enum values]}`
  - `description`: `{present: true}`
  - `language`: `{value: "uk"}`
  - `link`: `{value | null}`
  - `meeting_code`: `{value | null, keys | null}`
- `keys` = list of groups. A predicted value matches if EVERY group has AT LEAST ONE alternative that is a substring of the normalized predicted value (AND of ORs).
- Normalization — implement once and reuse everywhere; it must stay identical to `norm()` in `check_synthetic.py`: NFKC + casefold; `’ ʼ ` ´` → `'`; `– — ‑ −` → `-`; drop `№`; `2-го / 2-й / 2-ого / 2-му` → `2`; remove spaces around a hyphen that precedes a digit; collapse whitespace. For `meeting_code`, also remove all whitespace before matching.

Composition: 200 messages = 118 announcements (88 single-event, 18 multi-event, 6 postponed, 6 mass consultations) + 70 traps (13 subtypes) + 12 individual consultations; 152 gold events, 140 of them in announcements → 1540 announcement fields (140 × 11). Two hundreds generated with different seeds and topic pools: 3001–3100 (seed 20260921) and 3101–3200 (seed 20260922; more multi-event messages, postponements and consultations).

Labelling conventions — deliberate, do NOT "fix" them in code or data:

1. Dates follow prompt rules 1–7 relative to `date`. «цього четверга» and «у найближчу суботу» are treated as rule 3; «через два тижні» = +14 days. «наступного/наступної <weekday>» uses rule 4 (+7 days after rule 3). These 5 messages carry `ambiguous:next_weekday` because message 211 in the real set uses the other reading — report metrics both with and without them.
2. Deadlines are not events. In the 11 announcements tagged `deadline_mention` the registration deadline belongs in `description`; an extra `deadline` event counts as an extra event. Deadline-only messages are traps.
3. `boundary` traps (4 deadline-only, 4 exam changes, 2 defence schedules) have `events: []` under the faculty-calendar convention used in the article. Always report them in a separate row so the headline trap metric does not depend on a contested convention.
4. `individual` messages have the consultation in gold (the model may extract it) but `reaches_db: false` — the deterministic audience filter must drop it.
5. Speakers, participating companies and other events' organizers are not the organizer; gold is null where the text states no organizer.
6. Postponements (tag `postponed`): gold holds the NEW date or time; the old one is a distractor. One message moves an event «з сьогодні на завтра», one changes only the start time.

## Part A — place files and verify  ⛔ CHECKPOINT 1

1. Copy the files. Run:
   ```powershell
   python -m scripts.synthetic.check_synthetic data/synthetic/synthetic_messages.json
   ```
   Expected: `200 повідомлень {'announcement': 118, 'trap': 70, 'individual': 12}, еталонних подій 152, помилок 0`, exit code 0.
2. Loader check (one-off snippet, not a new module): `load_messages("data/synthetic/synthetic_messages.json")` → 200 messages, zero warnings; `scripts.bench_llm.kind(m.note)` → announcement 118 / trap 70 / individual 12.
3. If any number differs — STOP and report. Do not continue.

## Part B — gold-aware scorer

Create `app/eval/gold_scoring.py` (pure logic, unit-testable) and `scripts/score_gold.py` (CLI). Do not change `load_messages` or `ExportMessage`: read the raw JSON separately and index `gold` by `source_message_id`; use `load_messages` for text and date so extraction is identical to `bench_llm`.

**B1. Running models.** `python -m scripts.score_gold <dataset> --models <tags…> [--ctx N] [--limit N] [--only trap|announcement|individual] [--out reports/synthetic]`. Reuse the extraction loop of `scripts/bench_llm.run_one` (unload, warmup, same `make_ollama_chat_fn`, same options). Write one JSONL line per message: `{model, message_id, events (model_dump(mode="json")), error, seconds, prompt_sha256}`. Support `--from-run <runs.jsonl>` to rescore saved outputs without Ollama. `prompt_sha256` = SHA-256 of `SYSTEM_PROMPT + json.dumps(FEWSHOT, ensure_ascii=False, sort_keys=True)`; refuse to combine runs with different hashes in one report.

**B2. Optional `--pipeline` view.** Also pass the extracted events through the same deterministic steps `app/services/pipeline.py` applies (meeting normalization, schedule normalization, scope/audience filter, relevance filter), without a DB, in the same order and by calling the same service functions. Report "raw LLM" and "after filters" side by side. Do not modify the services; if a function needs a session, stop and ask me instead of rewriting it.

**B3. Event alignment (per message).** Pair score = 3·[date equal] + 2·[title keys match] + 1·[time equal]. Pairs with score ≥ 2 are eligible; take the highest-scoring pair greedily, remove both, repeat (ties: gold order, then prediction order). So a correct date alone, or a matching title alone, is enough to pair — a wrong date is then scored as a wrong field, not as missed + extra. Unmatched gold → `missed_event`; unmatched prediction in an announcement → `extra_event`; any prediction in a trap → `false_event`. `merged` = message where fewer predictions than gold events were returned and every prediction was paired.

**B4. Field categories** (every paired event, all 11 fields):

| category | condition |
|---|---|
| `correct` | prediction matches a non-null gold value, or gold is null and prediction is null (also counted separately as `correct_null`) |
| `invented_ok` | prediction matches and gold has `inferred: true` (relative dates only) |
| `invented_bad` | prediction non-null and not matching; split into `wrong` (gold non-null) and `fabricated` (gold null) |
| `missed` | gold non-null, prediction null or empty |

Match rules: `event_title`, `location`, `organizer`, `target_audience` → keys; `date` → exact ISO; `time` → normalize `9:00`/`09.00` to `09:00`, then exact; `event_type` → equals `value` or is in `accept`; `description` → gold present and prediction non-empty; `language` → `"uk"`; `link` → exact after stripping whitespace and trailing `/.,;)`; `meeting_code` → keys on the whitespace-free normalized value.

**B5. Metrics.**
- `field_accuracy_all` = (correct + invented_ok) / 1540 — fixed denominator; every field of a `missed_event` counts as `missed` (strict, comparable across models).
- `field_accuracy_paired` = same over paired events only (quality given detection).
- `fabrication_rate` = fabricated / gold-null fields in paired events — **the primary hallucination metric**; `wrong_rate` = wrong / gold-non-null fields in paired events; `omission_rate` = missed / gold-non-null fields in paired events. Report `extra_event` and `false_event` counts next to them (event-level fabrication).
- Messages: announcements with ≥1 paired event; `correct_reject` (trap with empty output) and `false_event` on traps — overall, without `boundary`, and per trap subtype; individual: extracted, and with `--pipeline` dropped by the audience filter; multi-event: all gold events paired, `merged`.
- Every table in two variants: all messages, and without `ambiguous:next_weekday`.
- Stability check: headline metrics separately for 3001–3100 and 3101–3200 (different seeds and topic pools). A large gap between the halves means the result depends on the generator, not the model — flag it in `summary.md`.

**B6. Uncertainty.** Bootstrap over messages (1000 resamples, seed 20260922) → 95% CI for `field_accuracy_all`, `fabrication_rate` and the `correct_reject` share. Paired bootstrap of the difference in `field_accuracy_all` for every pair of models (same resampled messages for both), so a gap like 90.0% vs 89.7% can be reported as "not distinguishable" when the CI covers 0.

**B7. Outputs** in `reports/synthetic/<run_id>/`:
- `summary.md`: header (dataset path, number of messages, models, `prompt_sha256`, date, options); per-model table correct / invented_ok / invented_bad / missed + shares (same layout as the article's Table 1); per-field error table (like Table 2); trap table by subtype with the boundary row separate; multi-event and individual rows; CIs.
- `fields.csv` (one row per model × message × gold event × field: category, gold value, predicted value), `events.csv` (alignment results), `runs.jsonl` (raw outputs).

## Part C — review export (manual review only where needed)

**C1.** `python -m scripts.synthetic.review_export reports/synthetic/<run_id>` → `review.md` with disagreements only, grouped by message: text, gold summary, each model's differing value per field, extra/missed/false events, and a line `verdict: gold wrong / model wrong / both acceptable`. Sort messages where ≥2 models agree with each other but disagree with gold to the top — the most likely gold errors.

**C2. Cross-check with an untested model.** Run the scorer with one extra model that is NOT among the models reported in the article (check `reports/model_comparison.md`; default candidate `gemma3:12b` — if it is in the article, ask me). Its disagreements with gold go to a separate section of `review.md`: "Possible gold errors (cross-check)". This model's numbers are never reported as a result.

**C3. Corrections.** No automatic edits of the dataset. After my review, corrections live in `data/synthetic/corrections.json` (`{"<message_id>": {"<field path>": <new value>, "reason": "..."}}`), are applied at load time by the scorer, and `check_synthetic.py` must also pass on the corrected data (add a `--corrections` option to it).

## Part D — tests (pytest, no Ollama)

- Normalization and keys: apostrophes, dashes, «студенти 2-го курсу» vs key «2 курс», «Zoom» vs gold «Онлайн (Zoom)», a meeting code written with and without spaces.
- Alignment: one prediction for two gold events (merged), swapped order, correct title with a wrong date, same-day two events disambiguated by title.
- Category counting on a tiny fixture (one announcement, one trap, one individual) with hand-computed expected numbers for every metric in B5.
- Dataset sanity: 200 messages, unique ids, every text ≥ 50 words, `check_synthetic` returns no errors.
- `--from-run` rescoring on a small fake `runs.jsonl` gives the same numbers as the in-memory path.
- Full suite still green: `python -m pytest -q`.

## Constraints

- Do NOT modify `app/llm/prompts.py`, `app/llm/extractor.py`, `app/services/*`, `data/test_messages.json`, or the behaviour of existing scripts (import from them instead).
- The synthetic set is held-out: never use its messages as few-shot examples or for prompt tuning. If the prompt changes later, synthetic numbers must be recomputed and labelled with the new hash.
- Never merge real (35) and synthetic (200) results into one metric; separate files and sections.
- pathlib for paths, PowerShell syntax in all commands, `force_utf8()` for console output, UTF-8 without BOM for written files.

## Checkpoints

1. ⛔ After Part A — print the counts; stop on any mismatch.
2. After Part B — show the head of `summary.md` for (a) the fake `--from-run` fixture and (b) a real run: `python -m scripts.score_gold data/synthetic/synthetic_messages.json --models gemma3:4b --limit 5`.
3. Before finishing — `python -m pytest -q` output, the list of created/changed files, and the exact full-run commands (all models from the article + the cross-check model). Rough time: 200 messages × ~15 s ≈ 50–60 min per model on my GPU; `--limit`/`--only` for quick iterations.
