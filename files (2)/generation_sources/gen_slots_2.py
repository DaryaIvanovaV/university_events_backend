# -*- coding: utf-8 -*-
"""
Вторая сотня слотов (3101-3200): та же схема «сначала эталон, потом текст»,
но свой seed и новые пулы тем, организаторов и мест — чтобы тексты не
повторяли первую сотню.

Состав смещён туда, где первой сотни мало:
  * больше сообщений с несколькими событиями (10 вместо 8, три из них — по 3 события),
    включая два потока в один день с одинаковым названием;
  * 4 переноса: на новую дату, «на завтра» и перенос только времени;
  * 4 массовые и 7 индивидуальных консультаций;
  * новые формы относительных дат: «цього четверга», «у найближчу суботу»,
    «через два тижні» (все сводятся к правилам 1-3 промпта).

Запуск:  python gen_slots_2.py  ->  slots_2.json
"""
from __future__ import annotations

import datetime as dt
import json
import random
import sys
from pathlib import Path

import gen_slots as G

G.R = random.Random(20260922)  # функции gen_slots берут генератор из своего модуля
R = G.R
D = dt.timedelta

# --- пулы: организаторы, места и аудитории дополняются, темы — полностью новые ---
G.ORGANIZERS += [
    ("кафедра інформаційних технологій", [["інформаційних технологій"]]),
    ("студентський медіацентр", [["медіацентр"]]),
    ("екологічний клуб «Зелений кампус»", [["зелений кампус"]]),
    ("спортивний клуб університету", [["спортивн"], ["клуб"]]),
    ("IT-клуб факультету", [["it-клуб"]]),
]
G.COMPANIES = [("компанія Quantix", [["quantix"]]), ("компанія RiverStack", [["riverstack"]]),
               ("компанія Tessera Labs", [["tessera"]])]
G.PARTICIPANTS = ["Quantix", "RiverStack", "Tessera Labs", "Lumen Apps", "SoftGrid", "NordByte"]
G.HALLS += [
    ("стадіон університету", [["стадіон"]]),
    ("біля головного входу до університету", [["головного входу"]]),
    ("галерея другого поверху бібліотеки", [["галере"]]),
    ("волонтерський хаб університету", [["хаб"]]),
    ("кінозал студентського клубу", [["кінозал"]]),
    ("медіатека корпусу 2", [["медіатек", "медіатец"]]),
]
G.ADDRESSES = [("вул. Садова, 3", [["садов"]]), ("вул. Паркова, 9", [["парков"]])]
G.AUDIENCES += [
    ("студенти 3 курсу", [["3 курс"]], "студентів 3 курсу"),
    ("студенти 1–2 курсів", [["1-2 курс"]], "студентів 1–2 курсів"),
    ("іноземні студенти", [["іноземн"]], "іноземних студентів"),
    ("школярі 10–11 класів", [["10-11 клас"]], "школярів 10–11 класів"),
    ("молоді викладачі", [["молод"], ["викладач"]], "молодих викладачів"),
]
G.AUD = {a[0]: a for a in G.AUDIENCES}
G.AUD["абітурієнти та їхні батьки"] = ("абітурієнти та їхні батьки", [["абітурієнт"]],
                                      "абітурієнтів та їхніх батьків")
