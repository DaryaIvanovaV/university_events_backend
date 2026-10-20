# -*- coding: utf-8 -*-
"""Выборка для ручной проверки эталона: 25 сообщений, пропорционально типам, seed фиксирован."""
import datetime as dt, json, random
from pathlib import Path

import sys

PART2 = "--part2" in sys.argv
SEED = 20260923 if PART2 else 20260922
data = json.loads(Path("synthetic_messages.json").read_text(encoding="utf-8"))
# часть 1 — первая сотня (3001-3100), часть 2 — вторая (3101-3200)
data = [o for o in data if (o["source_message_id"] > 3100) == PART2]
R = random.Random(SEED)
quota = ({"announcement": 9, "trap": 5, "individual": 1} if PART2
         else {"announcement": 15, "trap": 9, "individual": 1})
pick = []
for kind, n in quota.items():
    pool = [o for o in data if o["gold"]["kind"] == kind]
    pick += R.sample(pool, n)
pick.sort(key=lambda o: o["source_message_id"])

WD = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
KIND = {"announcement": "анонс", "trap": "ловушка", "individual": "индивидуальная консультация"}
TRAP = {
    "question": "вопрос или слух, а не анонс", "past_report": "отчёт о прошедшем",
    "chat_memory": "воспоминание о прошедшем занятии", "other_university": "новость о событии в другом вузе",
    "poll": "опрос о дате, событие ещё не назначено", "cancelled": "событие отменено, новой даты нет",
    "office_hours": "график работы", "requirements": "требования / положение",
    "publication": "анонс публикации", "greeting": "поздравление с праздником",
    "boundary_deadline_only": "пограничный случай: только срок подачи, мероприятия нет",
    "boundary_exam": "пограничный случай: экзамен (не мероприятие факультета)",
    "boundary_defense_schedule": "пограничный случай: график защит для отдельных групп",
}
TYPE = {"conference": "конференция", "lecture": "лекция", "webinar": "вебинар", "seminar": "семинар",
        "workshop": "мастер-класс/воркшоп", "hackathon": "хакатон/конкурс", "consultation": "консультация",
        "other": "другое"}

def d_ru(s):
    d = dt.date.fromisoformat(s)
    return f"{d:%d.%m.%Y} ({WD[d.weekday()]})"

TITLE = ("# Выборочная проверка эталона — часть 2: 15 из второй сотни (3101–3200)" if PART2
         else "# Выборочная проверка эталона — 25 из 100")
LEAD = ("Сообщения выбраны случайно (seed 20260923), пропорционально типам: 9 анонсов, "
        "5 ловушек, 1 индивидуальная консультация. Вместе с первой частью — 40 из 200." if PART2
        else "Сообщения выбраны случайно (seed 20260922), пропорционально типам: 15 анонсов, "
        "9 ловушек, 1 индивидуальная консультация.")
out = [TITLE, "", LEAD, "",
       "**Что проверять.** Прочитайте текст и сверьте с блоком «Ожидается»: те ли даты "
       "(с учётом даты публикации), время, место, организатор, аудитория; действительно ли "
       "ловушка не содержит нового мероприятия. «—» значит, что в тексте этого нет и "
       "правильный ответ модели — null.", "",
       ("**Итог для статьи:** вместе с первой частью — «эталон верен в N из 40». " if PART2 else
        "**Итог для статьи:** «эталон верен в N из 25». ") + "Если найдёте ошибку — пришлите номер "
       "сообщения и что не так, исправлю набор и перепроверю валидатором.", ""]
for o in pick:
    g = o["gold"]
    ref = dt.date.fromisoformat(o["date"])
    out += ["---", "", f"## {o['source_message_id']} · {KIND[g['kind']]} · опубликовано {d_ru(o['date'])}", ""]
    out += ["> " + line if line else ">" for line in o["text"].split("\n")]
    out.append("")
    if g["kind"] == "trap":
        sub = g["category"].split(":", 1)[1]
        out += [f"**Ожидается:** событий нет — {TRAP[sub]}.", ""]
    else:
        n = len(g["events"])
        head = "**Ожидается:** " + (f"{n} событие" if n == 1 else f"{n} события")
        if g["kind"] == "individual":
            head += " (модель может его извлечь, но фильтр audience.py должен отсеять — в БД не попадает)"
        out += [head, ""]
        for i, e in enumerate(g["events"], 1):
            if n > 1:
                out.append(f"*Событие {i}*")
            v = lambda f: e[f]["value"] if e[f]["value"] else "—"
            date = d_ru(e["date"]["value"])
            if e["date"]["inferred"]:
                date += f" — из «{e['date']['phrase']}»"
            if e["date"].get("range_end"):
                date += f", до {d_ru(e['date']['range_end'])} (диапазон → в описание)"
            time = v("time") + (f" (промежуток {e['time']['span']})" if e["time"].get("span") else "")
            et = TYPE[e["event_type"]["value"]] + f" (`{e['event_type']['value']}`"
            et += (", допустимо: " + ", ".join(e["event_type"]["accept"]) if e["event_type"]["accept"] else "") + ")"
            out += [f"- Название: {e['event_title']['value']}", f"- Дата: {date}", f"- Время: {time}",
                    f"- Место: {v('location')}", f"- Организатор: {v('organizer')}",
                    f"- Аудитория: {v('target_audience')}", f"- Тип: {et}"]
            if e["link"]["value"]:
                out.append(f"- Ссылка: {e['link']['value']}")
            if e["meeting_code"]["value"]:
                out.append(f"- Код встречи: {e['meeting_code']['value']}")
            out.append("")
        if "ambiguous:next_weekday" in g["tags"]:
            out += ["*«наступного/наступної» посчитано по правилу 4 промпта (+7 дней к ближайшему такому дню).*", ""]
        if "deadline_mention" in g["tags"]:
            out += ["*Срок регистрации — только в описание, отдельным событием не считается.*", ""]
    out += ["Проверка: ☐ верно  ☐ ошибка: ____________", ""]
Path("audit_sample_2.md" if PART2 else "audit_sample.md").write_text("\n".join(out), encoding="utf-8")
print([o["source_message_id"] for o in pick])
