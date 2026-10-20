"""
Тести польового оцінювача синтетичного набору.

Ollama, БД і мережа не потрібні: оцінювач працює зі звичайними dict, тому
«відповідь моделі» тут — просто літерал. Перевіряємо три речі, на яких
тримаються числа для статті:
  * нормалізацію і ключі — щоб «студенти 2-го курсу» і ключ «2 курс»
    вважалися збігом, а «Zoom» покривав еталонне «Онлайн (Zoom)»;
  * зіставлення подій — щоб невірна дата коштувала ОДНОГО поля, а не пари
    «пропущено + зайве» (інакше одна помилка з'їдала б 22 поля);
  * підрахунок категорій — на крихітній вибірці з порахованими руками
    числами для кожної метрики B5.
"""

from __future__ import annotations

import json

import pytest

from app.eval import gold_scoring as gs
from scripts.synthetic import check_synthetic


# --- Допоміжні конструктори -------------------------------------------------


def gold_event(
    *,
    title="Відкрита лекція",
    title_keys=(("лекц",),),
    date="2026-10-12",
    inferred=False,
    phrase="12 жовтня",
    time_value="14:00",
    location=None,
    location_keys=None,
    organizer=None,
    organizer_keys=None,
    audience=None,
    audience_keys=None,
    event_type="lecture",
    accept=(),
    link=None,
    code=None,
    code_keys=None,
) -> dict:
    """Еталонна подія в тому самому форматі, що й у датасеті."""
    return {
        "event_title": {"value": title, "keys": [list(g) for g in title_keys]},
        "date": {"value": date, "inferred": inferred, "phrase": phrase},
        "time": {"value": time_value, "phrase": f"о {time_value}"},
        "location": {"value": location,
                     "keys": [list(g) for g in location_keys] if location_keys else None},
        "organizer": {"value": organizer,
                      "keys": [list(g) for g in organizer_keys] if organizer_keys else None},
        "target_audience": {"value": audience,
                            "keys": [list(g) for g in audience_keys] if audience_keys else None},
        "event_type": {"value": event_type, "accept": list(accept)},
        "description": {"present": True},
        "language": {"value": "uk"},
        "link": {"value": link},
        "meeting_code": {"value": code,
                         "keys": [list(g) for g in code_keys] if code_keys else None},
    }


def predicted(**overrides) -> dict:
    """Відповідь моделі: всі 11 полів, як після model_dump(mode="json")."""
    event = {
        "event_title": "Відкрита лекція",
        "date": "2026-10-12",
        "time": "14:00",
        "location": None,
        "organizer": None,
        "target_audience": None,
        "event_type": "lecture",
        "description": "Деталі заходу.",
        "language": "uk",
        "link": None,
        "meeting_code": None,
    }
    event.update(overrides)
    return event


def gold_message(events, *, kind="announcement", message_id=3001, category="single",
                 tags=()) -> gs.GoldMessage:
    return gs.GoldMessage(
        message_id=message_id, date="2026-10-05", text="текст", expect="",
        kind=kind, category=category, tags=tuple(tags),
        reaches_db=kind == "announcement", events=tuple(events),
    )


# --- Нормалізація і ключі ---------------------------------------------------


@pytest.mark.parametrize("raw, expected", [
    ("П’ятниця", "п'ятниця"),          # типографський апостроф
    ("Пʼятниця", "п'ятниця"),          # модифікатор-апостроф
    ("15–16 жовтня", "15-16 жовтня"),  # коротке тире
    ("15 — 16 жовтня", "15-16 жовтня"),  # довге тире + пробіли перед цифрою
    ("студенти 2-го курсу", "студенти 2 курсу"),
    ("  Багато   пробілів  ", "багато пробілів"),
])
def test_norm(raw, expected):
    assert gs.norm(raw) == expected


def test_norm_numero_sign_becomes_no():
    """
    NFKC розкладає «№» на «No» ДО рядка з `.replace("№", "")`, тому знак не
    зникає, а стає літерами. Поведінка симетрична (еталонні ключі проходять
    ту саму нормалізацію), тож на збіги не впливає — але фіксуємо її тестом,
    щоб ніхто не «полагодив» нормалізацію й не зсунув усі числа набору.
    """
    assert gs.norm("ауд. № 305") == "ауд. no 305"
    assert gs.field_matches("location", {"value": "ауд. № 305", "keys": [["№ 305"]]},
                            "Ауд. № 305")


def test_checker_uses_the_same_norm():
    """Одна нормалізація на проєкт: інакше набір пройде перевірку й завалить оцінку."""
    assert check_synthetic.norm is gs.norm
    assert check_synthetic.keys_ok is gs.keys_ok


