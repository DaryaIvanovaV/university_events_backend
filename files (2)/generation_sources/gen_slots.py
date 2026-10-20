# -*- coding: utf-8 -*-
"""
Слоты синтетического набора: сначала эталон, потом текст.

Для каждого сообщения случайно (seed фиксирован) выбираются параметры:
дата публикации, способ записи даты события, время, место, организатор,
аудитория, ссылка, код онлайн-встречи. Эталонная дата считается по тем же
правилам, что записаны в системном промпте (раздел Dates, правила 1-7).
Тексты пишутся уже под эти слоты, поэтому правильный ответ известен до
прогона моделей.

Запуск:  python gen_slots.py  ->  slots.json (+ сводка в консоль)
"""
from __future__ import annotations

import datetime as dt
import json
import random
import string
import sys
from pathlib import Path

SEED = 20260921
R = random.Random(SEED)
D = dt.timedelta

MONTHS = ["січня", "лютого", "березня", "квітня", "травня", "червня",
          "липня", "серпня", "вересня", "жовтня", "листопада", "грудня"]
# «у + знахідний відмінок» и «наступного/наступної + родовий»
WD_ACC = ["понеділок", "вівторок", "середу", "четвер", "п'ятницю", "суботу", "неділю"]
WD_NEXT = ["наступного понеділка", "наступного вівторка", "наступної середи",
           "наступного четверга", "наступної п'ятниці", "наступної суботи",
           "наступної неділі"]
WD_EN = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# Номера аудиторий из реального датасета и few-shot промпта исключены,
# чтобы модель не могла «узнать» значения по памяти промпта.
EXCLUDED_ROOMS = {101, 108, 115, 118, 205, 207, 210, 214, 301, 302, 312, 405, 501}
TIMES = ["09:00", "09:30", "10:00", "10:20", "11:00", "11:30", "12:00", "12:40",
         "13:00", "13:20", "14:00", "14:10", "15:00", "15:20", "15:40", "16:00",
         "16:20", "17:00", "18:00", "18:30"]

ORGANIZERS = [  # (эталон, ключи)
    ("кафедра програмної інженерії", [["програмн"]]),
    ("кафедра кібербезпеки", [["кібербезпек"]]),
    ("кафедра штучного інтелекту та аналізу даних", [["штучного інтелекту"]]),
    ("кафедра прикладної математики", [["прикладн"], ["математик"]]),
    ("студентська рада факультету", [["студентськ"], ["рад"]]),
    ("науковий гурток «Data Lab»", [["data lab"]]),
    ("Центр кар'єри університету", [["кар'єр"]]),
    ("наукова бібліотека університету", [["бібліотек"]]),
    ("відділ міжнародних зв'язків", [["міжнародн"]]),
    ("студентське наукове товариство", [["товариств"]]),
    ("волонтерський центр університету", [["волонтер"]]),
    ("психологічна служба університету", [["психологічн"]]),
]
COMPANIES = [
    ("компанія SoftGrid", [["softgrid"]]),
    ("компанія NordByte", [["nordbyte"]]),
    ("компанія CloudPeak", [["cloudpeak"]]),
]
PARTICIPANTS = ["SoftGrid", "NordByte", "CloudPeak", "DataForge", "Bitwise Lab", "Arcadia Soft"]

AUDIENCES = [  # (эталон, ключи, как пишется в тексте)
    ("студенти 1 курсу", [["1 курс", "першокурсн"]], "студентів 1 курсу"),
    ("студенти 2 курсу", [["2 курс"]], "студентів 2 курсу"),
    ("студенти 3–4 курсів", [["3-4 курс"]], "студентів 3–4 курсів"),
    ("студенти 4 курсу", [["4 курс"]], "студентів 4 курсу"),
    ("магістранти", [["магістр"]], "магістрантів"),
    ("аспіранти та молоді науковці", [["аспірант"]], "аспірантів та молодих науковців"),
    ("викладачі та студенти", [["викладач"], ["студент"]], "викладачів та студентів"),
    ("усі охочі", [["охоч"]], "усіх охочих"),
    ("першокурсники", [["першокурсн", "1 курс"]], "першокурсників"),
    ("студенти спеціальності 121", [["121"]], "студентів спеціальності 121"),
    ("студенти старших курсів", [["старш"]], "студентів старших курсів"),
    ("студенти всіх курсів", [["всіх курс", "усіх курс"]], "студентів усіх курсів"),
]
HALLS = [  # (как написано, ключи)
    ("актова зала головного корпусу", [["актов"]]),
    ("конференц-зала наукової бібліотеки", [["конференц"]]),
    ("коворкінг на третьому поверсі корпусу 1", [["коворкінг"]]),
    ("спортивний комплекс університету", [["спортивн"]]),
    ("читальна зала бібліотеки", [["читальн"]]),
    ("амфітеатр корпусу 4", [["амфітеатр"]]),
    ("зала засідань вченої ради", [["засідань", "вченої ради"]]),
    ("фойє корпусу 3", [["фойє"]]),
]
ADDRESSES = [
    ("вул. Каштанова, 21", [["каштанов"]]),
    ("вул. Освітянська, 12", [["освітянськ"]]),
]

