"""
Польова оцінка видобування за еталоном синтетичного набору.

Чиста логіка без мережі, БД і Ollama: на вхід — еталон (`gold`) і те, що
повернула модель (звичайні dict, як після `model_dump(mode="json")`), на
вихід — категорія КОЖНОГО поля КОЖНОЇ події та зведені метрики.

Навіщо окремий модуль замість `app/eval/grounding.py`: grounding відповідає
на питання «чи підтверджується значення текстом» (без еталона, евристиками),
а тут є справжній ground truth — набір побудовано «спершу еталон, потім
текст», тож кожне поле можна звірити точно.

Чому dict, а не `ExtractedEvent`: так шлях «щойно з моделі» і шлях
`--from-run` (перерахунок збереженого `runs.jsonl`) проходять через ОДИН і
той самий код, і збіг їхніх чисел перевіряється тестом, а не на слово.
Побічно це знімає залежність від pydantic, тому `check_synthetic.py` імпортує
звідси нормалізацію й не тягне за собою половину застосунку.

Головні поняття:
  * зіставлення (`align_events`) — яку передбачену подію з якою еталонною
    порівнювати. Рахується балом 3·дата + 2·назва + 1·час, щоб невірна дата
    каралася як одне помилкове ПОЛЕ, а не як «пропущена подія + зайва подія»
    (інакше одна помилка коштувала б 22 поля);
  * категорія поля — correct / invented_ok / wrong / fabricated / missed;
  * `fabricated` (еталон null, модель щось написала) — головна метрика
    галюцинацій: саме її адміністратор ловить руками на модерації.
"""

from __future__ import annotations

import json
import random
import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

# 11 полів схеми ExtractedEvent у порядку, в якому вони йдуть у звітах.
FIELDS: tuple[str, ...] = (
    "event_title", "date", "time", "location", "organizer", "target_audience",
    "event_type", "description", "language", "link", "meeting_code",
)
# Поля, які звіряються за ключами (AND груп, OR альтернатив усередині групи):
# модель формулює назву й місце своїми словами, точний збіг рядків тут
# карав би за синоніми, а не за помилки.
KEY_FIELDS: frozenset[str] = frozenset(
    {"event_title", "location", "organizer", "target_audience"}
)

# Категорії поля. invented_ok виділено окремо від correct навмисно: це
# значення, якого в тексті НЕМАЄ дослівно (дата з «наступної п'ятниці»), і
# модель мусила його обчислити. Рахувати його просто як correct — втратити
# відповідь на питання «чи вміє модель у відносні дати».
CORRECT = "correct"
INVENTED_OK = "invented_ok"
WRONG = "wrong"
FABRICATED = "fabricated"
MISSED = "missed"

# Статуси зіставлення події.
PAIRED = "paired"
MISSED_EVENT = "missed_event"
EXTRA_EVENT = "extra_event"
FALSE_EVENT = "false_event"

AMBIGUOUS_TAG = "ambiguous:next_weekday"
BOUNDARY_TAG = "boundary"

# Поріг балу, з якого пару взагалі розглядаємо (див. `align_events`).
PAIR_THRESHOLD = 2


# --- Нормалізація -----------------------------------------------------------
# ВАЖЛИВО: `norm` тут — єдине джерело правди. `check_synthetic.py` імпортує
# саме її, тож еталон перевіряється й оцінюється однаковими правилами за
# побудовою, а не за обіцянкою тримати дві копії синхронними.

def norm(s: str) -> str:
    """Регістр, апострофи, тире, «2-го» → «2», схлопнуті пробіли."""
    s = unicodedata.normalize("NFKC", s).casefold()
    s = re.sub(r"[’ʼ`´]", "'", s)
    s = re.sub(r"[–—‑−]", "-", s)
    s = s.replace("№", "")
    s = re.sub(r"(\d)-(?:го|й|ого|му)\b", r"\1", s)
    s = re.sub(r"\s*-\s*(?=\d)", "-", s)
    return re.sub(r"\s+", " ", s).strip()


def flat(s: str) -> str:
    """`norm` без жодного пробілу — для кодів зустрічі («958 7048 5650»)."""
    return re.sub(r"\s+", "", norm(s))


