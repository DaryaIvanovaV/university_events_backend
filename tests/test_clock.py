"""
Тесты единого источника времени (app/services/clock.py).

Смысл модуля прикладной: «сегодня» для фильтра upcoming должно считаться по
времени Киева, а не по времени процесса. В контейнере процесс живёт в UTC,
и ночью фид показывал бы студентам вчерашние события.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.config import settings
from app.services import clock


@pytest.fixture(autouse=True)
def clear_tz_cache():
    """Зона кэшируется через lru_cache — иначе подмена настройки не подействует."""
    clock.tz.cache_clear()
    yield
    clock.tz.cache_clear()


def set_tz(monkeypatch, name: str) -> None:
    monkeypatch.setattr(settings, "app_timezone", name, raising=False)
    clock.tz.cache_clear()


def test_today_matches_now_in_configured_zone(monkeypatch):
    set_tz(monkeypatch, "Europe/Kyiv")
    assert clock.today() == clock.now().date()
    assert clock.now().tzinfo is not None


def test_configured_zone_is_actually_applied(monkeypatch):
    # Не «сегодня совпадает», а «зона реально применяется»: в 00:30 по Киеву
    # в UTC ещё вчерашний день, и именно эта разница ломала фид.
    set_tz(monkeypatch, "Europe/Kyiv")
    assert clock.now().utcoffset() != dt.timedelta(0)


def test_unknown_timezone_falls_back_to_local(monkeypatch):
    # Битая настройка не должна ронять приложение при старте.
    set_tz(monkeypatch, "Mars/Olympus_Mons")
    assert clock.tz() is None
    assert isinstance(clock.today(), dt.date)


def test_empty_timezone_means_local(monkeypatch):
    set_tz(monkeypatch, "")
    assert clock.tz() is None
    assert isinstance(clock.now(), dt.datetime)


def test_utc_now_is_utc():
    # Служебные отметки очереди намеренно в UTC: на SQLite func.now() —
    # тоже UTC, и смешивать их с киевским временем нельзя.
    assert clock.utc_now().utcoffset() == dt.timedelta(0)