def test_keys_match_ordinal_audience():
    """«студенти 2-го курсу» має закривати ключ «2 курс»."""
    field = {"value": "2 курс", "keys": [["2 курс"]]}
    assert gs.field_matches("target_audience", field, "студенти 2-го курсу")
    assert not gs.field_matches("target_audience", field, "студенти 3 курсу")


def test_keys_match_online_location():
    """Еталон «Онлайн (Zoom)» закривається будь-якою з альтернатив."""
    field = {"value": "Онлайн (Zoom)", "keys": [["онлайн", "zoom"]]}
    assert gs.field_matches("location", field, "Zoom")
    assert gs.field_matches("location", field, "онлайн")
    assert not gs.field_matches("location", field, "Ауд. 305")


def test_keys_are_and_of_ors():
    """Кожна група обов'язкова: збіг лише з однією — не збіг."""
    field = {"value": "ауд. 305 корпусу 2", "keys": [["305"], ["корпус"]]}
    assert gs.field_matches("location", field, "ауд. 305, корпус 2")
    assert not gs.field_matches("location", field, "ауд. 305")


def test_meeting_code_with_and_without_spaces():
    """Код зустрічі звіряється без пробілів: «958 7048 5650» == «95870485650»."""
    field = {"value": "ID 958 7048 5650 · код 326210",
             "keys": [["95870485650"], ["326210"]]}
    assert gs.field_matches("meeting_code", field, "ID 958 7048 5650, код 326210")
    assert gs.field_matches("meeting_code", field, "95870485650 326210")
    assert not gs.field_matches("meeting_code", field, "ID 958 7048 5650")


@pytest.mark.parametrize("raw, expected", [
    ("9:00", "09:00"), ("09:00", "09:00"), ("09.00", "09:00"),
    ("о 14:00", "14:00"), ("вечір", None), (None, None), ("25:00", None),
])
def test_normalize_clock(raw, expected):
    assert gs.normalize_clock(raw) == expected


def test_link_ignores_trailing_punctuation():
    field = {"value": "https://example.com/reg"}
    assert gs.field_matches("link", field, "https://example.com/reg).")
    assert gs.field_matches("link", field, "  https://example.com/reg  ")
    assert not gs.field_matches("link", field, "https://example.com/other")


def test_event_type_accepts_alternatives():
    field = {"value": "workshop", "accept": ["seminar"]}
    assert gs.field_matches("event_type", field, "workshop")
    assert gs.field_matches("event_type", field, "seminar")
    assert not gs.field_matches("event_type", field, "lecture")


# --- Зіставлення подій ------------------------------------------------------


def test_wrong_date_is_one_wrong_field_not_missed_plus_extra():
    """Правильна назва + невірна дата = пара (бал 2), а не втрата події."""
    gold = gold_message([gold_event()])
    score = gs.score_message(gold, [predicted(date="2026-10-20")])
    assert len(score.paired) == 1
    assert score.n_missed_events == 0 and score.n_extra_events == 0
    categories = {f.name: f.category for f in score.paired[0].fields}
    assert categories["date"] == gs.WRONG
    assert categories["event_title"] == gs.CORRECT


def test_one_prediction_for_two_gold_events_is_merged():
    """Модель злила дві події в одну: merged, і друга подія втрачена."""
    gold = gold_message([
        gold_event(date="2026-10-12"),
        gold_event(title="Семінар", title_keys=(("семінар",),), date="2026-10-13"),
    ])
    score = gs.score_message(gold, [predicted(date="2026-10-12")])
    assert score.merged is True
    assert score.n_missed_events == 1
    assert score.n_extra_events == 0


def test_swapped_order_pairs_correctly():
    """Порядок подій у відповіді значення не має."""
    gold = gold_message([
        gold_event(title="Лекція", title_keys=(("лекц",),), date="2026-10-12"),
        gold_event(title="Семінар", title_keys=(("семінар",),), date="2026-10-13"),
    ])
    score = gs.score_message(gold, [
        predicted(event_title="Семінар з методології", date="2026-10-13"),
        predicted(event_title="Лекція з ML", date="2026-10-12"),
    ])
    pairs = {(e.gold_index, e.pred_index) for e in score.paired}
    assert pairs == {(0, 1), (1, 0)}
    assert score.n_missed_events == 0 and score.n_extra_events == 0


