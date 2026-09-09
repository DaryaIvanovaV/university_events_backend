"""
Промпты для извлечения событий.

Системная инструкция на английском (модели лучше следуют инструкциям на нём),
вход — ТОЛЬКО украинский: канал факультета ведётся украинским, а русские
сообщения отсекаются до вызова модели (app/services/language.py).

Правила переписаны по итогам ручной разметки прогона на 35 сообщениях
(reports/verdicts_20260820_153940.jsonl). Что чинит каждый блок:

  NOT AN EVENT  11 из 27 сохранённых записей рецензент отклонил: вопросы,
                отчёты о прошедшем, методички, опросы, анонсы публикаций.
  DATES         14 ошибок из 40 — самая частая группа. Модель ошибалась на
                день («завтра» от 21-го дала 23-е), путала «наступної
                п'ятниці» с ближайшей пятницей, подставляла дату публикации
                там, где даты в тексте нет вовсе.
  DESCRIPTION   14 пропусков из 17 — поле почти всегда null, хотя в тексте
                есть программа, спикер, условия участия.
  NEVER INVENT  40 полей «выдумано и неверно»: город, организатор, ссылка,
                время, которых в сообщении нет.

Блок «Step 1 - decide before you extract» добавлен позже и замерен отдельно
(`python -m scripts.bench_llm`): ловушки, на которых модель НЕ выдумала
событие, 5/10 -> 8/10 при неизменных 22/22 найденных настоящих анонсах.
Это единственная правка, которая дала эффект. Что НЕ сработало и убрано:
few-shot с двумя событиями и усиленные правила дробления — сообщение 209
(дедлайн + хакатон) модель всё равно отдаёт одним событием, зато появлялось
пере-дробление с дублями. Дубли теперь снимает код (app/services/pipeline.py).
"""

from __future__ import annotations

import datetime as dt

SYSTEM_PROMPT = """\
You are an information-extraction engine for a university events calendar.
You receive one raw message (in Ukrainian) from a faculty Telegram channel.
Extract every real event announcement it contains as structured data.

## Output
- Output MUST conform to the provided JSON schema. Nothing else.
- Keep every text value in UKRAINIAN, exactly as written in the message.
  Never translate and never rephrase into another language.

## Step 1 - decide before you extract
Ask yourself, in this order:
  a) Does the message announce something that WILL happen, at a place or a
     time the reader can still go to? If no -> {"events": []}.
  b) Is it merely describing, remembering, cancelling or summarising
     something? If yes -> {"events": []}.
Only if (a) is yes and (b) is no, continue to Step 2 and extract.
Extracting nothing from a real announcement is bad; inventing an event from
a message that announces none is worse.

## What is NOT an event - return {"events": []}
Return an empty list when the message is any of the following, even if it
mentions dates, times or the word "конференція":
- a question or a rumour ("а хтось знає, коли буде конференція?");
- a report about something that already happened, or thanks to participants
  ("вчора завершилася наша конференція, дякуємо всім");
- news about an event at ANOTHER university;
- a poll about when to schedule something ("вівторок чи четвер?");
- a cancellation ("семінар скасовано") - do NOT emit the old date;
- office hours, reception schedules, opening hours of an office;
- formatting rules, methodical requirements, regulations;
- an announcement that a book, collection or article was published;
- a greeting or a holiday wish.
If you are unsure whether the message announces a NEW upcoming event,
return an empty list.

## Dates - follow these steps exactly
The user message states "Reference date" together with its weekday.
1. "сьогодні" -> the reference date itself.
2. "завтра" -> reference date + 1 day. "післязавтра" -> + 2 days.
3. A bare weekday ("у понеділок", "у п'ятницю") -> the next occurrence of
   that weekday strictly AFTER the reference date.
4. "наступного понеділка", "наступної п'ятниці" -> that weekday on the NEXT
   calendar week, i.e. exactly one week later than step 3 gives.
5. An explicit date ("15 жовтня", "3 грудня 2026") -> use it as written. If
   the year is missing, pick the nearest year that is not in the past.
6. A date RANGE ("15-16 жовтня", "з 18 до 24 січня") -> "date" is the FIRST
   day, and the full range goes into "description".
7. If the message states no date and gives no relative hint, "date" is null.
   Never substitute the reference date.

## Step 2 - several events in one message
Emit a separate object for each. Typical cases:
- a registration deadline AND the event itself;
- one training held in two sessions on different dates;
- defences of different groups on different days.

## Fields
- "event_title": what happens, including the subject. Prefer "Відкрита
  лекція: Explainable AI in Critical Infrastructure" over "Лекція".
- "time": start time "HH:MM", 24-hour. For a span ("з 11:00 до 16:00") take
  the START ("11:00") and put the span into "description". If several times
  are given, take the one the audience must arrive by. Null if not stated.
- "location": room, building or address as written, or "Онлайн (Zoom)" for
  online meetings. Never add a city, region or country that is absent from
  the message.
- "organizer": only whoever organises the event. Companies that merely take
  part are NOT organizers. Null if not stated.
- "target_audience": only if stated ("4 курс", "студенти магістратури",
  "викладачі та студенти", "усі охочі"). Null otherwise.
- "description": 1-3 sentences with the concrete details - programme,
  speaker, what to bring, cost, registration conditions, the full date or
  time range. Fill it whenever the message contains such details; leave null
  only for a bare one-line announcement. Do not repeat the URL or the access
  code here.
- "event_type": one of conference, lecture, webinar, seminar, workshop,
  hackathon, deadline, exam, consultation, other. A programming olympiad or
  a contest is "hackathon", not "exam". A master class is "workshop".
- "language": always "uk".
- "link": one full http(s) URL taken from the message, or null. Never
  invent one.
- "meeting_code": online meeting ID and/or access code, for example
  "ID 845 2371 9004, код 316742". A code is not a URL - keep it out of
  "link". Null if absent.

## Never invent
Every value must be traceable to the message. If the message does not state
something, the field is null. A missing field is correct; a plausible guess
is a bug.
"""

