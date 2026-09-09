"""
Тесты симуляции судьбы события: попадёт ли оно в БД и в фид.

Смысл: отчёт утверждает «это событие ляжет в базу». Если симуляция расходится
с настоящим пайплайном, отчёт врёт в самом важном месте. Поэтому проверяем,
что решения совпадают с app/services/pipeline.py и app/db/crud.py, а хэш
дедупликации — тот же самый, а не своя копия.
"""

import datetime as dt

from app.db.crud import compute_content_hash
from app.eval import pipeline_sim as sim
from app.models.schemas import ExtractedEvent

REF = dt.date(2026, 10, 1)


def event(title="Конференція AI", date=dt.date(2026, 10, 15)) -> ExtractedEvent:
    return ExtractedEvent(event_title=title, date=date)


def decide(simulator, ev, *, index=1, candidate=True, ref=REF):
    return simulator.decide(
        ev, index=index, prefilter_candidate=candidate, reference_date=ref
    )


def test_first_event_is_saved_and_reaches_feed():
    d = decide(sim.PipelineSimulator(), event())
    assert d.db == sim.SAVED
    assert d.feed == sim.FEED
    assert d.reaches_db and d.reaches_feed


def test_prefilter_blocks_before_llm():
    # В проде пре-фильтр отсекает сообщение и LLM даже не вызывается.
    d = decide(sim.PipelineSimulator(), event(), candidate=False)
    assert d.db == sim.FILTERED
    assert d.feed == ""
    assert not d.reaches_db


def test_duplicate_is_detected_across_messages():
    s = sim.PipelineSimulator()
    first = decide(s, event(), index=3)
    second = decide(s, event(), index=7)
    assert first.db == sim.SAVED
    assert second.db == sim.DUPLICATE
    assert "#3" in second.note, "нужно указать, где анонс встретился впервые"


def test_same_title_different_date_is_not_duplicate():
    s = sim.PipelineSimulator()
    decide(s, event(date=dt.date(2026, 10, 15)))
    d = decide(s, event(date=dt.date(2026, 11, 20)))
    assert d.db == sim.SAVED


def test_content_hash_matches_production():
    # Хэш обязан совпадать с crud, иначе симуляция дедупликации бессмысленна.
    ev = event()
    d = decide(sim.PipelineSimulator(), ev)
    assert d.content_hash == compute_content_hash(ev.event_title, ev.date)


def test_event_without_date_never_reaches_feed():
    # GET /events?upcoming=true фильтрует date >= ..., NULL туда не проходит.
    d = decide(sim.PipelineSimulator(), event(date=None))
    assert d.db == sim.SAVED
    assert d.feed == sim.FEED_NO_DATE
    assert not d.reaches_feed


def test_past_event_never_reaches_db():
    # Отчёт о прошедшем не занимает очередь модерации: администратор всё
    # равно отклонит, а в календаре оно не покажется никогда.
    d = decide(sim.PipelineSimulator(), event(date=dt.date(2026, 9, 1)))
    assert d.db == sim.STALE
    assert d.feed == ""
    assert not d.reaches_db


def test_past_event_does_not_occupy_dedup_slot():
    # Устаревшее отсекается ДО регистрации content_hash, поэтому нормальный
    # анонс того же события позже сохранится.
    s = sim.PipelineSimulator()
    decide(s, event(date=dt.date(2026, 9, 1)), index=1)
    later = decide(s, event(date=dt.date(2026, 9, 1)), index=2,
                   ref=dt.date(2026, 8, 20))
    assert later.db == sim.SAVED


def test_feed_counted_from_message_date_not_today():
    # Дата события в прошлом относительно СЕГОДНЯ, но в будущем относительно
    # даты публикации — на момент выхода объявления оно было актуальным.
    d = decide(
        sim.PipelineSimulator(),
        event(date=dt.date(2026, 6, 18)),
        ref=dt.date(2026, 6, 1),
    )
    assert d.feed == sim.FEED


def test_summarize_destiny_counts_run():
    records = [
        {"events_destiny": [
            {"db": sim.SAVED, "feed": sim.FEED},
            {"db": sim.SAVED, "feed": sim.FEED_NO_DATE},
        ]},
        {"events_destiny": [{"db": sim.DUPLICATE, "feed": ""}]},
        {"events_destiny": [{"db": sim.FILTERED, "feed": ""}]},
        {"events_destiny": [{"db": sim.STALE, "feed": ""}]},
        {},  # старый дамп без поля — не должен ронять сводку
    ]
    counts = sim.summarize_destiny(records)
    assert counts[sim.SAVED] == 2
    assert counts[sim.DUPLICATE] == 1
    assert counts[sim.FILTERED] == 1
    assert counts[sim.STALE] == 1
    assert counts[sim.FEED] == 1


def test_destiny_serializable():
    d = decide(sim.PipelineSimulator(), event())
    as_dict = d.to_dict()
    assert set(as_dict) == {"db", "feed", "content_hash", "note"}
