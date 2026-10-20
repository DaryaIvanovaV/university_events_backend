# -*- coding: utf-8 -*-
"""
Сборка синтетического набора и детерминированная проверка эталона.

slots.json (эталонные параметры) + texts_*.py (тексты) -> synthetic_messages.json

Проверки (все обязаны пройти, иначе файл не пишется):
  * эталонная дата пересчитывается из фразы в тексте по правилам промпта;
  * фраза даты, времени, ссылка, код встречи и ключи места, организатора,
    аудитории и названия реально есть в тексте;
  * у событий с относительной датой в тексте нет явных дат, у событий без
    времени в тексте нет времени вообще;
  * дни недели рядом с явными датами совпадают с календарём;
  * отвлекающие значения ловушек (дата, время, аудитория) в тексте есть;
  * текст не короче 50 слов, украинский, без дублей.

Запуск: python build_dataset.py [--write]
"""
from __future__ import annotations

import datetime as dt
import importlib
import json
import re
import sys
import unicodedata
from pathlib import Path

HERE = Path(__file__).parent
MONTHS = ["січня", "лютого", "березня", "квітня", "травня", "червня",
          "липня", "серпня", "вересня", "жовтня", "листопада", "грудня"]
M_IDX = {m: i + 1 for i, m in enumerate(MONTHS)}
WD_STEMS = ["понеділ", "вівтор", "серед", "четвер", "п'ятниц", "субот", "неділ"]
WD_NEXT = {"наступного понеділка": 0, "наступного вівторка": 1, "наступної середи": 2,
           "наступного четверга": 3, "наступної п'ятниці": 4, "наступної суботи": 5,
           "наступної неділі": 6}
WD_ACC = {"понеділок": 0, "вівторок": 1, "середу": 2, "четвер": 3, "п'ятницю": 4,
          "суботу": 5, "неділю": 6}
WD_THIS = {"цього понеділка": 0, "цього вівторка": 1, "цієї середи": 2, "цього четверга": 3,
           "цієї п'ятниці": 4, "цієї суботи": 5}
WD_NEAREST = {"у найближчий понеділок": 0, "у найближчий вівторок": 1, "у найближчу середу": 2,
              "у найближчий четвер": 3, "у найближчу п'ятницю": 4, "у найближчу суботу": 5}
UK_WD = ["понеділок", "вівторок", "середа", "четвер", "п'ятниця", "субота", "неділя"]
CHANNEL = "@synthetic_faculty"


def norm(s: str) -> str:
    """Нормализация для сверки по ключам: регистр, апострофы, тире, «2-го» -> «2»."""
    s = unicodedata.normalize("NFKC", s).casefold()
    s = re.sub(r"[’ʼ`´]", "'", s)
    s = re.sub(r"[–—‑−]", "-", s)
    s = s.replace("№", "")
    s = re.sub(r"(\d)-(?:го|й|ого|му)\b", r"\1", s)
    s = re.sub(r"\s*-\s*(?=\d)", "-", s)
    return re.sub(r"\s+", " ", s).strip()


def keys_ok(keys, text_n: str) -> bool:
    return all(any(norm(alt) in text_n for alt in group) for group in keys)


# --- даты по правилам промпта ---
def bare_weekday(ref, w):
    return ref + dt.timedelta(days=(w - ref.weekday()) % 7 or 7)


def with_year(ref, d, m, y=None):
    if y:
        return dt.date(y, m, d)
    cand = dt.date(ref.year, m, d)
    return cand if cand >= ref else dt.date(ref.year + 1, m, d)