def keys_ok(keys: Sequence[Sequence[str]], value_n: str) -> bool:
    """keys = [[альтернатива, ...], ...]: кожна група (І) хоч однією (АБО)."""
    return all(any(norm(alt) in value_n for alt in group) for group in keys)


def keys_ok_flat(keys: Sequence[Sequence[str]], value_flat: str) -> bool:
    """Те саме, але порівняння без пробілів з обох боків."""
    return all(any(flat(alt) in value_flat for alt in group) for group in keys)


_CLOCK_RE = re.compile(r"\b(\d{1,2})[:.](\d{2})\b")


def normalize_clock(value: object) -> str | None:
    """«9:00», «09.00», «о 14:00» → «09:00». Нерозпізнане — None."""
    if value is None:
        return None
    match = _CLOCK_RE.search(str(value))
    if match is None:
        return None
    hour, minute = int(match.group(1)), match.group(2)
    if hour > 23 or int(minute) > 59:
        return None
    return f"{hour:02d}:{minute}"


def normalize_link(value: object) -> str | None:
    """Прибирає пробіли і хвостову пунктуацію: «…/reg).» і «…/reg» — одне."""
    if value is None:
        return None
    text = str(value).strip().rstrip("/.,;)")
    return text or None


def is_empty(value: object) -> bool:
    """None, порожній рядок або самі пробіли — все це «модель не відповіла»."""
    if value is None:
        return True
    return not str(value).strip()


# --- Еталон -----------------------------------------------------------------


@dataclass(frozen=True)
class GoldMessage:
    """Один рядок датасету: текст, дата публікації і розмітка."""

    message_id: int
    date: str
    text: str
    expect: str
    kind: str               # announcement | trap | individual
    category: str           # single | multi:<kind> | trap:<subtype> | ...
    tags: tuple[str, ...]
    reaches_db: bool
    events: tuple[dict, ...]

    @property
    def half(self) -> str:
        """Половина набору: згенеровані різними сідами й пулами тем."""
        return "3001-3100" if self.message_id <= 3100 else "3101-3200"

    @property
    def is_ambiguous(self) -> bool:
        return AMBIGUOUS_TAG in self.tags

    @property
    def is_boundary(self) -> bool:
        return BOUNDARY_TAG in self.tags

    @property
    def subtype(self) -> str:
        """Підтип пастки («past_report») або сама категорія."""
        return self.category.split(":", 1)[1] if ":" in self.category else self.category


_PATH_PART = re.compile(r"^([A-Za-z_][A-Za-z_0-9]*)(?:\[(\d+)\])?$")


def _apply_path(target: dict, path: str, value: object) -> None:
    """
    Записує значення за шляхом «events[0].date.value».

    Використовується тільки для `data/synthetic/corrections.json`: правки
    еталона після ручного розгляду живуть окремим файлом, а не редагуванням
    датасету, щоб первинну розмітку завжди можна було відтворити.
    """
    parts = path.split(".")
    if parts and parts[0] == "gold":     # дозволяємо і повний шлях, і скорочений
        parts = parts[1:]
    if not parts:
        raise ValueError(f"порожній шлях правки: {path!r}")
    node: object = target
    for part in parts[:-1]:
        node = _descend(node, part, path)
    last = _PATH_PART.match(parts[-1])
    if last is None:
        raise ValueError(f"нерозбірлива ланка «{parts[-1]}» у шляху {path!r}")
    name, index = last.group(1), last.group(2)
    if index is None:
        node[name] = value                        # type: ignore[index]
    else:
        node[name][int(index)] = value            # type: ignore[index]


def _descend(node: object, part: str, path: str) -> object:
    match = _PATH_PART.match(part)
    if match is None:
        raise ValueError(f"нерозбірлива ланка «{part}» у шляху {path!r}")
    name, index = match.group(1), match.group(2)
    node = node[name]                             # type: ignore[index]
    if index is not None:
        node = node[int(index)]                   # type: ignore[index]
    return node


