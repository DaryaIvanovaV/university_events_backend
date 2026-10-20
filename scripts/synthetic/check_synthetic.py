# -*- coding: utf-8 -*-
"""
Перевірка синтетичного набору data/synthetic/synthetic_messages.json.

Еталон (блок gold) не має розходитися з текстом повідомлення. Скрипт
перевіряє це детерміновано, без моделі:
  * дата події перераховується з фрази в тексті (gold.date.phrase) і дати
    публікації за правилами 1-7 системного промта;
  * фрази дати й часу, посилання, код зустрічі та ключі місця, організатора,
    аудиторії і назви справді присутні в тексті;
  * у подій із відносною датою в тексті немає явних дат; у подій без часу
    в тексті немає часу; дні тижня поряд із датами правильні;
  * пастки: events порожні, _expect починається з «ПАСТКА»;
  * текст не коротший за 50 слів, без російських літер, id унікальні.

Запуск:  python -m scripts.synthetic.check_synthetic [шлях] [--corrections файл]
Код повернення 1, якщо знайдено хоч одну помилку.

`--corrections` накладає правки еталона (data/synthetic/corrections.json)
перед перевіркою: виправлений набір зобов'язаний лишатися валідним, інакше
ручна правка тихо зіпсувала б розмітку.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

# Нормалізація живе в оцінювачі й імпортується сюди, а не дублюється:
# перевірка еталона і звірка відповідей моделі зобов'язані розуміти
# «2-го курсу» однаково, інакше набір пройде перевірку й завалить оцінку.
from app.eval.gold_scoring import apply_corrections, keys_ok, norm

DEFAULT = Path("data/synthetic/synthetic_messages.json")
MONTHS = ["січня", "лютого", "березня", "квітня", "травня", "червня",
          "липня", "серпня", "вересня", "жовтня", "листопада", "грудня"]
M_IDX = {m: i + 1 for i, m in enumerate(MONTHS)}
WD_ACC = {"понеділок": 0, "вівторок": 1, "середу": 2, "четвер": 3, "п'ятницю": 4,
          "суботу": 5, "неділю": 6}
WD_NEXT = {"наступного понеділка": 0, "наступного вівторка": 1, "наступної середи": 2,
           "наступного четверга": 3, "наступної п'ятниці": 4, "наступної суботи": 5}
WD_STEMS = ["понеділ", "вівтор", "серед", "четвер", "п'ятниц", "субот", "неділ"]
WD_THIS = {"цього понеділка": 0, "цього вівторка": 1, "цієї середи": 2, "цього четверга": 3,
           "цієї п'ятниці": 4, "цієї суботи": 5}
WD_NEAREST = {"у найближчий понеділок": 0, "у найближчий вівторок": 1, "у найближчу середу": 2,
              "у найближчий четвер": 3, "у найближчу п'ятницю": 4, "у найближчу суботу": 5}
FIELDS = ["event_title", "date", "time", "location", "organizer", "target_audience",
          "event_type", "description", "language", "link", "meeting_code"]
EVENT_TYPES = {"conference", "lecture", "webinar", "seminar", "workshop", "hackathon",
               "deadline", "exam", "consultation", "other"}


def bare_weekday(ref, w):  # правило 3: найближчий такий день строго після дати публікації
    return ref + dt.timedelta(days=(w - ref.weekday()) % 7 or 7)


def with_year(ref, d, m, y=None):  # правило 5: без року — найближчий не минулий
    if y:
        return dt.date(y, m, d)
    cand = dt.date(ref.year, m, d)
    return cand if cand >= ref else dt.date(ref.year + 1, m, d)


def resolve(phrase: str, ref: dt.date) -> tuple[dt.date, bool]:
    p = phrase.strip().casefold()
    simple = {"сьогодні": 0, "завтра": 1, "післязавтра": 2, "через три дні": 3, "через тиждень": 7,
              "через два тижні": 14}
    if p in simple:
        return ref + dt.timedelta(days=simple[p]), True
    if p in WD_THIS:  # «цього четверга» = правило 3
        return bare_weekday(ref, WD_THIS[p]), True
    if p in WD_NEAREST:  # «у найближчу суботу» = правило 3
        return bare_weekday(ref, WD_NEAREST[p]), True
    if p in WD_NEXT:  # правило 4: на тиждень пізніше за правило 3
        return bare_weekday(ref, WD_NEXT[p]) + dt.timedelta(days=7), True
    m = re.fullmatch(r"у (\S+)", p)
    if m and m.group(1) in WD_ACC:
        return bare_weekday(ref, WD_ACC[m.group(1)]), True
    m = re.fullmatch(r"у (\S+), (\d{1,2}) (\S+)", p)
    if m:
        d = with_year(ref, int(m.group(2)), M_IDX[m.group(3)])
        if d.weekday() != WD_ACC[m.group(1)]:
            raise ValueError(f"день тижня не збігається: {phrase} -> {d}")
        return d, False
    m = re.fullmatch(r"(\d{1,2}) (\S+) (\d{4}) року", p)
    if m:
        return with_year(ref, int(m.group(1)), M_IDX[m.group(2)], int(m.group(3))), False
    m = re.fullmatch(r"(\d{1,2})\.(\d{2})(?:\.(\d{4}))?", p)
    if m:
        return with_year(ref, int(m.group(1)), int(m.group(2)),
                         int(m.group(3)) if m.group(3) else None), False
    m = re.fullmatch(r"(?:з )?(\d{1,2})(?:–| по )(\d{1,2}) (\S+)", p)  # правило 6: діапазон
    if m:
        return with_year(ref, int(m.group(1)), M_IDX[m.group(3)]), False
    m = re.fullmatch(r"з (\d{1,2}) (\S+) по (\d{1,2}) (\S+)", p)
    if m:
        return with_year(ref, int(m.group(1)), M_IDX[m.group(2)]), False
    m = re.fullmatch(r"(\d{1,2}) (\S+)", p)
    if m and m.group(2) in M_IDX:
        return with_year(ref, int(m.group(1)), M_IDX[m.group(2)]), False
    raise ValueError(f"нерозпізнана фраза дати: {phrase!r}")


EXPLICIT_DATE = re.compile(
    r"\b\d{1,2}(?:\s*[-–]\s*\d{1,2})?\s+(?:" + "|".join(MONTHS) + r")\b|\b\d{1,2}\.\d{2}(?:\.\d{4})?\b")
TIME_RE = re.compile(r"\b(?:[01]?\d|2[0-3]):[0-5]\d\b|(?:\bоб?|\bз|\bдо)\s+(?:[01]?\d|2[0-3])\.[0-5]\d\b")
WD_DATE_RE = re.compile(r"\b(понеділ\w*|вівтор\w*|серед\w*|четвер\w*|п'ятниц\w*|субот\w*|неділ\w*)"
                        r"\s*,?\s+(\d{1,2})\s+(" + "|".join(MONTHS) + r")\b", re.I)
URL_RE = re.compile(r"https?://\S+?(?=[\s,;)]|\.(?:\s|$)|$)")


def explicit_dates(text: str) -> list[str]:
    out = []
    for m in EXPLICIT_DATE.finditer(text):
        g = m.group(0)
        if re.fullmatch(r"\d{1,2}\.\d{2}(?:\.\d{4})?", g):
            if re.search(r"(?:\bоб?|\bз|\bдо)\s+$", text[:m.start()]) or not 1 <= int(g.split(".")[1]) <= 12:
                continue  # «об 11.00» — це час
        out.append(g)
    return out


def check_message(o: dict) -> list[str]:
    errs: list[str] = []
    mid, text, g = o["source_message_id"], o["text"], o["gold"]
    tn = norm(text)
    ref = dt.date.fromisoformat(o["date"])

    def err(msg):
        errs.append(f"{mid}: {msg}")

    if len(text.split()) < 50:
        err(f"закороткий текст ({len(text.split())} слів)")
    if re.search(r"[ыэъё]", text, re.I):
        err("російські літери в тексті")
    note = o.get("_expect", "")
    expected_prefix = {"trap": "ПАСТКА", "individual": "ІНДИВІДУАЛЬНА"}.get(g["kind"])
    if expected_prefix and not note.startswith(expected_prefix):
        err(f"_expect має починатися з «{expected_prefix}»")
    if g["kind"] == "announcement" and note.startswith(("ПАСТКА", "ІНДИВІДУАЛЬНА")):
        err("_expect анонсу не може починатися з маркера пастки")
    for wm in WD_DATE_RE.finditer(text):
        stem = next((i for i, st in enumerate(WD_STEMS) if norm(wm.group(1)).startswith(st)), None)
        d = with_year(ref, int(wm.group(2)), M_IDX[wm.group(3).casefold()])
        if stem is not None and d.weekday() != stem:
            err(f"день тижня «{wm.group(0)}» не збігається з {d}")
    if g["kind"] == "trap":
        if g["events"]:
            err("у пастки не може бути еталонних подій")
        return errs
    if not g["events"]:
        err("анонс без еталонних подій")
    evs = g["events"]
    if all(e["date"]["inferred"] for e in evs) and explicit_dates(text):
        err(f"відносна дата, але в тексті є явна: {explicit_dates(text)[0]!r}")
    if all(e["time"]["value"] is None for e in evs) and TIME_RE.search(text):
        err(f"подія без часу, але в тексті є час «{TIME_RE.search(text).group(0)}»")
    urls = set(URL_RE.findall(text))
    gold_urls = {e["link"]["value"] for e in evs if e["link"]["value"]}
    if urls != gold_urls:
        err(f"посилання в тексті {sorted(urls)} ≠ еталон {sorted(gold_urls)}")
    for e in evs:
        tag = f"[{e['event_title']['value'][:28]}]"
        if set(e) != set(FIELDS):
            err(f"{tag} набір полів {sorted(e)} ≠ схема")
            continue
        if e["event_type"]["value"] not in EVENT_TYPES:
            err(f"{tag} невідомий event_type")
        phrase = e["date"]["phrase"]
        if phrase.casefold() not in text.casefold():
            err(f"{tag} немає фрази дати «{phrase}»")
        if phrase.casefold() == "завтра" and not re.search(r"(?<!після)завтра", text, re.I):
            err(f"{tag} «завтра» трапляється лише в «післязавтра»")
        try:
            d, inferred = resolve(phrase, ref)
            if d.isoformat() != e["date"]["value"]:
                err(f"{tag} дата з фрази {d} ≠ еталон {e['date']['value']}")
            if inferred != e["date"]["inferred"]:
                err(f"{tag} ознака inferred не збігається")
        except (ValueError, KeyError) as exc:
            err(f"{tag} {exc}")
        t = e["time"]
        if t["value"]:
            if t["phrase"].casefold() not in text.casefold():
                err(f"{tag} немає фрази часу «{t['phrase']}»")
            h, mi = re.search(r"(\d{1,2})[:.](\d{2})", t["phrase"]).groups()
            if f"{int(h):02d}:{mi}" != t["value"]:
                err(f"{tag} час з фрази ≠ еталон")
        for f in ("event_title", "location", "organizer", "target_audience"):
            if e[f]["value"] is not None and not keys_ok(e[f]["keys"], tn):
                err(f"{tag} ключі {f} {e[f]['keys']} не знайдено в тексті")
        if e["organizer"]["value"] is None and "організатор" in tn:
            err(f"{tag} організатор null, але в тексті є «організатор»")
        if e["link"]["value"] and e["link"]["value"] not in text:
            err(f"{tag} немає посилання")
        if e["meeting_code"]["value"]:
            flat = re.sub(r"\s+", "", tn)
            if not all(any(a in flat for a in grp) for grp in e["meeting_code"]["keys"]):
                err(f"{tag} немає коду зустрічі")
        if e["language"]["value"] != "uk":
            err(f"{tag} language ≠ uk")
    if g["reaches_db"] != (g["kind"] == "announcement"):
        err("reaches_db не відповідає kind")
    return errs


def check(path: Path, corrections: Path | None = None) -> tuple[list[dict], list[str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    errors: list[str] = []
    if corrections is not None and corrections.exists():
        # Правки накладаються ДО перевірки: сенс опції саме в тому, щоб
        # виправлений еталон проходив ті самі перевірки, що й первинний.
        try:
            apply_corrections(data, json.loads(corrections.read_text(encoding="utf-8")))
        except (KeyError, ValueError, IndexError, TypeError) as exc:
            errors.append(f"правки з {corrections}: {exc}")
    ids = [o["source_message_id"] for o in data]
    if len(ids) != len(set(ids)):
        errors.append("id повідомлень не унікальні")
    texts = [o["text"] for o in data]
    if len(texts) != len(set(texts)):
        errors.append("є дублікати текстів")
    for o in data:
        errors += check_message(o)
    return data, errors


def _utf8_console() -> None:
    """У PowerShell кирилиця без цього перетворюється на «????»."""
    try:
        from scripts._console import force_utf8
        force_utf8()
    except Exception:
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass


def main() -> None:
    _utf8_console()
    parser = argparse.ArgumentParser(description="Перевірка синтетичного набору")
    parser.add_argument("dataset", nargs="?", type=Path, default=DEFAULT)
    parser.add_argument(
        "--corrections", type=Path, default=None,
        help="файл правок еталона (data/synthetic/corrections.json)",
    )
    args = parser.parse_args()
    path = args.dataset
    data, errors = check(path, args.corrections)
    kinds: dict[str, int] = {}
    for o in data:
        kinds[o["gold"]["kind"]] = kinds.get(o["gold"]["kind"], 0) + 1
    events = sum(len(o["gold"]["events"]) for o in data)
    print(f"{path}: {len(data)} повідомлень {kinds}, еталонних подій {events}, помилок {len(errors)}")
    for e in errors:
        print("  !", e)
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