def test_same_day_events_disambiguated_by_title():
    """Однакова дата — розводить назва: бал 5 виграє в балу 3."""
    gold = gold_message([
        gold_event(title="Лекція", title_keys=(("лекц",),), date="2026-10-12",
                   time_value=None),
        gold_event(title="Майстер-клас", title_keys=(("майстер",),),
                   date="2026-10-12", time_value=None),
    ])
    score = gs.score_message(gold, [
        predicted(event_title="Майстер-клас з Docker", date="2026-10-12", time=None),
        predicted(event_title="Лекція з ML", date="2026-10-12", time=None),
    ])
    pairs = {(e.gold_index, e.pred_index) for e in score.paired}
    assert pairs == {(0, 1), (1, 0)}


def test_prediction_on_trap_is_false_event():
    gold = gold_message([], kind="trap", category="trap:past_report")
    score = gs.score_message(gold, [predicted()])
    assert score.n_false_events == 1
    assert score.correct_reject is False


def test_empty_output_on_trap_is_correct_reject():
    gold = gold_message([], kind="trap", category="trap:poll")
    assert gs.score_message(gold, []).correct_reject is True


def test_extraction_failure_is_not_a_correct_reject():
    """Збій моделі — не «правильно промовчала»: інакше впала Ollama = 100% пасток."""
    gold = gold_message([], kind="trap", category="trap:poll")
    assert gs.score_message(gold, [], error="LLMUnavailableError").correct_reject is False


def test_unmatched_prediction_in_announcement_is_extra():
    gold = gold_message([gold_event()])
    score = gs.score_message(gold, [
        predicted(),
        predicted(event_title="Дедлайн подання тез", date="2026-11-01"),
    ])
    assert score.n_extra_events == 1
    assert len(score.paired) == 1


# --- Категорії полів --------------------------------------------------------


def test_inferred_date_is_invented_ok():
    """Дата, порахована з «завтра», — окрема категорія, а не просто correct."""
    gold = gold_message([gold_event(date="2026-10-06", inferred=True, phrase="завтра")])
    score = gs.score_message(gold, [predicted(date="2026-10-06")])
    categories = {f.name: f.category for f in score.paired[0].fields}
    assert categories["date"] == gs.INVENTED_OK


def test_fabricated_versus_wrong_versus_missed():
    gold = gold_message([gold_event(
        location="ауд. 305", location_keys=(("305",),),
        audience="3 курс", audience_keys=(("3 курс",),),
    )])
    score = gs.score_message(gold, [predicted(
        organizer="Кафедра ІТ",     # еталон null → вигадано
        location="ауд. 101",        # еталон є, не збігається → невірно
        target_audience=None,       # еталон є, модель промовчала → пропущено
    )])
    categories = {f.name: f.category for f in score.paired[0].fields}
    assert categories["organizer"] == gs.FABRICATED
    assert categories["location"] == gs.WRONG
    assert categories["target_audience"] == gs.MISSED
    assert categories["link"] == gs.CORRECT          # правильний null


def test_empty_string_counts_as_missing():
    """Порожній рядок — це не відповідь: інакше description «  » був би correct."""
    gold = gold_message([gold_event()])
    score = gs.score_message(gold, [predicted(description="   ")])
    categories = {f.name: f.category for f in score.paired[0].fields}
    assert categories["description"] == gs.MISSED


# --- Метрики B5 на крихітній вибірці ---------------------------------------


@pytest.fixture
def tiny_scores() -> list[gs.MessageScore]:
    """
    Три повідомлення з порахованими руками числами.

    Анонс: 8 correct (з них 2 правильні null), 1 wrong (date),
    1 fabricated (organizer), 1 missed (target_audience) — разом 11 полів.
    Пастка: модель вигадала подію. Індивідуальна: видобуто, у поля не йде.
    """
    announcement = gs.score_message(
        gold_message([gold_event(
            location="ауд. 305", location_keys=(("305",),),
            audience="3 курс", audience_keys=(("3 курс",),),
        )], message_id=3001),
        [predicted(
            event_title="Відкрита лекція з ML",
            date="2026-10-13",
            location="Ауд. 305",
            organizer="Кафедра ІТ",
            target_audience=None,
        )],
    )
    trap = gs.score_message(
        gold_message([], kind="trap", category="trap:past_report", message_id=3002),
        [predicted()],
    )
    individual = gs.score_message(
        gold_message([gold_event(event_type="consultation")], kind="individual",
                     category="individual", message_id=3003),
        [predicted(event_type="consultation")],
    )
    return [announcement, trap, individual]


