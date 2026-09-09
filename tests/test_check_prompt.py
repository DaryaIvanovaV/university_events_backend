"""
Тесты сборки записи прогона и рендера HTML-отчёта — без Ollama.

Смысл: отчёт строится ПОСЛЕ длинного прогона модели, и падение рендера здесь
означало бы потерю всей работы. Поэтому и сборка записи, и генерация HTML
проверяются на заглушке chat_fn.
"""

import datetime as dt
import json

from app.eval import grounding, pipeline_sim, report
from app.ingestion.tg_export import ExportMessage
from app.llm.extractor import EventExtractor
from scripts.check_prompt import RecordingChat, describe, process_message

SRC = (
    "Увага! Завтра о 14:30 в 405 аудиторії відбудеться гостьова лекція "
    "від ІТ-компанії про розробку на Kotlin. Явка для 4 курсу обов'язкова!"
)
HONEST = (
    '{"events":[{"event_title":"Гостьова лекція: розробка на Kotlin",'
    '"date":"2026-04-03","time":"14:30","location":"Ауд. 405",'
    '"organizer":"ІТ-компанія","target_audience":"4 курс",'
    '"event_type":"lecture","description":null,"language":"uk","link":null}]}'
)
HALLUCINATED = (
    '{"events":[{"event_title":"Гостьова лекція: розробка на Kotlin",'
    '"date":"2026-05-20","time":"09:00","location":"Ауд. 512",'
    '"organizer":"Кафедра філософії","target_audience":"4 курс",'
    '"event_type":"lecture","description":null,"language":"uk",'
    '"link":"https://example.com/register"}]}'
)

MESSAGE = ExportMessage(
    message_id=101, date=dt.date(2026, 4, 2), text=SRC, channel="@demo"
)


def make_record(response: str, message: ExportMessage = MESSAGE, **kwargs) -> dict:
    recorder = RecordingChat(lambda messages, schema: response)
    extractor = EventExtractor(recorder, max_retries=0)
    kwargs.setdefault("respect_prefilter", False)
    return process_message(extractor, recorder, message, 1, **kwargs)


def test_honest_extraction_has_no_invented_fields():
    record = make_record(HONEST)
    assert record["valid_json"] is True
    assert record["attempts"] == 1
    assert record["error"] is None
    statuses = [c["status"] for group in record["event_checks"] for c in group]
    assert grounding.INVENTED not in statuses
    assert grounding.INVALID not in statuses


def test_hallucinated_extraction_is_flagged():
    record = make_record(HALLUCINATED)
    flagged = {
        c["field"]
        for group in record["event_checks"]
        for c in group
        if c["status"] in (grounding.INVENTED, grounding.INVALID)
    }
    # Выдуманы аудитория, организатор, дата, время и ссылка.
    assert {"location", "organizer", "date", "time", "link"} <= flagged
    assert record["worst"] == grounding.INVENTED


def test_raw_response_is_captured():
    # Сырой ответ модели нужен, чтобы понять ПОЧЕМУ вышло не то.
    record = make_record(HONEST)
    assert record["raw_responses"] == [HONEST]


def test_retry_is_counted():
    responses = iter(['{"events":[{"event_title":"X","date":"НЕ-ДАТА"}]}', HONEST])

    def chat_fn(messages, schema):
        return next(responses)

    recorder = RecordingChat(chat_fn)
    record = process_message(
        EventExtractor(recorder, max_retries=2), recorder, MESSAGE, 1,
        respect_prefilter=False,
    )
    assert record["attempts"] == 2
    assert record["valid_json"] is True


def test_extraction_error_recorded_not_raised():
    record = make_record('{"events":[{"event_title":"X","date":"НЕ-ДАТА"}]}')
    assert record["valid_json"] is False
    assert record["error"]
    assert record["worst"] == grounding.INVALID