G.ORG = {name: (name, keys) for name, keys in G.ORGANIZERS + G.COMPANIES}
G.DEPTS = [o[0] for o in G.ORGANIZERS[:4]] + ["кафедра інформаційних технологій"]
D5 = G.DEPTS
G.ORG_BY_TYPE = {
    "lecture": D5 + ["студентське наукове товариство", "IT-клуб факультету"],
    "seminar": D5 + ["студентське наукове товариство", "науковий гурток «Data Lab»"],
    "conference": D5 + ["студентське наукове товариство", "відділ міжнародних зв'язків"],
    "workshop": D5 + ["IT-клуб факультету", "студентський медіацентр", "науковий гурток «Data Lab»"],
    "webinar": D5 + ["Центр кар'єри університету", "наукова бібліотека університету"],
    "hackathon": D5 + ["IT-клуб факультету", "студентська рада факультету"],
}
G.TOPICS = {
    "lecture": ["як працює компілятор: від тексту до машинного коду", "мікросервіси: коли вони шкодять",
                "захист критичної інфраструктури", "цифрові двійники у промисловості",
                "теорія ігор для розробників", "обробка природної мови для української",
                "блокчейн без хайпу", "як влаштовані пошукові системи",
                "open source: як стати контриб'ютором", "алгоритми стиснення даних",
                "енергоефективне програмування", "безпека мобільних застосунків"],
    "seminar": ["семінар «Методи оптимізації в логістиці»", "засідання гуртка з комп'ютерної лінгвістики",
                "круглий стіл «Цифрова доступність університетських сервісів»",
                "науковий семінар «Нечітка логіка в системах керування»",
                "семінар «Статистика для дослідників: типові помилки»",
                "круглий стіл «Етика досліджень за участю людей»", "семінар з квантової криптографії",
                "науковий семінар «Дифузійні моделі для генерації зображень»"],
    "workshop": ["майстер-клас з Unity для початківців", "воркшоп з Kubernetes",
                 "практикум з алгоритмічних задач для співбесід", "майстер-клас з 3D-друку",
                 "воркшоп з React Native", "практикум з аналізу даних у pandas",
                 "майстер-клас з тайм-менеджменту", "воркшоп з технічної документації", "практикум з OSINT"],
    "webinar": ["вебінар «Як отримати грант на дослідження»", "вебінар «Стажування за кордоном для ІТ-студентів»",
                "вебінар «Академічна доброчесність і ШІ-інструменти»", "вебінар «Кар'єра в тестуванні»",
                "вебінар «Як не вигоріти під час сесії»", "вебінар «Відкриті наукові дані»",
                "вебінар «Менеджер бібліографії Zotero»", "вебінар «Інклюзивний дизайн інтерфейсів»"],
    "conference": ["конференція «Штучний інтелект в освіті та науці»",
                   "студентська конференція «Кібербезпека: погляд молоді»",
                   "науково-практична конференція «Програмна інженерія: виклики та рішення»",
                   "конференція «Інформаційні технології в аграрному секторі»",
                   "форум молодих науковців «Наука без кордонів»"],
    "hackathon": ["хакатон «EdTech Challenge»", "змагання з робототехніки «RoboRace»",
                  "конкурс мобільних застосунків", "хакатон з відкритих даних", "турнір з програмування в парах"],
    "other": ["фестиваль настільних ігор", "екоакція з прибирання парку", "концерт студентської самодіяльності",
              "презентація студентських клубів", "спортивне свято факультету", "квест для першокурсників",
              "фотовиставка «Факультет крізь об'єктив»", "книжковий обмін", "майстерня з виготовлення окопних свічок"],
}
G.TYPE_ACCEPT.update({
    "змагання з робототехніки «RoboRace»": ["other"], "конкурс мобільних застосунків": ["other"],
    "турнір з програмування в парах": ["other"], "квест для першокурсників": ["hackathon"],
    "майстерня з виготовлення окопних свічок": ["workshop"],
})
G.TOPIC_ORG.update({
    "вебінар «Стажування за кордоном для ІТ-студентів»": ["відділ міжнародних зв'язків", "Центр кар'єри університету"],
    "вебінар «Менеджер бібліографії Zotero»": ["наукова бібліотека університету"],
    "вебінар «Відкриті наукові дані»": ["наукова бібліотека університету"],
    "вебінар «Кар'єра в тестуванні»": ["Центр кар'єри університету"],
    "вебінар «Як не вигоріти під час сесії»": ["психологічна служба університету"],
    "майстер-клас з тайм-менеджменту": ["психологічна служба університету", "студентська рада факультету"],
    "майстер-клас з 3D-друку": ["IT-клуб факультету"],
    "фестиваль настільних ігор": ["студентська рада факультету"],
    "екоакція з прибирання парку": ["екологічний клуб «Зелений кампус»", "волонтерський центр університету"],
    "концерт студентської самодіяльності": ["студентська рада факультету"],
    "презентація студентських клубів": ["студентська рада факультету"],
    "спортивне свято факультету": ["спортивний клуб університету", "студентська рада факультету"],
    "квест для першокурсників": ["студентська рада факультету"],
    "фотовиставка «Факультет крізь об'єктив»": ["студентський медіацентр"],
    "книжковий обмін": ["наукова бібліотека університету"],
    "майстерня з виготовлення окопних свічок": ["волонтерський центр університету"],
})
G.TOPIC_HALL.update({
    "фестиваль настільних ігор": ["коворкінг на третьому поверсі корпусу 1", "кінозал студентського клубу"],
    "екоакція з прибирання парку": ["біля головного входу до університету"],
    "концерт студентської самодіяльності": ["актова зала головного корпусу"],
    "презентація студентських клубів": ["фойє корпусу 3"],
    "спортивне свято факультету": ["стадіон університету"],
    "квест для першокурсників": ["біля головного входу до університету"],
    "фотовиставка «Факультет крізь об'єктив»": ["галерея другого поверху бібліотеки"],
    "книжковий обмін": ["читальна зала бібліотеки", "медіатека корпусу 2"],
    "майстерня з виготовлення окопних свічок": ["волонтерський хаб університету"],
})
G.TOPIC_AUD.update({
    "квест для першокурсників": ["першокурсники", "студенти 1 курсу"],
    "презентація студентських клубів": ["першокурсники", "студенти 1–2 курсів"],
})