def apply_corrections(raw: list[dict], corrections: dict) -> list[str]:
    """
    Накладає правки еталона на сирі записи датасету. Повертає опис змін.

    Формат: {"3043": {"events[0].date.value": "2026-10-19", "reason": "..."}}.
    Ключ `reason` — для людини, у дані не потрапляє.
    """
    by_id = {int(item["source_message_id"]): item for item in raw}
    applied: list[str] = []
    for key, patch in corrections.items():
        message_id = int(key)
        item = by_id.get(message_id)
        if item is None:
            raise KeyError(f"правка для невідомого повідомлення {message_id}")
        reason = str(patch.get("reason", "")).strip()
        for path, value in patch.items():
            if path == "reason":
                continue
            _apply_path(item["gold"], path, value)
            applied.append(f"{message_id}: {path} → {value!r}"
                           + (f" ({reason})" if reason else ""))
    return applied


def load_gold(
    dataset: str | Path, corrections: str | Path | None = None
) -> tuple[dict[int, GoldMessage], list[str]]:
    """
    Читає датасет окремо від `load_messages` і індексує еталон за id.

    Навмисно не чіпає `ExportMessage`: текст і дату для видобування бере
    штатний завантажувач, а `gold` — цей модуль. Так видобування лишається
    побітово таким самим, як у `scripts.bench_llm`.
    """
    raw = json.loads(Path(dataset).read_text(encoding="utf-8"))
    applied: list[str] = []
    if corrections is not None:
        path = Path(corrections)
        if path.exists():
            applied = apply_corrections(raw, json.loads(path.read_text(encoding="utf-8")))
    result: dict[int, GoldMessage] = {}
    for item in raw:
        gold = item["gold"]
        result[int(item["source_message_id"])] = GoldMessage(
            message_id=int(item["source_message_id"]),
            date=str(item["date"]),
            text=str(item["text"]),
            expect=str(item.get("_expect", "")),
            kind=str(gold["kind"]),
            category=str(gold["category"]),
            tags=tuple(gold.get("tags", ())),
            reaches_db=bool(gold["reaches_db"]),
            events=tuple(gold["events"]),
        )
    return result, applied


# --- Порівняння полів -------------------------------------------------------


def gold_field_value(name: str, gold_field: dict) -> object | None:
    """Еталонне значення поля або None, якщо в еталоні його немає."""
    if name == "description":
        # У description немає «правильного тексту» — є лише вимога, щоб
        # деталі з повідомлення не загубилися. Тому в еталоні {present: true}.
        return True if gold_field.get("present") else None
    return gold_field.get("value")


def field_matches(name: str, gold_field: dict, predicted: object) -> bool:
    """Чи зараховується передбачене значення як збіг з еталоном."""
    if is_empty(predicted):
        return False
    if name in KEY_FIELDS:
        keys = gold_field.get("keys")
        value_n = norm(str(predicted))
        if not keys:   # запасний шлях: ключів немає, звіряємо саме значення
            return norm(str(gold_field.get("value", ""))) in value_n
        return keys_ok(keys, value_n)
    if name == "date":
        return str(predicted).strip() == gold_field.get("value")
    if name == "time":
        return normalize_clock(predicted) == gold_field.get("value")
    if name == "event_type":
        value = str(predicted).strip()
        return value == gold_field.get("value") or value in (gold_field.get("accept") or ())
    if name == "description":
        return True   # не порожнє (перевірено вище) і еталон вимагає деталей
    if name == "language":
        return str(predicted).strip() == gold_field.get("value")
    if name == "link":
        return normalize_link(predicted) == normalize_link(gold_field.get("value"))
    if name == "meeting_code":
        keys = gold_field.get("keys")
        if not keys:
            return flat(str(gold_field.get("value", ""))) in flat(str(predicted))
        return keys_ok_flat(keys, flat(str(predicted)))
    raise ValueError(f"невідоме поле {name!r}")


@dataclass(frozen=True)
class FieldScore:
    """Вердикт по одному полю однієї події."""

    name: str
    category: str
    gold_null: bool
    gold_repr: str
    pred_repr: str

    @property
    def correct_null(self) -> bool:
        """Правильно поставлений null — окремо від «вгадав значення»."""
        return self.category == CORRECT and self.gold_null