def resolve_phrase(phrase: str, ref: dt.date):
    """Фраза даты -> (дата, признак «вычислена из относительного указания»)."""
    p = phrase.strip().casefold()
    if p == "сьогодні":
        return ref, True
    if p == "завтра":
        return ref + dt.timedelta(days=1), True
    if p == "післязавтра":
        return ref + dt.timedelta(days=2), True
    if p == "через тиждень":
        return ref + dt.timedelta(days=7), True
    if p == "через три дні":
        return ref + dt.timedelta(days=3), True
    if p == "через два тижні":
        return ref + dt.timedelta(days=14), True
    if p in WD_THIS:  # «цього четверга» = правило 3
        return bare_weekday(ref, WD_THIS[p]), True
    if p in WD_NEAREST:  # «у найближчу суботу» = правило 3
        return bare_weekday(ref, WD_NEAREST[p]), True
    if p in WD_NEXT:
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
    m = re.fullmatch(r"(?:з )?(\d{1,2})(?:–| по )(\d{1,2}) (\S+)", p)
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
    r"\b\d{1,2}(?:\s*[-–]\s*\d{1,2})?\s+(?:" + "|".join(MONTHS) + r")\b"
    r"|\b\d{1,2}\.\d{2}(?:\.\d{4})?\b(?!\s*(?:пт|мм|%))")
def explicit_dates(text: str) -> list[str]:
    """Явные даты в тексте; «об 11.00» — это время, «11.00» с месяцем 00 — не дата."""
    found = []
    for m in EXPLICIT_DATE.finditer(text):
        g = m.group(0)
        if re.fullmatch(r"\d{1,2}\.\d{2}(?:\.\d{4})?", g):
            if re.search(r"(?:\bоб?|\bз|\bдо)\s+$", text[:m.start()]) or not 1 <= int(g.split(".")[1]) <= 12:
                continue
        found.append(g)
    return found


TIME_RE = re.compile(r"\b(?:[01]?\d|2[0-3]):[0-5]\d\b|(?:\bоб?|\bз|\bдо)\s+(?:[01]?\d|2[0-3])\.[0-5]\d\b")
RELATIVE_RE = re.compile(r"сьогодні|(?<!після)завтра|післязавтра|через тиждень|через три дні|через два тижні|"
                         r"\bцього\s+(?:понеділка|вівторка|четверга)|\bцієї\s+(?:середи|п'ятниці|суботи)|найближч\w*\s+(?:понеділок|вівторок|середу|четвер|п'ятницю|суботу)|"
                         r"наступно(?:го|ї)\s+(?:понеділка|вівторка|середи|четверга|п'ятниці|суботи)|"
                         r"\bу\s+(?:понеділок|вівторок|середу|четвер|п'ятницю|суботу|неділю)\b", re.I)
WD_DATE_RE = re.compile(r"\b(понеділ\w*|вівтор\w*|серед\w*|четвер\w*|п'ятниц\w*|субот\w*|неділ\w*)"
                        r"\s*,?\s+(\d{1,2})\s+(" + "|".join(MONTHS) + r")\b", re.I)


def time_from_phrase(phrase: str) -> str:
    m = re.search(r"(\d{1,2})[:.](\d{2})", phrase)
    return f"{int(m.group(1)):02d}:{m.group(2)}"


TRAP_NOTES = {
    "question": "питання або чутки, а не анонс",
    "past_report": "звіт про подію, що вже відбулася",
    "chat_memory": "спогад про минуле заняття в чаті",
    "other_university": "новина про подію в іншому виші",
    "poll": "опитування щодо дати, подію ще не призначено",
    "cancelled": "подію скасовано, нової дати немає",
    "office_hours": "графік роботи, не подія",
    "requirements": "вимоги до оформлення / правила, не подія",
    "publication": "анонс публікації, не подія",
    "greeting": "привітання зі святом",
    "boundary_deadline_only": "(межовий випадок) лише кінцевий термін подання, заходу немає",
    "boundary_exam": "(межовий випадок) іспит — не захід для факультету загалом",
    "boundary_defense_schedule": "(межовий випадок) графік захисту для окремих груп",
}


def plural(n):
    return "подія" if n == 1 else "події"