def test_respect_prefilter_skips_llm():
    message = ExportMessage(
        message_id=9, date=dt.date(2026, 4, 2),
        text="Дякуємо всім! Гарних вихідних.", channel="@demo",
    )
    calls = []

    def chat_fn(messages, schema):
        calls.append(1)
        return HONEST

    recorder = RecordingChat(chat_fn)
    record = process_message(
        EventExtractor(recorder), recorder, message, 1, respect_prefilter=True
    )
    assert calls == []
    assert record["events"] == []


def test_describe_is_printable():
    assert "соб." in describe(make_record(HONEST))


def test_report_renders_valid_html():
    records = [make_record(HONEST), make_record(HALLUCINATED)]
    records[1]["index"] = 2
    meta = {
        "run_id": "test",
        "model": "gemma3:4b",
        "temperature": 0.0,
        "max_retries": 2,
        "base_url": "http://localhost:11434",
        "dataset": "test.jsonl",
        "started_at": "2026-08-19 12:00:00",
        "duration_s": 12.3,
        "warmup_s": 5.0,
        "warnings": ["тестовое предупреждение"],
        "prompt": {"system": "SYS", "fewshot": [{"role": "user", "content": "FS"}],
                   "user_example": "USER"},
    }
    html = report.render(meta, records)
    assert html.startswith("<!doctype html>")
    assert html.rstrip().endswith("</html>")
    # Подсветка подтверждающих фрагментов — главная функция отчёта.
    assert "<mark" in html
    # Оригинал и выдуманное значение оба присутствуют.
    assert "405 аудиторії" in html
    assert "Ауд. 512" in html
    assert "тестовое предупреждение" in html


def test_report_escapes_html_in_message():
    message = ExportMessage(
        message_id=1, date=dt.date(2026, 4, 2),
        text="Лекція <script>alert(1)</script> завтра о 14:30 в 405 ауд.",
        channel="@demo",
    )
    record = make_record(HONEST, message)
    meta = {
        "run_id": "t", "model": "m", "temperature": 0.0, "max_retries": 0,
        "base_url": "u", "dataset": "d", "started_at": "s", "duration_s": 1.0,
        "warmup_s": None, "warnings": [],
        "prompt": {"system": "", "fewshot": [], "user_example": ""},
    }
    html = report.render(meta, [record])
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_expect_reaches_the_record_and_report():
    message = ExportMessage(
        message_id=1, date=dt.date(2026, 4, 2), text=SRC, channel="@demo",
        note="ПАСТКА: подія в минулому → events: []",
    )
    record = make_record(HONEST, message)
    assert record["expect"] == "ПАСТКА: подія в минулому → events: []"

    meta = {
        "run_id": "t", "model": "m", "temperature": 0.0, "max_retries": 0,
        "base_url": "u", "dataset": "d", "started_at": "s", "duration_s": 1.0,
        "warmup_s": None, "warnings": [],
        "prompt": {"system": "", "fewshot": [], "user_example": ""},
    }
    html = report.render(meta, [record])
    assert "Очікується:" in html
    assert "подія в минулому" in html


def test_report_has_verdict_import_control():
    # Вердикти лежать у localStorage одного браузера: без імпорту розмітку
    # не перенести на іншу машину і не відновити після очищення сховища.
    meta = {
        "run_id": "t", "model": "m", "temperature": 0.0, "max_retries": 0,
        "base_url": "u", "dataset": "d", "started_at": "s", "duration_s": 1.0,
        "warmup_s": None, "warnings": [],
        "prompt": {"system": "", "fewshot": [], "user_example": ""},
    }
    html = report.render(meta, [make_record(HONEST)])
    assert 'id="import"' in html
    assert 'id="import-file"' in html and 'type="file"' in html
    # Кнопка без обробника — мертва: перевіряємо і скрипт.
    assert "applyVerdicts" in html
    assert 'getElementById("import")' in html