WD_THIS = ["цього понеділка", "цього вівторка", "цієї середи", "цього четверга", "цієї п'ятниці", "цієї суботи"]
WD_NEAREST = ["у найближчий понеділок", "у найближчий вівторок", "у найближчу середу",
              "у найближчий четвер", "у найближчу п'ятницю", "у найближчу суботу"]
_base_date_slot = G.date_slot


def date_slot2(kind):
    """Новые формы относительных дат; остальные — как в первой сотне."""
    if kind == "this_weekday":
        ref = G.rand_ref(weekdays=(0, 1, 2))
        w = R.randint(ref.weekday() + 1, 4)
        return ref, WD_THIS[w], G.bare_weekday(ref, w), True, {}
    if kind == "nearest_weekday":
        ref = G.rand_ref(weekdays=(0, 1, 2, 3))
        w = R.choice([x for x in (3, 4, 5) if x > ref.weekday()])
        return ref, WD_NEAREST[w], G.bare_weekday(ref, w), True, {}
    if kind == "in_2weeks":
        ref = G.rand_ref(hi=dt.date(2026, 12, 4))
        return ref, "через два тижні", ref + D(days=14), True, {}
    return _base_date_slot(kind)


G.date_slot = date_slot2
REL = {"today", "tomorrow", "day_after", "bare_weekday", "next_weekday", "in_week", "in_3days",
       "this_weekday", "nearest_weekday", "in_2weeks"}


def tags_for(ev, ref, dk, tk, lk):
    tags = []
    if dk in REL:
        tags.append(f"relative:{dk}")
    if ev.get("ambiguous"):
        tags.append("ambiguous:next_weekday")
    if dk == "range":
        tags.append("date_range")
    if dt.date.fromisoformat(ev["date"]).year > ref.year:
        tags.append("year_rollover")
    for cond, tag in ((tk == "range", "time_span"), (tk == "none", "no_time"), (lk == "none", "no_location"),
                      (lk == "online", "online"), (ev["organizer"] is None, "no_organizer"),
                      (ev["target_audience"] is None, "no_audience")):
        if cond:
            tags.append(tag)
    return tags