def gold_event(e):
    ev = {
        "event_title": {"value": e["title"], "keys": e["title_keys"]},
        "date": {"value": e["date"], "inferred": e["date_inferred"], "phrase": e["date_phrase"]},
        "time": {"value": e["time"], "phrase": e["time_phrase"]},
        "location": {"value": e["location"], "keys": e["loc_keys"]},
        "organizer": {"value": e["organizer"], "keys": e["org_keys"]},
        "target_audience": {"value": e["target_audience"], "keys": e["aud_keys"]},
        "event_type": {"value": e["event_type"], "accept": e["type_accept"]},
        "description": {"present": True},
        "language": {"value": "uk"},
        "link": {"value": e["link"]},
        "meeting_code": {"value": e["meeting_code"], "keys": e["code_keys"]},
    }
    if e.get("range_end"):
        ev["date"]["range_end"] = e["range_end"]
    if e.get("time_span"):
        ev["time"]["span"] = e["time_span"]
    return ev


def expect_note(m):
    ref = dt.date.fromisoformat(m["ref"])
    if m["kind"] == "trap":
        sub = m["category"].split(":", 1)[1]
        extra = []
        dis = m["distractors"]
        for k in ("date_phrase", "past_date_phrase", "old_date_phrase"):
            if k in dis:
                extra.append(dis[k])
        if "dates" in dis:
            extra += dis["dates"]
        extra.append(dis["time"])
        return (f"ПАСТКА: {TRAP_NOTES[sub]} → events: []. "
                f"Є дата/час/місце ({', '.join(extra)}; {dis['room']}), але подія в календар не йде")
    parts = []
    for i, e in enumerate(m["events"], 1):
        d = dt.date.fromisoformat(e["date"])
        s = f"({i}) {e['event_type']}, date={e['date']}"
        if e["date_inferred"]:
            s += f" («{e['date_phrase']}» від {UK_WD[ref.weekday()]} {ref:%d.%m})"
        if e.get("range_end"):
            s += f", діапазон до {e['range_end']} → у description"
        s += f", time={e['time'] or 'null'}"
        if e.get("time_span"):
            s += f" (проміжок {e['time_span']})"
        s += f", місце — {e['location'] or 'null'}"
        s += f", організатор — {e['organizer'] or 'null'}"
        s += f", аудиторія — {e['target_audience'] or 'null'}"
        if e["link"]:
            s += f", link={e['link']}"
        if e["meeting_code"]:
            s += f", код — {e['meeting_code']}"
        parts.append(s)
    head = f"{len(m['events'])} {plural(len(m['events']))}: " + "; ".join(parts)
    dis = m["distractors"]
    traps = []
    if "deadline_phrase" in dis:
        traps.append(f"термін реєстрації {dis['deadline_phrase']} — лише в description, окремою подією не є")
    if "old_date_phrase" in dis:
        traps.append(f"стара дата {dis['old_date_phrase']} — НЕ використовувати")
    if "participants" in dis:
        traps.append("компанії-учасниці не є організаторами")
    if "ambiguous:next_weekday" in m["tags"]:
        traps.append("«наступного/наступної» за правилом 4 промпту (+7 днів до правила 3)")
    if traps:
        head += ". ПАСТКИ: " + "; ".join(traps)
    if m["kind"] == "individual":
        return (f"ІНДИВІДУАЛЬНА: «{dis['individual_marker']}» → фільтр audience.py має відсіяти, "
                f"у БД не потрапляє. Модель може витягти: " + head)
    return head


DERIVED = {"no_time", "no_location", "no_organizer", "no_audience", "online", "time_span"}


def retag(m):
    """Производные теги одиночного анонса пересчитываются ПОСЛЕ ручных правок слота."""
    if m["category"] != "single":
        return
    e = m["events"][0]
    tags = [t for t in m["tags"] if t not in DERIVED]
    for cond, tag in ((e.get("time_span"), "time_span"), (e["time"] is None, "no_time"),
                      (e["location"] is None, "no_location"),
                      ((e["location"] or "").startswith("Онлайн"), "online"),
                      (e["organizer"] is None, "no_organizer"),
                      (e["target_audience"] is None, "no_audience")):
        if cond:
            tags.append(tag)
    m["tags"] = tags