def _repr_value(value: object) -> str:
    if value is None:
        return ""
    return str(value)


def score_field(name: str, gold_field: dict, predicted_event: dict) -> FieldScore:
    """Категорія поля за таблицею B4."""
    predicted = predicted_event.get(name)
    gold_value = gold_field_value(name, gold_field)
    gold_null = gold_value is None
    pred_present = not is_empty(predicted)
    gold_repr = "є деталі" if name == "description" and gold_value else _repr_value(gold_value)

    if gold_null and not pred_present:
        category = CORRECT                       # правильний null
    elif gold_null:
        category = FABRICATED                    # вигадка на порожньому місці
    elif not pred_present:
        category = MISSED                        # еталон є, модель промовчала
    elif field_matches(name, gold_field, predicted):
        # Дати, пораховані з відносної вказівки, виділяємо окремо: дослівно
        # такого значення в тексті немає, модель його обчислила.
        category = INVENTED_OK if (name == "date" and gold_field.get("inferred")) else CORRECT
    else:
        category = WRONG
    return FieldScore(
        name=name,
        category=category,
        gold_null=gold_null,
        gold_repr=gold_repr,
        pred_repr=_repr_value(predicted),
    )


# --- Зіставлення подій ------------------------------------------------------


def pair_score(gold_event: dict, predicted: dict) -> int:
    """
    3·дата + 2·назва + 1·час.

    Вага дати найбільша: у межах одного повідомлення саме вона надійно
    розрізняє події (дводенний захід, дві сесії тренінгу). Назва одна дає 2,
    тобто теж проходить поріг: коли модель помилилася датою, це має коштувати
    одного поля, а не пари «пропущено + зайве».
    """
    score = 0
    gold_date = gold_event["date"].get("value")
    predicted_date = predicted.get("date")
    if gold_date and not is_empty(predicted_date) and str(predicted_date).strip() == gold_date:
        score += 3
    title = predicted.get("event_title")
    if not is_empty(title) and field_matches("event_title", gold_event["event_title"], title):
        score += 2
    gold_time = gold_event["time"].get("value")
    if gold_time and normalize_clock(predicted.get("time")) == gold_time:
        score += 1
    return score


@dataclass
class EventScore:
    """Результат зіставлення однієї пари (або одинокої події)."""

    status: str
    gold_index: int | None = None
    pred_index: int | None = None
    score: int = 0
    fields: tuple[FieldScore, ...] = ()
    gold_title: str = ""
    pred_title: str = ""
    pred_event: dict | None = None


def align_events(gold_events: Sequence[dict], predictions: Sequence[dict],
                 *, is_trap: bool) -> list[EventScore]:
    """
    Жадібне зіставлення: щоразу беремо найкращу пару з балом ≥ 2.

    Нічия розв'язується порядком еталона, далі порядком передбачень — щоб
    результат не залежав від порядку перебору і відтворювався побітово.
    """
    candidates = []
    for gi, gold_event in enumerate(gold_events):
        for pi, predicted in enumerate(predictions):
            score = pair_score(gold_event, predicted)
            if score >= PAIR_THRESHOLD:
                candidates.append((-score, gi, pi))
    candidates.sort()

    used_gold: set[int] = set()
    used_pred: set[int] = set()
    pairs: list[tuple[int, int, int]] = []
    for neg_score, gi, pi in candidates:
        if gi in used_gold or pi in used_pred:
            continue
        used_gold.add(gi)
        used_pred.add(pi)
        pairs.append((gi, pi, -neg_score))

    results: list[EventScore] = []
    for gi, pi, score in sorted(pairs):
        gold_event, predicted = gold_events[gi], predictions[pi]
        results.append(EventScore(
            status=PAIRED,
            gold_index=gi,
            pred_index=pi,
            score=score,
            fields=tuple(score_field(name, gold_event[name], predicted) for name in FIELDS),
            gold_title=str(gold_event["event_title"].get("value", "")),
            pred_title=str(predicted.get("event_title", "")),
            pred_event=predicted,
        ))
    for gi, gold_event in enumerate(gold_events):
        if gi not in used_gold:
            results.append(EventScore(
                status=MISSED_EVENT,
                gold_index=gi,
                gold_title=str(gold_event["event_title"].get("value", "")),
            ))
    for pi, predicted in enumerate(predictions):
        if pi not in used_pred:
            # У пастці еталонних подій немає взагалі, тому будь-яка видобута
            # подія — вигадана (false_event), а не «зайва до правильних».
            results.append(EventScore(
                status=FALSE_EVENT if is_trap else EXTRA_EVENT,
                pred_index=pi,
                pred_title=str(predicted.get("event_title", "")),
                pred_event=predicted,
            ))
    return results