def singles(used):
    out = []
    date_kinds = (["dm"] * 6 + ["dm_wd"] * 4 + ["num"] * 3 + ["num_year"] * 2 + ["year"] * 2
                  + ["cross_year"] * 2 + ["range"] * 4 + ["today"] * 2 + ["tomorrow"] * 3 + ["day_after"] * 2
                  + ["bare_weekday"] * 3 + ["next_weekday"] * 2 + ["in_week"] + ["this_weekday"] * 2
                  + ["nearest_weekday"] + ["in_2weeks"])
    types = (["lecture"] * 8 + ["seminar"] * 6 + ["workshop"] * 7 + ["webinar"] * 6
             + ["conference"] * 4 + ["hackathon"] * 4 + ["other"] * 5)
    assert len(date_kinds) == len(types) == 40
    R.shuffle(date_kinds)
    R.shuffle(types)
    for i, t in enumerate(types):  # конференции — к диапазонам дат
        if t == "conference" and "range" in date_kinds[i + 1:] and date_kinds[i] != "range":
            j = date_kinds.index("range", i + 1)
            date_kinds[i], date_kinds[j] = date_kinds[j], date_kinds[i]
    time_kinds = ["colon"] * 24 + ["dot"] * 4 + ["start"] * 4 + ["range"] * 4 + ["none"] * 4
    R.shuffle(time_kinds)
    locs = ["room"] * 16 + ["hall"] * 10 + ["address"] * 3 + ["online"] * 2 + ["none"] * 3
    R.shuffle(locs)
    deadline_left = 5
    for et, dk, tk in zip(types, date_kinds, time_kinds):
        lk = "online" if et == "webinar" else locs.pop()
        if lk == "none" and tk == "none":
            tk = "colon"
        ref, ev = G.event_slot(et, dk, tk, lk, used)
        G.finish(ev)
        tags = tags_for(ev, ref, dk, tk, lk)
        dis = {}
        explicit = dk in ("dm", "dm_wd", "num", "num_year", "year", "range")
        gap = (dt.date.fromisoformat(ev["date"]) - ref).days
        if explicit and deadline_left and ev["link"] and gap > 6:
            dis["deadline_phrase"] = G.dm(ref + D(days=R.randint(2, gap - 2)))
            tags.append("deadline_mention")
            deadline_left -= 1
        out.append({"kind": "announcement", "category": "single", "ref": ref.isoformat(),
                    "events": [ev], "tags": tags, "distractors": dis})
    return out


def dates_ok(*ds):
    return all(d.weekday() != 6 and not G.in_break(d) for d in ds)


def fixed(ref, d, phrase=None, inferred=False):
    return (ref, phrase or G.dm(d), d, inferred, {})