TOPICS = {
    "lecture": ["Rust для системного програмування", "як працюють рекомендаційні системи",
                "етика штучного інтелекту", "квантові обчислення для програмістів",
                "архітектура високонавантажених систем", "кар'єра в геймдеві",
                "відкриті дані в публічному секторі", "машинне навчання в біоінформатиці",
                "історія української кібернетики", "UX-дослідження в продуктових командах",
                "комп'ютерний зір у медицині", "криптографія на еліптичних кривих"],
    "seminar": ["науковий семінар «Графові нейронні мережі»", "засідання гуртка з криптографії",
                "круглий стіл «Академічна мобільність студентів»", "методологічний семінар для аспірантів",
                "семінар «Формальна верифікація програм»", "круглий стіл «Жінки в ІТ»",
                "семінар з математичного моделювання епідемій", "семінар «Великі мовні моделі в освіті»"],
    "workshop": ["майстер-клас з Figma для розробників", "воркшоп з Docker Compose",
                 "практикум з Git для командної роботи", "майстер-клас зі складання резюме",
                 "воркшоп з тестування API у Postman", "майстер-клас з публічних виступів",
                 "практикум з Linux для початківців", "воркшоп з SQL-оптимізації"],
    "webinar": ["вебінар «Як підготуватися до технічної співбесіди»", "вебінар про програму Erasmus+",
                "вебінар «Кібергігієна для студентів»", "вебінар «Вступ до MLOps»",
                "вебінар про наукометричні бази Scopus і Web of Science", "вебінар «Фріланс для початківців»",
                "вебінар «Як написати першу наукову статтю»", "вебінар «Хмарні сервіси для студентських проєктів»"],
    "conference": ["студентська конференція «Молода наука — 2026»", "конференція «Цифрові технології в освіті»",
                   "науково-практична конференція «Інтелектуальні системи та технології»",
                   "конференція молодих вчених «Інформатика та кібернетика»",
                   "конференція «Сучасні проблеми захисту інформації»"],
    "hackathon": ["хакатон «Smart City Hack»", "факультетський етап олімпіади з програмування",
                  "CTF-змагання з кібербезпеки", "хакатон «GreenTech»", "конкурс студентських стартапів"],
    "other": ["день відкритих дверей", "ярмарок вакансій", "екскурсія до IT-компанії",
              "кіноклуб: показ фільму «Гра в імітацію»", "турнір з настільного тенісу",
              "благодійний ярмарок", "інтелектуальна гра «Що? Де? Коли?»", "зустріч із випускниками",
              "день донора", "посвята в студенти"],
}
# Где жанр неоднозначен для enum схемы — допустимые альтернативы.
TYPE_ACCEPT = {
    "факультетський етап олімпіади з програмування": ["other"],
    "конкурс студентських стартапів": ["other"],
    "CTF-змагання з кібербезпеки": ["other"],
    "турнір з настільного тенісу": ["hackathon"],
    "інтелектуальна гра «Що? Де? Коли?»": ["hackathon"],
    "зустріч із випускниками": ["seminar"],
    "екскурсія до IT-компанії": [],
}


def pop_topic(kind: str, used: dict) -> str:
    """Тема без повторов, пока пул не исчерпан."""
    pool = [t for t in TOPICS[kind] if t not in used.setdefault(kind, set())]
    if not pool:
        used[kind] = set()
        pool = list(TOPICS[kind])
    topic = R.choice(pool)
    used[kind].add(topic)
    return topic


def rand_ref(lo=dt.date(2026, 9, 7), hi=dt.date(2026, 12, 11), weekdays=(0, 1, 2, 3, 4)):
    while True:
        d = lo + D(days=R.randint(0, (hi - lo).days))
        if d.weekday() in weekdays:
            return d


def in_break(d):
    """Зимние каникулы: с 25 декабря по 10 января мероприятий нет."""
    return (d.month == 12 and d.day >= 25) or (d.month == 1 and d.day <= 10)


def future(ref, lo=4, hi=35):
    while True:
        d = ref + D(days=R.randint(lo, hi))
        if d.weekday() != 6 and not in_break(d):  # в воскресенье мероприятий нет
            return d


def bare_weekday(ref, w):  # правило 3: ближайший такой день строго ПОСЛЕ даты публикации
    return ref + D(days=(w - ref.weekday()) % 7 or 7)


def next_weekday(ref, w):  # правило 4: ровно на неделю позже правила 3
    return bare_weekday(ref, w) + D(days=7)


def dm(d):
    return f"{d.day} {MONTHS[d.month - 1]}"


