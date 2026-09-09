"""
Тесты автоопределения формата входного файла (load_messages).

Пользователь присылает сообщения в том виде, в каком они у него есть —
скрипт проверки промта должен принимать JSONL, JSON-массив и экспорт
Telegram Desktop без ручной конвертации.
"""

import datetime as dt
import json

import pytest

from app.ingestion.samples import iter_sample_messages, load_messages
from app.services.clock import today as clock_today

JSONL = (
    '{"text": "Лекція про Kotlin", "date": "2026-04-02", '
    '"source_channel": "@demo", "source_message_id": 101}\n'
    "# комментарий\n"
    "\n"
    '{"text": "Вебінар про Docker", "date": "2026-04-24", "source_message_id": 102}\n'
)


@pytest.fixture
def write(tmp_path):
    def _write(name: str, content: str):
        path = tmp_path / name
        path.write_text(content, encoding="utf-8")
        return path

    return _write


def test_loads_jsonl(write):
    messages, warnings = load_messages(write("m.jsonl", JSONL))
    assert [m.message_id for m in messages] == [101, 102]
    assert messages[0].date == dt.date(2026, 4, 2)
    assert messages[0].channel == "@demo"
    assert messages[1].channel == "@sample"
    assert warnings == []


def test_loads_json_array(write):
    payload = [
        {"text": "Лекція про Kotlin", "date": "2026-04-02",
         "source_channel": "@demo", "source_message_id": 101},
        {"text": "Вебінар про Docker", "date": "2026-04-24", "source_message_id": 102},
    ]
    messages, warnings = load_messages(
        write("m.json", json.dumps(payload, ensure_ascii=False))
    )
    assert [m.message_id for m in messages] == [101, 102]
    assert messages[0].text == "Лекція про Kotlin"
    assert warnings == []


def test_loads_telegram_export(write):
    export = {
        "name": "@cs_faculty",
        "messages": [
            {"id": 1, "type": "service", "date": "2026-04-01T10:00:00", "text": "created"},
            {"id": 2, "type": "message", "date": "2026-04-02T14:30:00",
             "text": ["Лекція про ", {"type": "bold", "text": "Kotlin"}]},
        ],
    }
    messages, _ = load_messages(write("result.json", json.dumps(export, ensure_ascii=False)))
    assert len(messages) == 1
    assert messages[0].text == "Лекція про Kotlin"
    assert messages[0].channel == "@cs_faculty"


def test_pretty_printed_json_array(write):
    # Файл «в json» чаще всего приходит с отступами, а не одной строкой.
    payload = [{"text": "Подія", "date": "2026-04-02", "source_message_id": 7}]
    messages, _ = load_messages(write("m.json", json.dumps(payload, indent=2, ensure_ascii=False)))
    assert messages[0].message_id == 7


def test_bad_date_produces_warning(write):
    # Относительные даты разрешаются от этой даты — молча подменять её нельзя.
    messages, warnings = load_messages(
        write("m.jsonl", '{"text": "Завтра лекція", "source_message_id": 1}\n')
    )
    assert messages[0].date == clock_today()
    assert warnings and "date" in warnings[0]


def test_missing_text_is_reported_not_crash(write):
    messages, warnings = load_messages(
        write("m.jsonl", '{"date": "2026-04-02"}\n{"text": "ok", "date": "2026-04-02"}\n')
    )
    assert len(messages) == 1
    assert any("text" in w for w in warnings)


def test_empty_file(write):
    messages, warnings = load_messages(write("m.jsonl", "  \n"))
    assert messages == []
    assert warnings


def test_expect_annotation_is_carried(write):
    # _expect — ключ проверки в тестовом датасете. Пайплайн его игнорирует,
    # но проверка промта показывает его рядом с ответом модели.
    payload = [{"text": "Подія", "date": "2026-04-02", "source_message_id": 1,
                "_expect": "ПАСТКА: подія в минулому → events: []"}]
    messages, _ = load_messages(write("m.json", json.dumps(payload, ensure_ascii=False)))
    assert messages[0].note == "ПАСТКА: подія в минулому → events: []"


def test_missing_expect_gives_empty_note(write):
    payload = [{"text": "Подія", "date": "2026-04-02", "source_message_id": 1}]
    messages, _ = load_messages(write("m.json", json.dumps(payload, ensure_ascii=False)))
    assert messages[0].note == ""


def test_real_test_dataset_is_wellformed():
    # Датасет для проверки промта: 35 сообщений, каждое не короче 50 слов,
    # у всех есть ключ _expect. Три категории:
    #   ПАСТКА       — события быть не должно вовсе (events: [])
    #   ІНДИВІДУАЛЬНА — модель извлечёт, а фильтр охвата обязан отсеять
    #   остальные    — настоящие анонсы, должны сохраниться
    messages, warnings = load_messages("data/test_messages.json")
    assert warnings == []
    assert len(messages) == 35
    assert all(m.note for m in messages), "у каждого сообщения должен быть _expect"
    short = [(m.message_id, len(m.text.split())) for m in messages if len(m.text.split()) < 50]
    assert not short, f"слишком короткие сообщения: {short}"
    traps = [m for m in messages if m.note.startswith("ПАСТКА")]
    assert len(traps) == 10, f"ожидалось 10 ловушек, найдено {len(traps)}"
    individual = [m for m in messages if m.note.startswith("ІНДИВІДУАЛЬНА")]
    assert len(individual) == 3, f"ожидалось 3 индивидуальных, найдено {len(individual)}"
    assert len({m.message_id for m in messages}) == 35, "id должны быть уникальны"


def test_dataset_individual_consultations_are_filtered():
    # Сквозная проверка требования: помеченные ІНДИВІДУАЛЬНА отсекаются
    # фильтром охвата, а массовые консультации (234, 235) — нет.
    from app.models.schemas import ExtractedEvent
    from app.services.audience import is_individual

    messages, _ = load_messages("data/test_messages.json")
    by_id = {m.message_id: m for m in messages}

    for msg_id in (231, 232, 233):
        msg = by_id[msg_id]
        event = ExtractedEvent(event_title="Консультація")
        assert is_individual(event, msg.text), f"#{msg_id} должно отсеиваться"

    for msg_id in (234, 235):
        msg = by_id[msg_id]
        event = ExtractedEvent(event_title="Консультація")
        assert not is_individual(event, msg.text), f"#{msg_id} должно сохраняться"


def test_legacy_iter_sample_messages_unchanged():
    # Старый API остаётся построчным и не отдаёт предупреждений.
    messages = list(iter_sample_messages(JSONL.splitlines()))
    assert [m.message_id for m in messages] == [101, 102]