def multi(used):
    out = []

    def add(category, ref, evs, tags=()):
        out.append({"kind": "announcement", "category": category, "ref": ref.isoformat(),
                    "events": evs, "tags": ["multi_event", *tags], "distractors": {}})

    # M1: один воркшоп у двох сесіях через тиждень
    while True:
        ref = G.rand_ref(); d1 = G.future(ref, 5, 15); d2 = d1 + D(days=7)
        if dates_ok(d2):
            break
    tp, tv, _ = G.time_slot("colon")
    loc, keys = G.room_phrase(workshop=True)
    evs = []
    for d in (d1, d2):
        _, ev = G.event_slot("workshop", "dm", "none", "room", used, fixed_date=fixed(ref, d))
        ev.update(time_phrase=tp, time=tv, time_kind="colon", location=loc, loc_keys=keys)
        G.finish(ev, org=G.ORG["IT-клуб факультету"], aud=G.AUD["студенти 1–2 курсів"], link_p=0)
        evs.append(ev)
    evs[1]["topic"] = evs[0]["topic"]
    add("multi:two_sessions", ref, evs)

    # M2: дайджест тижня від понеділка — вівторок, середа, п'ятниця
    ref = G.rand_ref(weekdays=(0,))
    evs = []
    for w, et in ((1, "lecture"), (2, "workshop"), (4, "other")):
        _, ev = G.event_slot(et, "bare_weekday", "colon", R.choice(["room", "hall"]), used,
                             fixed_date=fixed(ref, G.bare_weekday(ref, w), f"у {G.WD_ACC[w]}", True))
        G.finish(ev, link_p=0)
        evs.append(ev)
    add("multi:digest", ref, evs, ["relative:bare_weekday"])

    # M3: цикл із трьох лекцій щотижня
    while True:
        ref = G.rand_ref(); d1 = G.future(ref, 4, 12); ds = [d1 + D(days=7 * k) for k in range(3)]
        if dates_ok(*ds):
            break
    tp, tv, _ = G.time_slot("colon")
    loc, keys = R.choice(G.HALLS[:8])
    org = G.ORG[R.choice(G.DEPTS)]
    evs = []
    for d in ds:
        _, ev = G.event_slot("lecture", "dm", "none", "hall", used, fixed_date=fixed(ref, d))
        ev.update(time_phrase=tp, time=tv, time_kind="colon", location=loc, loc_keys=keys)
        G.finish(ev, org=org, aud=G.AUD["усі охочі"], link_p=0)
        evs.append(ev)
    add("multi:lecture_series3", ref, evs)

    # M4: кваліфікація та фінал змагань з робототехніки
    while True:
        ref = G.rand_ref(); d1 = G.future(ref, 7, 16); d2 = d1 + D(days=R.choice([7, 14]))
        if dates_ok(d2):
            break
    evs = []
    for d in (d1, d2):
        _, ev = G.event_slot("hackathon", "dm", "colon", "hall", used, fixed_date=fixed(ref, d))
        ev["topic"], ev["type_accept"] = "змагання з робототехніки «RoboRace»", ["other"]
        ev["location"], ev["loc_keys"] = "лабораторія робототехніки корпусу 5", [["робототехн"]]
        G.finish(ev, org=G.ORG["IT-клуб факультету"], aud=G.AUD["студенти всіх курсів"], link_p=0)
        evs.append(ev)
    evs[0]["link"] = G.reg_link("robo")
    add("multi:qualifier_final", ref, evs)

    # M5: конференція + підготовчий онлайн-вебінар для доповідачів
    while True:
        ref = G.rand_ref(); d1 = G.future(ref, 12, 25); d2 = d1 - D(days=R.choice([4, 5, 6]))
        if dates_ok(d1, d2) and d2 > ref:
            break
    _, ev1 = G.event_slot("conference", "dm", "colon", "hall", used, fixed_date=fixed(ref, d1))
    G.finish(ev1, org=G.ORG["студентське наукове товариство"], aud=G.AUD["студенти 3 курсу"], link_p=0)
    _, ev2 = G.event_slot("webinar", "dm", "colon", "online", used, fixed_date=fixed(ref, d2))
    ev2["topic"] = "онлайн-інструктаж для доповідачів конференції"
    G.finish(ev2, org=G.ORG["студентське наукове товариство"], aud=None)
    add("multi:conference_plus_webinar", ref, [ev1, ev2], ["online"])

    # M6: два воркшопи компанії у два дні поспіль
    while True:
        ref = G.rand_ref(); d1 = G.future(ref, 6, 18); d2 = d1 + D(days=1)
        if d1.weekday() < 5 and dates_ok(d2):
            break
    loc, keys = G.room_phrase(workshop=True)
    comp = R.choice(G.COMPANIES)
    evs = []
    for d in (d1, d2):
        _, ev = G.event_slot("workshop", "dm", "colon", "room", used, fixed_date=fixed(ref, d))
        ev.update(location=loc, loc_keys=keys)
        G.finish(ev, org=comp, aud=G.AUD["студенти 3 курсу"], link_p=0)
        evs.append(ev)
    add("multi:company_two_days", ref, evs)

    # M7: семінар і круглий стіл в один день
    ref = G.rand_ref(); d = G.future(ref, 5, 20)
    evs = []
    for _ in range(2):
        _, ev = G.event_slot("seminar", "dm", "colon", "hall", used, fixed_date=fixed(ref, d))
        G.finish(ev, org=G.ORG["кафедра штучного інтелекту та аналізу даних"], aud=G.AUD["молоді викладачі"], link_p=0)
        evs.append(ev)
    if evs[0]["time"] >= evs[1]["time"]:
        evs[0]["time_phrase"], evs[0]["time"] = "о 10:00", "10:00"
        evs[1]["time_phrase"], evs[1]["time"] = "о 14:00", "14:00"
    add("multi:same_day", ref, evs, ["same_day"])

    # M8: фестиваль настільних ігор + концерт
    while True:
        ref = G.rand_ref(); d1 = G.future(ref, 4, 12); d2 = d1 + D(days=R.choice([2, 3, 5]))
        if dates_ok(d2):
            break
    evs = []
    for d, topic in ((d1, "фестиваль настільних ігор"), (d2, "концерт студентської самодіяльності")):
        _, ev = G.event_slot("other", "dm", "colon", "hall", used, fixed_date=fixed(ref, d))
        ev["topic"], ev["type_accept"] = topic, []
        G.finish(ev, org=G.ORG["студентська рада факультету"], aud=G.AUD["усі охочі"], link_p=0)
        evs.append(ev)
    add("multi:two_leisure", ref, evs)

    # M9: анонси місяця — три різні події з явними датами
    while True:
        ref = G.rand_ref(); ds = sorted({G.future(ref, 4, 30) for _ in range(3)})
        if len(ds) == 3 and dates_ok(*ds):
            break
    evs = []
    for d, et in zip(ds, ("lecture", "workshop", "other")):
        _, ev = G.event_slot(et, "dm", "colon", R.choice(["room", "hall"]), used, fixed_date=fixed(ref, d))
        G.finish(ev, link_p=0)
        evs.append(ev)
    add("multi:month_digest", ref, evs)

    # M10: два потоки одного практикуму в один день (однакова назва, різний час)
    ref = G.rand_ref(); d = G.future(ref, 4, 14)
    loc, keys = G.room_phrase(workshop=True)
    evs = []
    for tp, tv in (("о 10:00", "10:00"), ("о 12:00", "12:00")):
        _, ev = G.event_slot("workshop", "dm", "none", "room", used, fixed_date=fixed(ref, d))
        ev.update(time_phrase=tp, time=tv, time_kind="colon", location=loc, loc_keys=keys,
                  topic="практикум з роботи в Moodle")
        G.finish(ev, org=G.ORG["кафедра інформаційних технологій"], aud=G.AUD["першокурсники"], link_p=0)
        evs.append(ev)
    add("multi:two_streams_same_day", ref, evs, ["same_day"])
    return out