def date_slot(kind):
    """(ref, phrase, date, inferred, extra) для одного события."""
    if kind == "today":
        ref = rand_ref()
        return ref, "сьогодні", ref, True, {}
    if kind == "tomorrow":
        ref = rand_ref()
        return ref, "завтра", ref + D(days=1), True, {}
    if kind == "day_after":
        ref = rand_ref(weekdays=(0, 1, 2, 3))
        return ref, "післязавтра", ref + D(days=2), True, {}
    if kind == "bare_weekday":
        ref = rand_ref(weekdays=(0, 1, 2, 3))
        w = R.randint(ref.weekday() + 1, 4)
        return ref, f"у {WD_ACC[w]}", bare_weekday(ref, w), True, {}
    if kind == "next_weekday":
        ref = rand_ref(weekdays=(0, 1, 2))
        w = R.randint(ref.weekday() + 1, 4)
        return ref, WD_NEXT[w], next_weekday(ref, w), True, {"ambiguous": "next_weekday"}
    if kind == "in_week":
        ref = rand_ref()
        return ref, "через тиждень", ref + D(days=7), True, {}
    if kind == "in_3days":
        ref = rand_ref(weekdays=(0, 1, 2))
        return ref, "через три дні", ref + D(days=3), True, {}
    if kind == "cross_year":
        ref = rand_ref(lo=dt.date(2026, 12, 7), hi=dt.date(2026, 12, 18))
        while True:
            d = dt.date(2027, 1, R.randint(12, 28))
            if d.weekday() != 6:
                return ref, dm(d), d, False, {"year_rollover": True}
    ref = rand_ref()
    if kind == "range":
        d1 = future(ref, 8, 40)
        d2 = d1 + D(days=R.choice([1, 1, 2]))
        if d2.weekday() == 6:
            d2 -= D(days=1)
        if d1.month == d2.month:
            phrase = R.choice([f"{d1.day}–{d2.day} {MONTHS[d1.month - 1]}",
                               f"з {d1.day} по {d2.day} {MONTHS[d1.month - 1]}"])
        else:
            phrase = f"з {dm(d1)} по {dm(d2)}"
        return ref, phrase, d1, False, {"range_end": d2.isoformat()}
    d = future(ref)
    if kind == "dm":
        return ref, dm(d), d, False, {}
    if kind == "dm_wd":
        return ref, f"у {WD_ACC[d.weekday()]}, {dm(d)}", d, False, {}
    if kind == "num":
        return ref, f"{d.day:02d}.{d.month:02d}", d, False, {}
    if kind == "num_year":
        return ref, f"{d.day:02d}.{d.month:02d}.{d.year}", d, False, {}
    if kind == "year":
        return ref, f"{dm(d)} {d.year} року", d, False, {}
    raise ValueError(kind)


def prep(t):  # «об 11:00», но «о 10:00»: милозвучність перед голосним
    return "об" if t.startswith("11") else "о"


def time_slot(kind):
    t = R.choice(TIMES)
    h, m = t.split(":")
    hh = str(int(h))
    if kind == "none":
        return None, None, {}
    if kind == "colon":
        return f"{prep(hh)} {hh}:{m}", t, {}
    if kind == "dot":
        return f"{prep(hh)} {hh}.{m}", t, {}
    if kind == "start":
        return f"початок {prep(hh)} {hh}:{m}", t, {}
    if kind == "range":
        end_h = min(int(h) + R.randint(2, 5), 20)
        return f"з {hh}:{m} до {end_h}:{m}", t, {"time_span": f"{t}-{end_h:02d}:{m}"}
    raise ValueError(kind)


def room_phrase(workshop=False):
    while True:
        n = R.randint(1, 5) * 100 + R.randint(2, 25)
        if n not in EXCLUDED_ROOMS:
            break
    k = R.randint(1, 5)
    styles = [f"ауд. {n}, корпус {k}", f"аудиторія {n} (корпус №{k})", f"ауд. {n} корпусу {k}"]
    if workshop:
        styles += [f"лабораторія {n}, корпус {k}", f"комп'ютерний клас {n}, корпус {k}"]
    return R.choice(styles), [[str(n)]]


def rand_digits(n):
    return "".join(R.choice(string.digits) for _ in range(n))


def online_slot():
    kind = R.choice(["zoom", "zoom", "zoom", "meet", "teams"])
    if kind == "zoom":
        mid = f"{R.randint(7, 9)}{rand_digits(2)} {rand_digits(4)} {rand_digits(4)}"
        code = rand_digits(6)
        return {"platform": "Zoom", "location": "Онлайн (Zoom)", "loc_keys": [["онлайн", "zoom"]],
                "link": f"https://zoom.us/j/{mid.replace(' ', '')}",
                "meeting_code": f"ID {mid} · код {code}",
                "code_keys": [[mid.replace(" ", "")], [code]],
                "code_phrase": f"ID {mid}, код доступу {code}"}
    if kind == "meet":
        letters = string.ascii_lowercase
        slug = "-".join("".join(R.choice(letters) for _ in range(n)) for n in (3, 4, 3))
        return {"platform": "Google Meet", "location": "Онлайн (Google Meet)",
                "loc_keys": [["онлайн", "meet"]], "link": f"https://meet.google.com/{slug}",
                "meeting_code": None, "code_keys": None, "code_phrase": None}
    mid = f"{rand_digits(3)} {rand_digits(3)} {rand_digits(3)} {rand_digits(3)}"
    code = "".join(R.choice(string.ascii_letters + string.digits) for _ in range(6))
    return {"platform": "Microsoft Teams", "location": "Онлайн (Microsoft Teams)",
            "loc_keys": [["онлайн", "teams"]], "link": f"https://teams.live.com/meet/{mid.replace(' ', '')}",
            "meeting_code": f"ID {mid} · код {code}", "code_keys": [[mid.replace(" ", "")], [code.lower()]],
            "code_phrase": f"ID наради {mid}, секретний код {code}"}