def test_tiny_fixture_metrics(tiny_scores):
    m = gs.aggregate(tiny_scores)
    assert m["messages"] == 3
    # поля
    assert (m["correct"], m["correct_null"]) == (8, 2)
    assert m["invented_ok"] == 0
    assert (m["wrong"], m["fabricated"], m["missed"]) == (1, 1, 1)
    assert m["invented_bad"] == 2
    assert m["gold_fields_total"] == 11        # лише анонс: 1 подія × 11
    assert m["paired_fields"] == 11
    assert m["missed_event_fields"] == 0
    assert m["field_accuracy_all"] == pytest.approx(8 / 11)
    assert m["field_accuracy_paired"] == pytest.approx(8 / 11)
    # частки помилок
    assert m["paired_gold_null"] == 3          # organizer, link, meeting_code
    assert m["paired_gold_nonnull"] == 8
    assert m["fabrication_rate"] == pytest.approx(1 / 3)
    assert m["wrong_rate"] == pytest.approx(1 / 8)
    assert m["omission_rate"] == pytest.approx(1 / 8)
    # події й повідомлення
    assert (m["announcements"], m["announcements_detected"]) == (1, 1)
    assert m["detection_rate"] == pytest.approx(1.0)
    assert (m["missed_events"], m["extra_events"]) == (0, 0)
    assert (m["traps"], m["traps_clean"], m["trap_false_events"]) == (1, 0, 1)
    assert m["correct_reject_rate"] == pytest.approx(0.0)
    assert (m["individual"], m["individual_extracted"]) == (1, 1)
    assert m["multi"] == 0


def test_missed_event_fields_count_as_missed():
    """Не знайдена подія карається всіма 11 полями — інакше вигідно мовчати."""
    gold = gold_message([
        gold_event(date="2026-10-12"),
        gold_event(title="Семінар", title_keys=(("семінар",),), date="2026-10-13"),
    ])
    score = gs.score_message(gold, [predicted(date="2026-10-12")])
    m = gs.aggregate([score])
    assert m["gold_fields_total"] == 22
    assert m["missed_event_fields"] == 11
    assert m["field_accuracy_all"] == pytest.approx(11 / 22)
    assert m["field_accuracy_paired"] == pytest.approx(1.0)


def test_per_field_table_counts_missed_events(tiny_scores):
    table = gs.per_field_errors(tiny_scores)
    assert table["date"]["wrong"] == 1
    assert table["organizer"]["fabricated"] == 1
    assert table["target_audience"]["missed"] == 1
    assert table["link"]["correct_null"] == 1
    assert all(row["total"] == 1 for row in table.values())


def test_trap_breakdown_marks_boundary():
    scores = [
        gs.score_message(gold_message([], kind="trap", category="trap:boundary_exam",
                                      message_id=3010, tags=("boundary",)), []),
        gs.score_message(gold_message([], kind="trap", category="trap:poll",
                                      message_id=3011), [predicted()]),
    ]
    table = gs.trap_breakdown(scores)
    assert table["boundary_exam"] == {"traps": 1, "clean": 1, "false_events": 0,
                                      "boundary": 1}
    assert table["poll"]["false_events"] == 1
    assert gs.aggregate(gs.without_boundary(scores))["traps"] == 1


def test_without_ambiguous_drops_tagged_messages():
    scores = [
        gs.score_message(gold_message([gold_event()], message_id=3001,
                                      tags=("ambiguous:next_weekday",)), []),
        gs.score_message(gold_message([gold_event()], message_id=3002), []),
    ]
    assert len(gs.without_ambiguous(scores)) == 1


def test_halves_split_by_id():
    scores = [
        gs.score_message(gold_message([gold_event()], message_id=3050), []),
        gs.score_message(gold_message([gold_event()], message_id=3150), []),
    ]
    assert sorted(gs.by_half(scores)) == ["3001-3100", "3101-3200"]


# --- Невизначеність ---------------------------------------------------------


def test_bootstrap_ci_brackets_the_point_estimate(tiny_scores):
    cis = gs.bootstrap_cis(tiny_scores, resamples=200)
    low, high = cis["field_accuracy_all"]
    assert 0.0 <= low <= high <= 1.0


def test_bootstrap_is_reproducible(tiny_scores):
    assert gs.bootstrap_cis(tiny_scores, resamples=200) == gs.bootstrap_cis(
        tiny_scores, resamples=200
    )


def test_paired_bootstrap_of_identical_models_covers_zero(tiny_scores):
    diff, low, high = gs.paired_bootstrap_diff(tiny_scores, tiny_scores, resamples=200)
    assert diff == pytest.approx(0.0)
    assert low <= 0 <= high


# --- Правки еталона ---------------------------------------------------------