def postponed(used):
    out = []
    for et in ("seminar", "workshop"):  # новая явная дата
        ref = G.rand_ref()
        old = G.future(ref, 4, 12)
        new = G.future(ref, (old - ref).days + 3, (old - ref).days + 12)
        _, ev = G.event_slot(et, "dm", "colon", "room", used, fixed_date=fixed(ref, new))
        G.finish(ev, link_p=0)
        out.append({"kind": "announcement", "category": "postponed", "ref": ref.isoformat(),
                    "events": [ev], "tags": ["postponed"], "distractors": {"old_date_phrase": G.dm(old)}})
    # перенос «з сьогодні на завтра»
    ref = G.rand_ref(weekdays=(0, 1, 2, 3))
    _, ev = G.event_slot("lecture", "tomorrow", "colon", "room", used,
                         fixed_date=(ref, "завтра", ref + D(days=1), True, {}))
    G.finish(ev, link_p=0)
    out.append({"kind": "announcement", "category": "postponed", "ref": ref.isoformat(), "events": [ev],
                "tags": ["postponed", "relative:tomorrow"], "distractors": {"old_date_phrase": "сьогодні"}})
    # перенос только времени, дата прежняя
    ref = G.rand_ref()
    d = G.future(ref, 4, 15)
    _, ev = G.event_slot("lecture", "dm", "none", "hall", used, fixed_date=fixed(ref, d))
    old_t, new_t = R.choice([("10:00", "12:00"), ("13:00", "15:00"), ("16:00", "18:00")])
    ev.update(time_kind="colon", time_phrase=f"на {int(new_t[:2])}:{new_t[3:]}", time=new_t)
    G.finish(ev, link_p=0)
    out.append({"kind": "announcement", "category": "postponed", "ref": ref.isoformat(), "events": [ev],
                "tags": ["postponed", "time_change"],
                "distractors": {"old_time_phrase": f"{int(old_t[:2])}:{old_t[3:]}"}})
    return out