# --- Оцінка повідомлення ----------------------------------------------------


@dataclass
class PipelineView:
    """
    Що лишилося б після детермінованих фільтрів пайплайна.

    `kept_events` — уже нормалізовані події (посилання й код зустрічі по
    своїх полях, перерахована відносна дата). Їх оцінюють другим заходом:
    у звіті «сира LLM» і «після фільтрів» стоять поруч, бо частину помилок
    моделі код виправляє, а частину — ні, і стаття розрізняє ці два числа.
    """

    kept_events: list[dict] = field(default_factory=list)
    dropped: list[tuple[str, str]] = field(default_factory=list)   # (назва, причина)
    prefiltered: str | None = None       # причина відсіву ДО виклику моделі

    @property
    def kept(self) -> int:
        return len(self.kept_events)


@dataclass
class MessageScore:
    """Усе, що ми знаємо про одне повідомлення після оцінки."""

    message_id: int
    kind: str
    category: str
    subtype: str
    tags: tuple[str, ...]
    half: str
    n_gold: int
    n_pred: int
    events: list[EventScore]
    error: str | None = None
    seconds: float | None = None
    pipeline: PipelineView | None = None

    @property
    def paired(self) -> list[EventScore]:
        return [e for e in self.events if e.status == PAIRED]

    @property
    def n_missed_events(self) -> int:
        return sum(1 for e in self.events if e.status == MISSED_EVENT)

    @property
    def n_extra_events(self) -> int:
        return sum(1 for e in self.events if e.status == EXTRA_EVENT)

    @property
    def n_false_events(self) -> int:
        return sum(1 for e in self.events if e.status == FALSE_EVENT)

    @property
    def detected(self) -> bool:
        """Анонс, у якому знайдено хоч одну еталонну подію."""
        return bool(self.paired)

    @property
    def all_paired(self) -> bool:
        return self.n_gold > 0 and self.n_missed_events == 0

    @property
    def merged(self) -> bool:
        """Кілька подій злиті в одну: передбачень менше, і всі зіставлені."""
        return (
            self.n_gold > 1
            and self.n_pred < self.n_gold
            and self.n_pred > 0
            and self.n_extra_events == 0
            and self.n_false_events == 0
        )

    @property
    def correct_reject(self) -> bool:
        """Пастка, на якій модель нічого не вигадала."""
        return self.kind == "trap" and self.n_pred == 0 and self.error is None

    @property
    def is_ambiguous(self) -> bool:
        return AMBIGUOUS_TAG in self.tags

    @property
    def is_boundary(self) -> bool:
        return BOUNDARY_TAG in self.tags


def score_message(
    gold: GoldMessage,
    predictions: Sequence[dict],
    *,
    error: str | None = None,
    seconds: float | None = None,
    pipeline: PipelineView | None = None,
) -> MessageScore:
    """Зіставляє події й розкладає поля по категоріях для одного повідомлення."""
    predictions = list(predictions)
    events = align_events(gold.events, predictions, is_trap=gold.kind == "trap")
    return MessageScore(
        message_id=gold.message_id,
        kind=gold.kind,
        category=gold.category,
        subtype=gold.subtype,
        tags=gold.tags,
        half=gold.half,
        n_gold=len(gold.events),
        n_pred=len(predictions),
        events=events,
        error=error,
        seconds=seconds,
        pipeline=pipeline,
    )


# --- Лічильники й метрики ---------------------------------------------------