def load_texts():
    T, OV, OVM = {}, {}, {}
    for p in sorted(HERE.glob("texts_*.py")):
        mod = importlib.import_module(p.stem)
        T.update(mod.T)
        OV.update(getattr(mod, "OV", {}))
        OVM.update(getattr(mod, "OVM", {}))
    return T, OV, OVM


def validate(m, errors):
    mid = m["id"]
    text = m["text"]
    tn = norm(text)
    ref = dt.date.fromisoformat(m["ref"])

    def err(msg):
        errors.append(f"{mid}: {msg}")

    words = len(text.split())
    if words < 50:
        err(f"закороткий текст ({words} слів)")
    if re.search(r"[ыэъё]", text, re.I):
        err("є російські літери")
    # дни недели рядом с явными датами
    for wm in WD_DATE_RE.finditer(text):
        stem = next((i for i, st in enumerate(WD_STEMS) if norm(wm.group(1)).startswith(st)), None)
        d = with_year(ref, int(wm.group(2)), M_IDX[wm.group(3).casefold()])
        if stem is not None and d.weekday() != stem:
            err(f"день тижня «{wm.group(0)}» не збігається з {d} ({UK_WD[d.weekday()]})")
    if m["kind"] == "trap":
        dis = m["distractors"]
        for k in ("date_phrase", "past_date_phrase", "old_date_phrase"):
            if k in dis and dis[k].casefold() not in text.casefold():
                err(f"немає відволікаючої дати «{dis[k]}»")
        for dp in dis.get("dates", []):
            if dp not in text:
                err(f"немає дати «{dp}»")
        for op in dis.get("options", []):
            if op not in text:
                err(f"немає варіанту опитування «{op}»")
        h, mi = dis["time"].split(":")
        if not re.search(rf"\b0?{int(h)}[:.]{mi}\b", text):
            err(f"немає відволікаючого часу {dis['time']}")
        room = re.search(r"\d{3}", dis["room"]).group(0)
        if room not in text:
            err(f"немає аудиторії {room}")
        # «День студента» у тексті стоїть в іншому відмінку («з Днем студента»)
        if "holiday" in dis and norm(" ".join(dis["holiday"].split()[1:])) not in tn:
            err(f"немає свята «{dis['holiday']}»")
        return
    all_relative = all(e["date_inferred"] for e in m["events"])
    if all_relative and explicit_dates(text):
        err(f"у повідомленні з відносною датою є явна дата: {explicit_dates(text)[0]!r}")
    if not all_relative and m["category"] != "multi:digest":
        rel = RELATIVE_RE.search(text)
        if rel and not WD_DATE_RE.search(text):
            err(f"у повідомленні з явною датою є відносне указання «{rel.group(0)}»")
    if all(e["time"] is None for e in m["events"]) and TIME_RE.search(text):
        err(f"подія без часу, але в тексті є час «{TIME_RE.search(text).group(0)}»")
    urls = set(re.findall(r"https?://\S+?(?=[\s,;)]|\.(?:\s|$)|$)", text))
    gold_urls = {e["link"] for e in m["events"] if e["link"]}
    if urls != gold_urls:
        err(f"посилання в тексті {sorted(urls)} ≠ еталон {sorted(gold_urls)}")
    for e in m["events"]:
        tag = f"[{e['title'][:30]}]"
        phrase = e["date_phrase"]
        if phrase.casefold() not in text.casefold():
            err(f"{tag} немає фрази дати «{phrase}»")
        else:
            if phrase.casefold() == "завтра" and not re.search(r"(?<!після)завтра", text, re.I):
                err(f"{tag} «завтра» є лише у складі «післязавтра»")
        try:
            d, inferred = resolve_phrase(phrase, ref)
            if d.isoformat() != e["date"]:
                err(f"{tag} дата з фрази {d} ≠ еталон {e['date']}")
            if inferred != e["date_inferred"]:
                err(f"{tag} ознака inferred не збігається")
        except (ValueError, KeyError) as exc:
            err(f"{tag} {exc}")
        if e["time_phrase"]:
            if e["time_phrase"].casefold() not in text.casefold():
                err(f"{tag} немає фрази часу «{e['time_phrase']}»")
            if time_from_phrase(e["time_phrase"]) != e["time"]:
                err(f"{tag} час з фрази ≠ еталон")
        for field, kk in (("location", "loc_keys"), ("organizer", "org_keys"),
                          ("target_audience", "aud_keys"), ("title", "title_keys")):
            if e.get(field) and not keys_ok(e[kk], tn):
                err(f"{tag} ключі поля {field} {e[kk]} не знайдено в тексті")
        if e["organizer"] is None and "організатор" in tn:
            err(f"{tag} організатор null, але в тексті є слово «організатор»")
        if e["link"] and e["link"] not in text:
            err(f"{tag} немає посилання {e['link']}")
        if e["meeting_code"]:
            flat = re.sub(r"\s+", "", tn)
            if not all(any(a in flat for a in g) for g in e["code_keys"]):
                err(f"{tag} немає коду зустрічі {e['meeting_code']}")
    dis = m["distractors"]
    for k in ("deadline_phrase", "old_date_phrase", "old_time_phrase", "individual_marker"):
        if k in dis and dis[k] not in text:
            err(f"немає «{dis[k]}» ({k})")
    if "participants" in dis and sum(p in text for p in dis["participants"]) < 2:
        err("немає компаній-учасниць")
    if m["kind"] == "announcement" and re.search(
            r"скасован|відмінен|не\s+відбудеться|індивідуальн|попереднім\s+записом|вийш\w+\s+друком", tn):
        err("анонс містить маркери, які відсіє детермінований фільтр")