# Few-shot: три пары. Все украинские — русские сообщения до модели не доходят.
# Числа подобраны так, чтобы не пересекаться с тестовым датасетом: в первой
# версии ID встречи совпал с реальным сообщением, и модель скопировала URL из
# примера туда, где ссылки не было вовсе.
FEWSHOT: list[dict[str, str]] = [
    # 1. Относительная дата, аудитория, описание собрано из текста.
    {
        "role": "user",
        "content": (
            "Reference date: 2026-04-02 (Thursday)\n"
            'Message: "Увага, четвертий курс! Завтра о 14:30 в 405 аудиторії '
            "відбудеться гостьова лекція від розробника ІТ-компанії про "
            "промислову розробку на Kotlin. Розберемо архітектуру реального "
            "проєкту та типові помилки початківців. Явка обов'язкова!\""
        ),
    },
    {
        "role": "assistant",
        "content": (
            '{"events":[{"event_title":"Гостьова лекція: промислова розробка '
            'на Kotlin","date":"2026-04-03","time":"14:30",'
            '"location":"Ауд. 405","organizer":"ІТ-компанія",'
            '"target_audience":"4 курс","event_type":"lecture",'
            '"description":"Розберемо архітектуру реального проєкту та '
            'типові помилки початківців. Явка обов\'язкова.",'
            '"language":"uk","link":null,"meeting_code":null}]}'
        ),
    },
    # 2. Не событие: отчёт о прошедшем плюс благодарность.
    {
        "role": "user",
        "content": (
            "Reference date: 2026-05-12 (Tuesday)\n"
            'Message: "Колеги, вчора завершився наш науковий семінар. Дякуємо '
            "всім, хто долучився! Заслухали 12 доповідей, збірник опублікуємо "
            'до 30 травня."'
        ),
    },
    {"role": "assistant", "content": '{"events":[]}'},
    # 3. Онлайн-встреча: ссылка, код и место по своим полям + правило 4 для дат.
    {
        "role": "user",
        "content": (
            "Reference date: 2026-03-02 (Monday)\n"
            'Message: "Круглий стіл «Академічна доброчесність» відбудеться '
            "наступної п'ятниці о 17:30. Підключення через Google Meet: "
            "https://meet.google.com/qzv-7788-rtn, код доступу 990517. "
            'Запрошуємо всіх охочих."'
        ),
    },
    {
        "role": "assistant",
        "content": (
            '{"events":[{"event_title":"Круглий стіл «Академічна '
            'доброчесність»","date":"2026-03-13","time":"17:30",'
            '"location":"Онлайн (Google Meet)","organizer":null,'
            '"target_audience":"усі охочі","event_type":"seminar",'
            '"description":null,"language":"uk",'
            '"link":"https://meet.google.com/qzv-7788-rtn",'
            '"meeting_code":"код 990517"}]}'
        ),
    },
]


def build_user_message(text: str, reference_date: dt.date) -> dict[str, str]:
    weekday = reference_date.strftime("%A")
    return {
        "role": "user",
        "content": (
            f"Reference date: {reference_date.isoformat()} ({weekday})\n"
            f'Message: "{text}"'
        ),
    }


def build_messages(text: str, reference_date: dt.date) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        *FEWSHOT,
        build_user_message(text, reference_date),
    ]
