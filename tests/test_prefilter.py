"""Тесты пре-фильтра."""

from app.services.prefilter import prefilter


def test_ukrainian_announcement_passes():
    text = "Завтра о 14:30 в 405 аудиторії відбудеться гостьова лекція про Kotlin"
    res = prefilter(text)
    assert res.is_candidate
    assert res.score >= 2


def test_russian_announcement_passes():
    text = "Напоминаю: дедлайн подачи тезисов на конференцию — 20 мая"
    res = prefilter(text)
    assert res.is_candidate


def test_chatter_is_filtered_out():
    text = "Привіт, хтось знає коли буде готова їдальня? дякую"
    res = prefilter(text)
    assert not res.is_candidate


def test_empty_message():
    assert not prefilter("").is_candidate
    assert not prefilter("   ").is_candidate


def test_time_pattern_alone_contributes():
    res = prefilter("зустрічаємось о 18:00")
    assert "<time>" in res.matched