def main():
    slots = json.loads((HERE / "slots.json").read_text(encoding="utf-8"))
    if (HERE / "slots_2.json").exists():  # вторая сотня (3101-3200)
        slots += json.loads((HERE / "slots_2.json").read_text(encoding="utf-8"))
    T, OV, OVM = load_texts()
    errors, out, seen = [], [], set()
    for m in slots:
        if m["id"] not in T:
            continue
        entry = T[m["id"]]
        m.update(OVM.get(m["id"], {}))  # правки уровня сообщения (дата публикации)
        m["text"] = entry["t"].strip()
        if len(entry["ev"]) != len(m["events"]):
            errors.append(f"{m['id']}: подій у тексті {len(entry['ev'])} ≠ у слоті {len(m['events'])}")
            continue
        for e, (title, keys), ov in zip(m["events"], entry["ev"], OV.get(m["id"], [{}] * len(m["events"]))):
            e["title"], e["title_keys"] = title, keys
            e.update(ov)
        retag(m)
        if m["text"] in seen:
            errors.append(f"{m['id']}: дубль тексту")
        seen.add(m["text"])
        validate(m, errors)
        out.append({
            "source_message_id": m["id"], "date": m["ref"], "source_channel": CHANNEL,
            "text": m["text"], "_expect": expect_note(m),
            "gold": {"kind": m["kind"], "category": m["category"], "tags": m["tags"],
                     "reaches_db": m["kind"] == "announcement",
                     "events": [gold_event(e) for e in m["events"]]},
        })
    # почти-дубли: пересечение словесных триграмм
    grams = {o["source_message_id"]: {tuple(norm(o["text"]).split()[i:i + 3])
                                      for i in range(len(o["text"].split()) - 2)} for o in out}
    ids = sorted(grams)
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            j = len(grams[a] & grams[b]) / max(1, len(grams[a] | grams[b]))
            if j > 0.25:
                errors.append(f"{a}/{b}: схожі тексти (Jaccard {j:.2f})")
    print(f"зібрано {len(out)} повідомлень, помилок: {len(errors)}")
    for e in errors:
        print("  !", e)
    if "--write" in sys.argv:
        if errors:
            sys.exit("файл не записано: є помилки")
        out.sort(key=lambda o: o["source_message_id"])
        (HERE / "synthetic_messages.json").write_text(
            json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print("записано synthetic_messages.json")


if __name__ == "__main__":
    sys.path.insert(0, str(HERE))
    main()