# Порядок важливий: на цих полях тримається і додавання, і бутстреп.
COUNTER_FIELDS: tuple[str, ...] = (
    "correct", "correct_null", "invented_ok", "wrong", "fabricated", "missed",
    "paired_fields", "paired_gold_null", "paired_gold_nonnull",
    "missed_event_fields", "gold_fields_total",
    "announcements", "announcements_detected",
    "traps", "traps_clean", "trap_false_events",
    "individual", "individual_extracted", "individual_dropped",
    "multi", "multi_all_paired", "multi_merged",
    "extra_events", "missed_events", "errors", "messages",
)


@dataclass
class Counters:
    """Адитивні лічильники одного повідомлення (або суми за вибіркою)."""

    correct: int = 0
    correct_null: int = 0
    invented_ok: int = 0
    wrong: int = 0
    fabricated: int = 0
    missed: int = 0
    paired_fields: int = 0
    paired_gold_null: int = 0
    paired_gold_nonnull: int = 0
    missed_event_fields: int = 0
    gold_fields_total: int = 0
    announcements: int = 0
    announcements_detected: int = 0
    traps: int = 0
    traps_clean: int = 0
    trap_false_events: int = 0
    individual: int = 0
    individual_extracted: int = 0
    individual_dropped: int = 0
    multi: int = 0
    multi_all_paired: int = 0
    multi_merged: int = 0
    extra_events: int = 0
    missed_events: int = 0
    errors: int = 0
    messages: int = 0

    def as_tuple(self) -> tuple[int, ...]:
        return tuple(getattr(self, name) for name in COUNTER_FIELDS)

    @classmethod
    def from_tuple(cls, values: Sequence[int]) -> Counters:
        return cls(**dict(zip(COUNTER_FIELDS, values, strict=True)))

    def __add__(self, other: Counters) -> Counters:
        return Counters.from_tuple(
            [a + b for a, b in zip(self.as_tuple(), other.as_tuple(), strict=True)]
        )


def message_counters(score: MessageScore) -> Counters:
    """
    Лічильники одного повідомлення.

    Польові метрики рахуються ЛИШЕ по анонсах: знаменник статті (1540 =
    140 подій × 11 полів) — це поля анонсів. Індивідуальні консультації
    в нього не входять, бо їх завдання — не доїхати до БД.
    """
    counters = Counters(messages=1, errors=int(score.error is not None))
    if score.kind == "announcement":
        counters.announcements = 1
        counters.announcements_detected = int(score.detected)
        counters.gold_fields_total = score.n_gold * len(FIELDS)
        counters.missed_events = score.n_missed_events
        counters.missed_event_fields = score.n_missed_events * len(FIELDS)
        counters.extra_events = score.n_extra_events
        for event in score.paired:
            counters.paired_fields += len(event.fields)
            for field_score in event.fields:
                if field_score.gold_null:
                    counters.paired_gold_null += 1
                else:
                    counters.paired_gold_nonnull += 1
                if field_score.category == CORRECT:
                    counters.correct += 1
                    counters.correct_null += int(field_score.gold_null)
                elif field_score.category == INVENTED_OK:
                    counters.invented_ok += 1
                elif field_score.category == WRONG:
                    counters.wrong += 1
                elif field_score.category == FABRICATED:
                    counters.fabricated += 1
                elif field_score.category == MISSED:
                    counters.missed += 1
        if score.n_gold > 1:
            counters.multi = 1
            counters.multi_all_paired = int(score.all_paired)
            counters.multi_merged = int(score.merged)
    elif score.kind == "trap":
        counters.traps = 1
        counters.traps_clean = int(score.correct_reject)
        counters.trap_false_events = score.n_false_events
    elif score.kind == "individual":
        counters.individual = 1
        counters.individual_extracted = int(score.n_pred > 0)
        if score.pipeline is not None:
            # Відсіяти індивідуальну консультацію має audience.py, а не модель.
            counters.individual_dropped = int(score.n_pred > 0 and score.pipeline.kept == 0)
    return counters


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def metrics_from_counters(c: Counters) -> dict[str, float | int | None]:
    """Зведені метрики B5 з підсумованих лічильників."""
    correct_total = c.correct + c.invented_ok
    return {
        "messages": c.messages,
        "errors": c.errors,
        "field_accuracy_all": _ratio(correct_total, c.gold_fields_total),
        "field_accuracy_paired": _ratio(correct_total, c.paired_fields),
        "fabrication_rate": _ratio(c.fabricated, c.paired_gold_null),
        "wrong_rate": _ratio(c.wrong, c.paired_gold_nonnull),
        "omission_rate": _ratio(c.missed, c.paired_gold_nonnull),
        "correct": c.correct,
        "correct_null": c.correct_null,
        "invented_ok": c.invented_ok,
        "invented_bad": c.wrong + c.fabricated,
        "wrong": c.wrong,
        "fabricated": c.fabricated,
        "missed": c.missed,
        "gold_fields_total": c.gold_fields_total,
        "paired_fields": c.paired_fields,
        "paired_gold_null": c.paired_gold_null,
        "paired_gold_nonnull": c.paired_gold_nonnull,
        "missed_event_fields": c.missed_event_fields,
        "announcements": c.announcements,
        "announcements_detected": c.announcements_detected,
        "detection_rate": _ratio(c.announcements_detected, c.announcements),
        "missed_events": c.missed_events,
        "extra_events": c.extra_events,
        "traps": c.traps,
        "traps_clean": c.traps_clean,
        "correct_reject_rate": _ratio(c.traps_clean, c.traps),
        "trap_false_events": c.trap_false_events,
        "individual": c.individual,
        "individual_extracted": c.individual_extracted,
        "individual_dropped": c.individual_dropped,
        "multi": c.multi,
        "multi_all_paired": c.multi_all_paired,
        "multi_merged": c.multi_merged,
    }