def test_apply_corrections_patches_nested_path():
    raw = [{"source_message_id": 3001, "text": "t", "date": "2026-10-05",
            "gold": {"kind": "announcement", "category": "single", "tags": [],
                     "reaches_db": True, "events": [gold_event()]}}]
    applied = gs.apply_corrections(
        raw, {"3001": {"events[0].date.value": "2026-10-19", "reason": "еталон хибний"}}
    )
    assert raw[0]["gold"]["events"][0]["date"]["value"] == "2026-10-19"
    assert "еталон хибний" in applied[0]


def test_apply_corrections_rejects_unknown_message():
    with pytest.raises(KeyError):
        gs.apply_corrections([], {"9999": {"reaches_db": False}})


# --- Датасет ----------------------------------------------------------------

DATASET = "data/synthetic/synthetic_messages.json"


@pytest.fixture(scope="module")
def dataset() -> list[dict]:
    from pathlib import Path
    return json.loads(Path(DATASET).read_text(encoding="utf-8"))


def test_dataset_size_and_ids(dataset):
    assert len(dataset) == 200
    ids = [o["source_message_id"] for o in dataset]
    assert len(set(ids)) == 200
    assert min(ids) == 3001 and max(ids) == 3200


def test_dataset_texts_are_long_enough(dataset):
    """Короткі тексти зробили б набір легшим за реальні оголошення."""
    short = [o["source_message_id"] for o in dataset if len(o["text"].split()) < 50]
    assert short == []


def test_dataset_composition(dataset):
    kinds: dict[str, int] = {}
    for item in dataset:
        kinds[item["gold"]["kind"]] = kinds.get(item["gold"]["kind"], 0) + 1
    assert kinds == {"announcement": 118, "trap": 70, "individual": 12}
    assert sum(len(o["gold"]["events"]) for o in dataset) == 152


def test_checker_finds_no_errors():
    from pathlib import Path
    _, errors = check_synthetic.check(Path(DATASET))
    assert errors == []


def test_gold_loads_and_indexes_by_id():
    gold, applied = gs.load_gold(DATASET)
    assert len(gold) == 200 and applied == []
    message = gold[3001]
    assert message.half == "3001-3100"
    assert message.kind in {"announcement", "trap", "individual"}


def test_announcement_fields_denominator_is_1540():
    """Знаменник статті: 140 подій анонсів × 11 полів."""
    gold, _ = gs.load_gold(DATASET)
    scores = [gs.score_message(m, []) for m in gold.values()]
    assert gs.aggregate(scores)["gold_fields_total"] == 1540


# --- Узгодженість шляхів --from-run і в пам'яті ------------------------------


def test_from_run_rescoring_matches_in_memory(tmp_path):
    """Перерахунок збереженого прогону має давати ТІ САМІ числа."""
    from scripts.score_gold import load_run_records, score_records

    gold = {
        3001: gold_message([gold_event(
            location="ауд. 305", location_keys=(("305",),))], message_id=3001),
        3002: gold_message([], kind="trap", category="trap:poll", message_id=3002),
    }
    records = [
        {"model": "fake", "message_id": 3001,
         "events": [predicted(organizer="Кафедра ІТ", location="Ауд. 305")],
         "error": None, "seconds": 1.5, "prompt_sha256": "abc"},
        {"model": "fake", "message_id": 3002, "events": [], "error": None,
         "seconds": 0.5, "prompt_sha256": "abc"},
    ]
    in_memory, _ = score_records(records, gold)

    path = tmp_path / "runs.jsonl"
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records),
                    encoding="utf-8")
    loaded, sha = load_run_records(path)
    from_file, _ = score_records(loaded, gold)

    assert sha == "abc"
    assert gs.aggregate(from_file["fake"]) == gs.aggregate(in_memory["fake"])
    # Еталон лишає null у organizer, target_audience, link, meeting_code;
    # модель вигадала лише organizer.
    assert gs.aggregate(from_file["fake"])["fabrication_rate"] == pytest.approx(1 / 4)


def test_from_run_refuses_mixed_prompt_hashes(tmp_path):
    """Два промти в одному файлі — це дві різні системи, звіт зводити не можна."""
    from scripts.score_gold import load_run_records

    path = tmp_path / "runs.jsonl"
    path.write_text(
        json.dumps({"model": "a", "message_id": 1, "events": [], "prompt_sha256": "x"})
        + "\n"
        + json.dumps({"model": "a", "message_id": 2, "events": [], "prompt_sha256": "y"})
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(SystemExit):
        load_run_records(path)


def test_prompt_hash_is_stable():
    from scripts.score_gold import prompt_hash

    assert prompt_hash() == prompt_hash()
    assert len(prompt_hash()) == 64