SLUGS = ["reg", "join", "form", "apply", "signup", "event"]


def reg_link(topic):
    host = R.choice(["forms.example.ua", "events.example.ua", "reg.example.ua"])
    return f"https://{host}/{R.choice(SLUGS)}-{rand_digits(4)}"


def event_slot(etype, date_kind, time_kind, loc_kind, used, ref=None, fixed_date=None):
    topic = pop_topic(etype, used)
    if fixed_date is not None:
        ref_, phrase, date, inferred, extra = fixed_date
    else:
        ref_, phrase, date, inferred, extra = date_slot(date_kind)
    tphrase, tval, textra = time_slot(time_kind)
    ev = {"event_type": etype, "type_accept": TYPE_ACCEPT.get(topic, []), "topic": topic,
          "date_kind": date_kind, "date_phrase": phrase, "date": date.isoformat(),
          "date_inferred": inferred, **extra,
          "time_kind": time_kind, "time_phrase": tphrase, "time": tval, **textra,
          "loc_kind": loc_kind, "link": None, "meeting_code": None, "code_keys": None,
          "code_phrase": None, "platform": None}
    if loc_kind == "room":
        ev["location"], ev["loc_keys"] = room_phrase(etype in ("workshop", "hackathon"))
    elif loc_kind == "hall":
        ev["location"], ev["loc_keys"] = R.choice(HALLS)
    elif loc_kind == "address":
        ev["location"], ev["loc_keys"] = R.choice(ADDRESSES)
    elif loc_kind == "online":
        o = online_slot()
        ev.update(location=o["location"], loc_keys=o["loc_keys"], link=o["link"],
                  meeting_code=o["meeting_code"], code_keys=o["code_keys"],
                  code_phrase=o["code_phrase"], platform=o["platform"])
    else:
        ev["location"], ev["loc_keys"] = None, None
    return ref_, ev


ORG = {name: (name, keys) for name, keys in ORGANIZERS + COMPANIES}
DEPTS = [o[0] for o in ORGANIZERS[:4]]
ORG_BY_TYPE = {
    "lecture": DEPTS + ["студентське наукове товариство", "науковий гурток «Data Lab»"],
    "seminar": DEPTS + ["студентське наукове товариство", "науковий гурток «Data Lab»"],
    "conference": DEPTS + ["студентське наукове товариство", "відділ міжнародних зв'язків"],
    "workshop": DEPTS + ["науковий гурток «Data Lab»", "студентська рада факультету"],
    "webinar": DEPTS + ["Центр кар'єри університету"],
    "hackathon": DEPTS + ["студентська рада факультету", "студентське наукове товариство"],
}
COMPANY_OK = {"lecture", "workshop", "hackathon", "webinar"}
TOPIC_ORG = {  # тема -> допустимые организаторы
    "вебінар про програму Erasmus+": ["відділ міжнародних зв'язків"],
    "вебінар про наукометричні бази Scopus і Web of Science": ["наукова бібліотека університету"],
    "вебінар «Фріланс для початківців»": ["Центр кар'єри університету"],
    "вебінар «Як підготуватися до технічної співбесіди»": ["Центр кар'єри університету"],
    "майстер-клас зі складання резюме": ["Центр кар'єри університету"],
    "майстер-клас з публічних виступів": ["психологічна служба університету", "студентська рада факультету"],
    "вебінар «Як написати першу наукову статтю»": ["наукова бібліотека університету", "студентське наукове товариство"],
    "круглий стіл «Академічна мобільність студентів»": ["відділ міжнародних зв'язків"],
    "день відкритих дверей": DEPTS,
    "ярмарок вакансій": ["Центр кар'єри університету"],
    "екскурсія до IT-компанії": ["Центр кар'єри університету"],
    "кіноклуб: показ фільму «Гра в імітацію»": ["студентська рада факультету", "студентське наукове товариство"],
    "турнір з настільного тенісу": ["студентська рада факультету"],
    "благодійний ярмарок": ["волонтерський центр університету", "студентська рада факультету"],
    "інтелектуальна гра «Що? Де? Коли?»": ["студентська рада факультету", "студентське наукове товариство"],
    "зустріч із випускниками": ["Центр кар'єри університету"] + DEPTS,
    "день донора": ["волонтерський центр університету"],
    "посвята в студенти": ["студентська рада факультету"],
}
TOPIC_HALL = {  # тема -> допустимые залы/места
    "турнір з настільного тенісу": ["спортивний комплекс університету"],
    "ярмарок вакансій": ["спортивний комплекс університету", "фойє корпусу 3", "актова зала головного корпусу"],
    "посвята в студенти": ["актова зала головного корпусу"],
    "день відкритих дверей": ["актова зала головного корпусу", "амфітеатр корпусу 4"],
    "день донора": ["фойє корпусу 3", "спортивний комплекс університету"],
    "благодійний ярмарок": ["фойє корпусу 3"],
    "кіноклуб: показ фільму «Гра в імітацію»": ["амфітеатр корпусу 4", "актова зала головного корпусу"],
    "зустріч із випускниками": ["актова зала головного корпусу", "амфітеатр корпусу 4"],
    "інтелектуальна гра «Що? Де? Коли?»": ["читальна зала бібліотеки", "коворкінг на третьому поверсі корпусу 1"],
}
TOPIC_AUD = {
    "посвята в студенти": ["першокурсники", "студенти 1 курсу"],
    "день відкритих дверей": ["абітурієнти та їхні батьки"],
    "ярмарок вакансій": ["студенти старших курсів", "студенти всіх курсів", None],
}
AUD = {a[0]: a for a in AUDIENCES}
AUD["абітурієнти та їхні батьки"] = ("абітурієнти та їхні батьки", [["абітурієнт"]], "абітурієнтів та їхніх батьків")