def consultations(used):
    out = []
    mass = [("загальна консультація перед модульною контрольною з дискретної математики", "студенти 1 курсу"),
            ("консультація щодо вибору вибіркових дисциплін", "студенти 2 курсу"),
            ("настановча консультація з виробничої практики", "студенти 3 курсу"),
            ("консультація для першокурсників щодо роботи в Moodle", "першокурсники")]
    for topic, aud in mass:
        ref = G.rand_ref(); d = G.future(ref, 3, 14)
        _, ev = G.event_slot("other", "dm", "colon", R.choice(["room", "online"]), used, fixed_date=fixed(ref, d))
        ev.update(event_type="consultation", type_accept=[], topic=topic)
        G.finish(ev, org=None, aud=G.AUD[aud], link_p=0)
        out.append({"kind": "announcement", "category": "mass_consultation", "ref": ref.isoformat(),
                    "events": [ev], "tags": ["consultation_mass"] + (["online"] if ev["link"] else []),
                    "distractors": {}})
    individual = [
        ("консультації щодо академічної різниці", "за попередньою домовленістю"),
        ("консультації з магістерських робіт", "час зустрічі кожен узгоджує з науковим керівником"),
        ("консультації з програмування для тих, хто пропустив модуль", "індивідуальні консультації"),
        ("зустрічі з куратором щодо індивідуального плану", "у форматі особистих зустрічей"),
        ("консультації для боржників з математичного аналізу", "кожному студенту призначено окремий час"),
        ("консультації щодо тем кваліфікаційних робіт", "лише за записом через форму"),
        ("консультації щодо звітів з науково-дослідної практики", "у порядку черги за списком, який надішле староста"),
    ]
    for topic, marker in individual:
        ref = G.rand_ref(); d = G.future(ref, 2, 12)
        _, ev = G.event_slot("other", "dm_wd", R.choice(["colon", "range"]), "room", used,
                             fixed_date=(ref, f"у {G.WD_ACC[d.weekday()]}, {G.dm(d)}", d, False, {}))
        room = R.randint(2, 5) * 100 + R.randint(10, 35)
        ev.update(event_type="consultation", type_accept=[], topic=topic,
                  location=f"каб. {room}", loc_keys=[[str(room)]])
        G.finish(ev, org=None, aud=None, link_p=0)
        out.append({"kind": "individual", "category": "individual", "ref": ref.isoformat(), "events": [ev],
                    "tags": ["consultation_individual"], "distractors": {"individual_marker": marker}})
    return out


def traps():
    out = G.build_traps()
    holidays = [("День програміста", dt.date(2026, 9, 13)),
                ("День захисників і захисниць України", dt.date(2026, 10, 1)),
                ("День Збройних Сил України", dt.date(2026, 12, 6))]
    for m, (name, day) in zip([m for m in out if m["category"] == "trap:greeting"], holidays):
        m["distractors"]["holiday"] = name
        m["ref"] = day.isoformat()  # привітання публікується в день свята
    return out


def build2():
    used: dict = {}
    msgs = singles(used) + multi(used) + postponed(used) + consultations(used) + traps()
    R.shuffle(msgs)
    for i, m in enumerate(msgs):
        m["id"] = 3101 + i
    return msgs


if __name__ == "__main__":
    msgs = build2()
    Path("slots_2.json").write_text(json.dumps(msgs, ensure_ascii=False, indent=1), encoding="utf-8")
    kinds: dict = {}
    for m in msgs:
        kinds[m["kind"]] = kinds.get(m["kind"], 0) + 1
    if "--quiet" not in sys.argv:
        for m in msgs:
            print(G.show(m))
    print("messages:", len(msgs), kinds, "gold events:", sum(len(m["events"]) for m in msgs))
