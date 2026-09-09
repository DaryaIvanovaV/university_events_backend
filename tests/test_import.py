"""Тесты парсеров импорта: Telegram-экспорт и набор симулированных сообщений."""

import datetime as dt

from app.ingestion.samples import iter_sample_messages
from app.ingestion.tg_export import flatten_text, iter_export_messages
from app.services.clock import today as clock_today


# --- Telegram Desktop export (result.json) ---


def test_flatten_plain_string():
    assert flatten_text("привіт") == "привіт"


def test_flatten_entity_list():
    # Telegram хранит форматированный текст как список строк и объектов.
    field = ["Лекція про ", {"type": "bold", "text": "Kotlin"}, " завтра"]
    assert flatten_text(field) == "Лекція про Kotlin завтра"


def test_flatten_unknown_type():
    assert flatten_text(12345) == ""


def test_iter_export_skips_service_and_empty():
    export = {
        "name": "@cs_faculty",
        "messages": [
            {"id": 1, "type": "service", "date": "2026-04-01T10:00:00", "text": "channel created"},
            {"id": 2, "type": "message", "date": "2026-04-02T14:30:00",
             "text": ["Лекція про ", {"type": "bold", "text": "Kotlin"}]},
            {"id": 3, "type": "message", "date": "2026-04-03T10:00:00", "text": ""},
            {"id": 4, "type": "message", "date": "bad-date", "text": "щось"},
        ],
    }
    msgs = list(iter_export_messages(export))
    assert len(msgs) == 1  # service, пустое и с битой датой отброшены
    assert msgs[0].message_id == 2
    assert msgs[0].text == "Лекція про Kotlin"
    assert msgs[0].date == dt.date(2026, 4, 2)
    assert msgs[0].channel == "@cs_faculty"


# --- Набор симулированных сообщений (JSONL) ---


def test_iter_samples_parses_jsonl():
    lines = [
        '{"text": "Конференція AI 1 вересня", "date": "2026-09-01", "source_channel": "@demo", "source_message_id": 101}',
        "",
        "# комментарий игнорируется",
        '{"text": "Вебінар про Docker", "date": "2026-04-24", "source_message_id": 102}',
    ]
    msgs = list(iter_sample_messages(lines))
    assert len(msgs) == 2
    assert msgs[0].message_id == 101
    assert msgs[0].channel == "@demo"
    assert msgs[0].date == dt.date(2026, 9, 1)
    assert msgs[1].channel == "@sample"  # дефолт, если канал не указан


def test_iter_samples_bad_date_falls_back_to_today():
    lines = ['{"text": "подія", "date": "не-дата", "source_message_id": 1}']
    msgs = list(iter_sample_messages(lines))
    # clock.today(), а не dt.date.today(): загрузчик считает «сегодня» по зоне
    # приложения, и в контейнере (UTC) эти две даты вечером расходятся.
    assert msgs[0].date == clock_today()