def pick_org(etype, topic, p_named=0.62, p_company=0.12):
    if topic in TOPIC_ORG:
        return ORG[R.choice(TOPIC_ORG[topic])] if R.random() < 0.8 else None
    x = R.random()
    if x < p_named:
        return ORG[R.choice(ORG_BY_TYPE.get(etype, DEPTS))]
    if x < p_named + p_company and etype in COMPANY_OK:
        return R.choice(COMPANIES)
    return None


def pick_aud(topic, p=0.62):
    if topic in TOPIC_AUD:
        choice = R.choice(TOPIC_AUD[topic])
        return AUD[choice] if choice else None
    return R.choice(AUDIENCES) if R.random() < p else None


def finish(ev, org="auto", aud="auto", link_p=0.35):
    o = pick_org(ev["event_type"], ev["topic"]) if org == "auto" else org
    a = pick_aud(ev["topic"]) if aud == "auto" else aud
    if ev["topic"] in TOPIC_HALL and ev["loc_kind"] in ("room", "hall", "address"):
        name = R.choice(TOPIC_HALL[ev["topic"]])
        ev["loc_kind"] = "hall"
        ev["location"], ev["loc_keys"] = next(h for h in HALLS if h[0] == name)
    ev["organizer"], ev["org_keys"] = (o if o else (None, None))
    ev["target_audience"], ev["aud_keys"], ev["aud_phrase"] = (a if a else (None, None, None))
    if ev["link"] is None and ev["loc_kind"] != "online" and R.random() < link_p:
        ev["link"] = reg_link(ev["topic"])
    return ev