def aggregate(scores: Iterable[MessageScore]) -> dict[str, float | int | None]:
    """Метрики по довільній підмножині повідомлень."""
    total = Counters()
    for score in scores:
        total = total + message_counters(score)
    return metrics_from_counters(total)


def per_field_errors(scores: Iterable[MessageScore]) -> dict[str, dict[str, int]]:
    """Розклад по кожному з 11 полів — таблиця 2 статті."""
    table = {
        name: {CORRECT: 0, INVENTED_OK: 0, WRONG: 0, FABRICATED: 0, MISSED: 0,
               "correct_null": 0, "total": 0}
        for name in FIELDS
    }
    for score in scores:
        if score.kind != "announcement":
            continue
        for event in score.paired:
            for field_score in event.fields:
                row = table[field_score.name]
                row[field_score.category] += 1
                row["correct_null"] += int(field_score.correct_null)
                row["total"] += 1
        # Поля пропущеної події — теж пропущені: інакше модель, яка знайшла
        # лише найпростіші події, виглядала б точнішою за ту, що знайшла всі.
        for event in score.events:
            if event.status == MISSED_EVENT:
                for name in FIELDS:
                    table[name][MISSED] += 1
                    table[name]["total"] += 1
    return table


def trap_breakdown(scores: Iterable[MessageScore]) -> dict[str, dict[str, int]]:
    """Пастки за підтипами: скільки чисто, скільки вигаданих подій."""
    table: dict[str, dict[str, int]] = {}
    for score in scores:
        if score.kind != "trap":
            continue
        row = table.setdefault(
            score.subtype, {"traps": 0, "clean": 0, "false_events": 0, "boundary": 0}
        )
        row["traps"] += 1
        row["clean"] += int(score.correct_reject)
        row["false_events"] += score.n_false_events
        row["boundary"] += int(score.is_boundary)
    return table


# --- Невизначеність ---------------------------------------------------------

BOOTSTRAP_SEED = 20260922
BOOTSTRAP_RESAMPLES = 1000

# Метрики, для яких рахуємо довірчі інтервали (B6).
CI_METRICS = ("field_accuracy_all", "fabrication_rate", "correct_reject_rate")


def _resample_indices(n: int, resamples: int, seed: int) -> list[list[int]]:
    """Один і той самий набір вибірок для всіх моделей — умова парного тесту."""
    rng = random.Random(seed)
    return [[rng.randrange(n) for _ in range(n)] for _ in range(resamples)]