def test_report_renders_records_without_expect_key():
    # Старые дампы прогонов не содержат поля expect — рендер не должен падать.
    record = make_record(HONEST)
    del record["expect"]
    meta = {
        "run_id": "t", "model": "m", "temperature": 0.0, "max_retries": 0,
        "base_url": "u", "dataset": "d", "started_at": "s", "duration_s": 1.0,
        "warmup_s": None, "warnings": [],
        "prompt": {"system": "", "fewshot": [], "user_example": ""},
    }
    html = report.render(meta, [record])
    assert "Очікується:" not in html
    assert html.rstrip().endswith("</html>")


def test_destiny_is_recorded_parallel_to_events():
    record = make_record(HONEST)
    assert len(record["events_destiny"]) == len(record["events"]) == 1
    assert record["events_destiny"][0]["db"] == pipeline_sim.SAVED


def test_destiny_marks_duplicate_across_messages():
    # Один симулятор на прогон: тот же анонс из второго сообщения отсекается.
    simulator = pipeline_sim.PipelineSimulator()
    made = []
    for i in (1, 2):
        recorder = RecordingChat(lambda m, s: HONEST)
        made.append(process_message(
            EventExtractor(recorder, max_retries=0), recorder, MESSAGE, i,
            respect_prefilter=False, simulator=simulator,
        ))
    assert made[0]["events_destiny"][0]["db"] == pipeline_sim.SAVED
    assert made[1]["events_destiny"][0]["db"] == pipeline_sim.DUPLICATE


def test_report_shows_destiny_and_override_buttons():
    record = make_record(HALLUCINATED)
    meta = {
        "run_id": "t", "model": "m", "temperature": 0.0, "max_retries": 0,
        "base_url": "u", "dataset": "d", "started_at": "s", "duration_s": 1.0,
        "warmup_s": None, "warnings": [],
        "prompt": {"system": "", "fewshot": [], "user_example": ""},
    }
    html = report.render(meta, [record])
    # Явно сказано, что запись пойдёт в БД и дальше на модерацию.
    assert "БД (pending) → на модерацію" in html
    # Четыре ручные пометки поля: правильная / выдумана-верно /
    # выдумана-неверно / пропущена.
    for value, _sym, _hint in report.MARK_BUTTONS:
        assert f'data-v="{value}"' in html
    assert 'data-auto="invented"' in html
    assert 'data-db="saved"' in html
    # Оценка события: отправлять дальше или нет.
    assert 'class="sendbtn" data-v="yes"' in html
    assert 'class="sendbtn" data-v="no"' in html
    assert "Відправляти далі?" in html


def test_mark_taxonomy_is_complete():
    # Четыре категории из требования: правильная, выдуманная верная,
    # выдуманная неверная, пропущенная.
    values = {value for value, _s, _h in report.MARK_BUTTONS}
    assert values == {"correct", "invented_ok", "invented_bad", "missed"}
    assert all(v in report.MARK_LABEL for v in values)


def test_report_renders_records_without_destiny():
    # Старые дампы прогонов не содержат events_destiny — рендер не должен падать.
    record = make_record(HONEST)
    del record["events_destiny"]
    meta = {
        "run_id": "t", "model": "m", "temperature": 0.0, "max_retries": 0,
        "base_url": "u", "dataset": "d", "started_at": "s", "duration_s": 1.0,
        "warmup_s": None, "warnings": [],
        "prompt": {"system": "", "fewshot": [], "user_example": ""},
    }
    html = report.render(meta, [record])
    assert html.rstrip().endswith("</html>")


def test_stats_over_records():
    stats = report.compute_stats([make_record(HONEST), make_record(HALLUCINATED)])
    assert stats["total"] == 2
    assert stats["events"] == 2
    assert stats["first_try"] == 2
    assert stats["invented"] >= 5


def test_record_is_json_serializable():
    # Машинный дамп прогона пишется в JSON — все значения должны сериализоваться.
    json.dumps(make_record(HALLUCINATED), ensure_ascii=False)