def build():
    used: dict = {}
    msgs = []

    # --- 48 одиночных анонсов ---
    date_kinds = (["dm"] * 12 + ["dm_wd"] * 5 + ["num"] * 3 + ["num_year"] * 2 + ["year"] * 2
                  + ["cross_year"] * 2 + ["range"] * 4 + ["today"] * 2 + ["tomorrow"] * 4
                  + ["day_after"] * 2 + ["bare_weekday"] * 5 + ["next_weekday"] * 3
                  + ["in_week"] * 1 + ["in_3days"] * 1)
    assert len(date_kinds) == 48, len(date_kinds)
    types = (["lecture"] * 10 + ["seminar"] * 7 + ["workshop"] * 7 + ["webinar"] * 7
             + ["conference"] * 5 + ["hackathon"] * 4 + ["other"] * 8)
    assert len(types) == 48
    R.shuffle(date_kinds)
    R.shuffle(types)
    # конференции и «школы» тяготеют к диапазонам дат
    for i, t in enumerate(types):
        if t == "conference" and "range" in date_kinds and date_kinds[i] != "range":
            j = date_kinds.index("range")
            date_kinds[i], date_kinds[j] = date_kinds[j], date_kinds[i]
    time_kinds = ["colon"] * 30 + ["dot"] * 4 + ["start"] * 5 + ["range"] * 5 + ["none"] * 4
    R.shuffle(time_kinds)
    non_web_locs = ["room"] * 22 + ["hall"] * 11 + ["address"] * 3 + ["online"] * 2 + ["none"] * 3
    R.shuffle(non_web_locs)
    deadline_left = 6
    for i in range(48):
        et, dk, tk = types[i], date_kinds[i], time_kinds[i]
        lk = "online" if et == "webinar" else non_web_locs.pop()
        if lk == "none" and tk == "none":
            tk = "colon"
        ref, ev = event_slot(et, dk, tk, lk, used)
        finish(ev)
        tags = []
        if dk in ("today", "tomorrow", "day_after", "bare_weekday", "next_weekday", "in_week", "in_3days"):
            tags.append(f"relative:{dk}")
        if ev.get("ambiguous"):
            tags.append("ambiguous:next_weekday")
        if dk == "range":
            tags.append("date_range")
        if dt.date.fromisoformat(ev["date"]).year > ref.year:
            tags.append("year_rollover")
        if tk == "range":
            tags.append("time_span")
        if tk == "none":
            tags.append("no_time")
        if lk == "none":
            tags.append("no_location")
        if lk == "online":
            tags.append("online")
        if ev["organizer"] is None:
            tags.append("no_organizer")
        if ev["target_audience"] is None:
            tags.append("no_audience")
        distractors = {}
        explicit = dk in ("dm", "dm_wd", "num", "num_year", "year", "range")
        if explicit and deadline_left and ev["link"] and dt.date.fromisoformat(ev["date"]) - ref > D(days=6):
            dl = ref + D(days=R.randint(2, (dt.date.fromisoformat(ev["date"]) - ref).days - 2))
            distractors["deadline_phrase"] = dm(dl)
            tags.append("deadline_mention")
            deadline_left -= 1
        if et == "other" and "ярмарок вакансій" in ev["topic"]:
            distractors["participants"] = R.sample(PARTICIPANTS, 3)
            tags.append("participants_not_organizers")
        msgs.append({"kind": "announcement", "category": "single", "ref": ref.isoformat(),
                     "events": [ev], "tags": tags, "distractors": distractors})

    # --- 2 перенесения с новой датой (оставить НОВУЮ дату) ---
    for et in ("seminar", "lecture"):
        ref = rand_ref()
        old = future(ref, 4, 15)
        new = future(old - (old - ref), (old - ref).days + 3, (old - ref).days + 12)
        _, ev = event_slot(et, "dm", "colon", "room", used,
                           fixed_date=(ref, dm(new), new, False, {}))
        finish(ev, link_p=0.0)
        msgs.append({"kind": "announcement", "category": "postponed", "ref": ref.isoformat(),
                     "events": [ev], "tags": ["postponed"],
                     "distractors": {"old_date_phrase": dm(old)}})

    # --- 2 массовые консультации (должны сохраниться) ---
    for aud_i in (1, 3):
        ref = rand_ref()
        d = future(ref, 3, 14)
        _, ev = event_slot("other", "dm", "colon", "room", used, fixed_date=(ref, dm(d), d, False, {}))
        ev["event_type"], ev["type_accept"] = "consultation", []
        ev["topic"] = R.choice(["загальна консультація перед модульною контрольною",
                                "відкрита консультація щодо вибору баз практики"]) if aud_i == 1 else \
            "консультація щодо вибору теми курсової роботи"
        finish(ev, org=None, aud=AUDIENCES[aud_i], link_p=0.0)
        msgs.append({"kind": "announcement", "category": "mass_consultation", "ref": ref.isoformat(),
                     "events": [ev], "tags": ["consultation_mass"], "distractors": {}})

    # --- 8 сообщений с несколькими событиями ---
    msgs.extend(build_multi(used))

    # --- 5 индивидуальных консультаций (модель извлечёт, фильтр охвата отсеет) ---
    markers = ["за попереднім записом", "індивідуально, час узгоджується з викладачем",
               "за індивідуальним графіком", "запис у таблиці, кожному студенту — 15 хвилин",
               "особисто з кожним студентом"]
    for mk in markers:
        ref = rand_ref()
        d = future(ref, 2, 12)
        tk = R.choice(["colon", "range"])
        _, ev = event_slot("other", "dm_wd", tk, "room", used,
                           fixed_date=(ref, f"у {WD_ACC[d.weekday()]}, {dm(d)}", d, False, {}))
        ev["event_type"], ev["type_accept"] = "consultation", []
        ev["topic"] = R.choice(["консультації для боржників", "консультації з дипломного проєктування",
                                "консультації щодо перескладання", "зустрічі з керівником практики",
                                "консультації з курсових робіт"])
        room = R.randint(2, 5) * 100 + R.randint(10, 30)
        ev["location"], ev["loc_keys"] = f"каб. {room}", [[str(room)]]
        finish(ev, org=None, aud=None, link_p=0.0)
        msgs.append({"kind": "individual", "category": "individual", "ref": ref.isoformat(),
                     "events": [ev], "tags": ["consultation_individual"],
                     "distractors": {"individual_marker": mk}})

    # --- 35 ловушек ---
    msgs.extend(build_traps())

    R.shuffle(msgs)
    for i, m in enumerate(msgs):
        m["id"] = 3001 + i
    return msgs