def _percentile(values: list[float], q: float) -> float:
    """Лінійна інтерполяція між сусідами — як numpy, але без numpy."""
    if not values:
        raise ValueError("порожня вибірка")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = q * (len(ordered) - 1)
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def bootstrap_cis(
    scores: Sequence[MessageScore],
    metrics: Sequence[str] = CI_METRICS,
    *,
    resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
    indices: Sequence[Sequence[int]] | None = None,
) -> dict[str, tuple[float, float]]:
    """
    95% ДІ перцентильним бутстрепом ПО ПОВІДОМЛЕННЯХ.

    Ресемплюємо повідомлення, а не поля: поля одного повідомлення корельовані
    (одна невірна дата тягне за собою і час, і опис), і бутстреп по полях
    дав би надто вузький інтервал.
    """
    if not scores:
        return {}
    counters = [message_counters(s).as_tuple() for s in scores]
    draws = list(indices) if indices is not None else _resample_indices(
        len(scores), resamples, seed
    )
    samples: dict[str, list[float]] = {name: [] for name in metrics}
    for draw in draws:
        total = [0] * len(COUNTER_FIELDS)
        for i in draw:
            row = counters[i]
            for k in range(len(total)):
                total[k] += row[k]
        values = metrics_from_counters(Counters.from_tuple(total))
        for name in metrics:
            value = values[name]
            if value is not None:
                samples[name].append(float(value))
    return {
        name: (_percentile(values, 0.025), _percentile(values, 0.975))
        for name, values in samples.items() if values
    }


def paired_bootstrap_diff(
    left: Sequence[MessageScore],
    right: Sequence[MessageScore],
    metric: str = "field_accuracy_all",
    *,
    resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> tuple[float, float, float]:
    """
    ДІ для РІЗНИЦІ метрики двох моделей на одних і тих самих вибірках.

    Повертає (різниця, низ, верх). Якщо інтервал накриває 0, різниця на цьому
    наборі не розрізняється — саме так і треба писати в статті замість
    «90.0% краще за 89.7%». Порядок повідомлень вирівнюється за id.
    """
    left_by_id = {s.message_id: s for s in left}
    right_by_id = {s.message_id: s for s in right}
    common = sorted(set(left_by_id) & set(right_by_id))
    if not common:
        raise ValueError("моделі не мають спільних повідомлень")
    left_counters = [message_counters(left_by_id[i]).as_tuple() for i in common]
    right_counters = [message_counters(right_by_id[i]).as_tuple() for i in common]

    def value_of(counters: list[tuple[int, ...]], draw: Sequence[int]) -> float | None:
        total = [0] * len(COUNTER_FIELDS)
        for i in draw:
            row = counters[i]
            for k in range(len(total)):
                total[k] += row[k]
        return metrics_from_counters(Counters.from_tuple(total))[metric]  # type: ignore[return-value]

    base_left = value_of(left_counters, range(len(common)))
    base_right = value_of(right_counters, range(len(common)))
    observed = (base_left or 0.0) - (base_right or 0.0)

    diffs: list[float] = []
    for draw in _resample_indices(len(common), resamples, seed):
        a, b = value_of(left_counters, draw), value_of(right_counters, draw)
        if a is not None and b is not None:
            diffs.append(a - b)
    if not diffs:
        return observed, observed, observed
    return observed, _percentile(diffs, 0.025), _percentile(diffs, 0.975)


# --- Зрізи ------------------------------------------------------------------


def without_ambiguous(scores: Iterable[MessageScore]) -> list[MessageScore]:
    """Без 5 повідомлень, де «наступної п'ятниці» читається двояко."""
    return [s for s in scores if not s.is_ambiguous]


def without_boundary(scores: Iterable[MessageScore]) -> list[MessageScore]:
    """Без пасток, чия «пастковість» тримається на спірній угоді календаря."""
    return [s for s in scores if not s.is_boundary]


def by_half(scores: Iterable[MessageScore]) -> dict[str, list[MessageScore]]:
    """Дві половини набору: різні сіди й пули тем — перевірка на генератор."""
    halves: dict[str, list[MessageScore]] = {}
    for score in scores:
        halves.setdefault(score.half, []).append(score)
    return dict(sorted(halves.items()))