def build_multi(used):
    out = []
    # M1: один воркшоп у двох сесіях з різницею в тиждень
    ref = rand_ref()
    d1 = future(ref, 5, 15)
    d2 = d1 + D(days=7)
    tp, tv, _ = time_slot("colon")
    loc, keys = room_phrase(workshop=True)
    org = R.choice(ORGANIZERS[:4])
    evs = []
    for d in (d1, d2):
        _, ev = event_slot("workshop", "dm", "none", "room", used, fixed_date=(ref, dm(d), d, False, {}))
        ev.update(time_phrase=tp, time=tv, time_kind="colon", location=loc, loc_keys=keys)
        finish(ev, org=org, aud=AUDIENCES[1], link_p=0.0)
        evs.append(ev)
    evs[1]["topic"] = evs[0]["topic"]
    out.append({"kind": "announcement", "category": "multi:two_sessions", "ref": ref.isoformat(),
                "events": evs, "tags": ["multi_event"], "distractors": {}})

    # M2: дайджест тижня — три події, дні тижня відносно понеділка
    ref = rand_ref(weekdays=(0,))
    evs = []
    for w, et in ((1, "lecture"), (3, "seminar"), (4, "other")):
        d = bare_weekday(ref, w)
        _, ev = event_slot(et, "bare_weekday", "colon", R.choice(["room", "hall"]), used,
                           fixed_date=(ref, f"у {WD_ACC[w]}", d, True, {}))
        finish(ev, link_p=0.0)
        evs.append(ev)
    out.append({"kind": "announcement", "category": "multi:digest", "ref": ref.isoformat(),
                "events": evs, "tags": ["multi_event", "relative:bare_weekday"], "distractors": {}})

    # M3: цикл із двох лекцій
    ref = rand_ref()
    d1 = future(ref, 5, 14)
    d2 = d1 + D(days=14)
    tp, tv, _ = time_slot("colon")
    loc, keys = R.choice(HALLS)
    evs = []
    for d in (d1, d2):
        _, ev = event_slot("lecture", "dm", "none", "hall", used, fixed_date=(ref, dm(d), d, False, {}))
        ev.update(time_phrase=tp, time=tv, time_kind="colon", location=loc, loc_keys=keys)
        finish(ev, org=ORGANIZERS[2], aud=AUDIENCES[7], link_p=0.0)
        evs.append(ev)
    out.append({"kind": "announcement", "category": "multi:lecture_series", "ref": ref.isoformat(),
                "events": evs, "tags": ["multi_event"], "distractors": {}})

    # M4: відбірковий етап + фінал олімпіади (обидва на факультеті)
    ref = rand_ref()
    d1 = future(ref, 7, 18)
    d2 = d1 + D(days=R.choice([14, 21]))
    evs = []
    for d in (d1, d2):
        _, ev = event_slot("hackathon", "dm", "colon", "room", used, fixed_date=(ref, dm(d), d, False, {}))
        ev["topic"] = "олімпіада з програмування"
        ev["type_accept"] = ["other"]
        finish(ev, org=ORGANIZERS[0], aud=AUDIENCES[11], link_p=0.0)
        evs.append(ev)
    evs[0]["link"] = reg_link("olymp")
    out.append({"kind": "announcement", "category": "multi:qualifier_final", "ref": ref.isoformat(),
                "events": evs, "tags": ["multi_event"], "distractors": {}})

    # M5: день відкритих дверей + онлайн-вебінар для тих, хто не зможе прийти
    ref = rand_ref()
    d1 = future(ref, 8, 20)
    d2 = d1 - D(days=R.choice([2, 3]))
    _, ev1 = event_slot("other", "dm", "colon", "hall", used, fixed_date=(ref, dm(d1), d1, False, {}))
    ev1["topic"] = "день відкритих дверей"
    ABIT = ("абітурієнти та їхні батьки", [["абітурієнт"]], "абітурієнтів та їхніх батьків")
    finish(ev1, org=None, aud=ABIT, link_p=0.0)
    _, ev2 = event_slot("webinar", "dm", "colon", "online", used, fixed_date=(ref, dm(d2), d2, False, {}))
    ev2["topic"] = "онлайн-зустріч з приймальною комісією"
    finish(ev2, org=None, aud=ABIT)
    out.append({"kind": "announcement", "category": "multi:open_day_plus_webinar", "ref": ref.isoformat(),
                "events": [ev1, ev2], "tags": ["multi_event", "online"], "distractors": {}})

    # M6: два майстер-класи від компанії у два дні поспіль
    ref = rand_ref()
    d1 = future(ref, 6, 20)
    if d1.weekday() == 5:
        d1 -= D(days=1)
    d2 = d1 + D(days=1)
    loc, keys = room_phrase(workshop=True)
    comp = R.choice(COMPANIES)
    evs = []
    for d in (d1, d2):
        _, ev = event_slot("workshop", "dm", "colon", "room", used, fixed_date=(ref, dm(d), d, False, {}))
        ev.update(location=loc, loc_keys=keys)
        finish(ev, org=comp, aud=AUDIENCES[10], link_p=0.0)
        evs.append(ev)
    out.append({"kind": "announcement", "category": "multi:company_two_days", "ref": ref.isoformat(),
                "events": evs, "tags": ["multi_event"], "distractors": {}})

    # M7: дві події в один день у різний час
    ref = rand_ref()
    d = future(ref, 5, 20)
    evs = []
    for et in ("seminar", "seminar"):
        _, ev = event_slot(et, "dm", "colon", "hall", used, fixed_date=(ref, dm(d), d, False, {}))
        finish(ev, org=ORGANIZERS[9], aud=AUDIENCES[5], link_p=0.0)
        evs.append(ev)
    if evs[0]["time"] == evs[1]["time"]:
        evs[1]["time_phrase"], evs[1]["time"] = "о 16:00", "16:00"
    out.append({"kind": "announcement", "category": "multi:same_day", "ref": ref.isoformat(),
                "events": evs, "tags": ["multi_event", "same_day"], "distractors": {}})

    # M8: інтелектуальна гра + кіноклуб на одному тижні
    ref = rand_ref()
    d1 = future(ref, 4, 12)
    d2 = d1 + D(days=R.choice([1, 2]))
    if d2.weekday() == 6:
        d2 += D(days=1)
    evs = []
    for d, tp_ in ((d1, "інтелектуальна гра «Що? Де? Коли?»"), (d2, "кіноклуб: показ фільму «Гра в імітацію»")):
        _, ev = event_slot("other", "dm", "colon", "hall", used, fixed_date=(ref, dm(d), d, False, {}))
        ev["topic"] = tp_
        ev["type_accept"] = TYPE_ACCEPT.get(tp_, [])
        finish(ev, org=ORGANIZERS[4], aud=AUDIENCES[7], link_p=0.0)
        evs.append(ev)
    out.append({"kind": "announcement", "category": "multi:two_leisure", "ref": ref.isoformat(),
                "events": evs, "tags": ["multi_event"], "distractors": {}})
    return out


TRAP_PLAN = [  # (подтип, количество)
    ("question", 3), ("past_report", 4), ("chat_memory", 2), ("other_university", 3),
    ("poll", 3), ("cancelled", 3), ("office_hours", 3), ("requirements", 3),
    ("publication", 3), ("greeting", 3),
    ("boundary_deadline_only", 2), ("boundary_exam", 2), ("boundary_defense_schedule", 1),
]


def build_traps():
    out = []
    for sub, n in TRAP_PLAN:
        for _ in range(n):
            ref = rand_ref()
            t = R.choice(TIMES)
            room, _k = room_phrase()
            past = ref - D(days=R.randint(1, 9))
            fut = future(ref, 3, 25)
            dis = {"time": t, "room": room}
            if sub in ("past_report", "chat_memory"):
                dis["past_date_phrase"] = dm(past)
            if sub in ("cancelled", "boundary_deadline_only", "boundary_exam", "requirements",
                       "other_university", "publication"):
                dis["date_phrase"] = dm(fut)
            if sub == "poll":
                w1, w2 = R.sample([1, 2, 3, 4], 2)
                dis["options"] = [f"у {WD_ACC[w1]}", f"у {WD_ACC[w2]}"]
            if sub == "boundary_exam":
                dis["old_date_phrase"] = dm(fut - D(days=R.randint(2, 4)))
            if sub == "boundary_defense_schedule":
                ref = rand_ref(lo=dt.date(2026, 11, 30), hi=dt.date(2026, 12, 8))
                d1 = future(ref, 6, 12)
                dis["dates"] = [dm(d1), dm(d1 + D(days=1))]
            if sub == "greeting":
                dis["holiday"] = R.choice(["День студента", "День працівників освіти",
                                           "День української писемності та мови"])
            out.append({"kind": "trap", "category": f"trap:{sub}", "ref": ref.isoformat(),
                        "events": [], "tags": ["boundary"] if sub.startswith("boundary") else [],
                        "distractors": dis})
    return out


def show(m):
    """Компактная строка для автора текстов."""
    ref = dt.date.fromisoformat(m["ref"])
    head = f"{m['id']} {m['kind'][:5]:5} {m['category']:28} ref {m['ref']} {WD_EN[ref.weekday()]}"
    lines = [head]
    for e in m["events"]:
        d = dt.date.fromisoformat(e["date"])
        parts = [f"  [{e['event_type']}] {e['topic']}",
                 f"date «{e['date_phrase']}»->{e['date']} {WD_EN[d.weekday()]}"
                 + (f" до {e['range_end']}" if e.get("range_end") else ""),
                 f"time «{e['time_phrase']}»" if e["time_phrase"] else "time -",
                 f"loc «{e['location']}»" if e["location"] else "loc -"]
        if e.get("code_phrase"):
            parts.append(f"code «{e['code_phrase']}»")
        if e["link"]:
            parts.append(f"link {e['link']}")
        parts.append(f"org «{e['organizer']}»" if e["organizer"] else "org -")
        parts.append(f"aud «{e['aud_phrase']}»" if e["target_audience"] else "aud -")
        lines.append(" | ".join(parts))
    if m["distractors"]:
        lines.append("  distr " + json.dumps(m["distractors"], ensure_ascii=False))
    return "\n".join(lines)


if __name__ == "__main__":
    msgs = build()
    Path("slots.json").write_text(json.dumps(msgs, ensure_ascii=False, indent=1), encoding="utf-8")
    if "--quiet" not in sys.argv:
        for m in msgs:
            print(show(m))
    kinds = {}
    for m in msgs:
        kinds[m["kind"]] = kinds.get(m["kind"], 0) + 1
    print("messages:", len(msgs), kinds, "gold events:", sum(len(m["events"]) for m in msgs))
