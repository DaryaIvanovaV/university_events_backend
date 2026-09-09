"""
Тип raw_json: JSONB на PostgreSQL, generic JSON на SQLite.

Проверяем портируемость (with_variant) без реального PostgreSQL — через
компиляцию типа под конкретный диалект и round-trip на in-memory SQLite.
"""

from sqlalchemy import create_engine, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import Session

from app.models.db import Base, Event


def test_raw_json_compiles_to_jsonb_on_postgres():
    ddl = Event.__table__.c.raw_json.type.compile(dialect=postgresql.dialect())
    assert ddl == "JSONB"


def test_raw_json_compiles_to_json_on_sqlite():
    ddl = Event.__table__.c.raw_json.type.compile(dialect=sqlite.dialect())
    assert ddl == "JSON"


def test_raw_json_roundtrip_on_sqlite():
    # create_all на SQLite не должен спотыкаться о GIN-листенер (execute_if
    # пропускает его вне postgresql), а dict round-trip'ится через generic JSON.
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    payload = {"event_type": "seminar", "назва": "тест", "n": [1, 2, 3]}
    with Session(engine) as s:
        s.add(Event(event_title="X", content_hash="h", raw_json=payload))
        s.commit()
        got = s.scalar(select(Event))
    assert got.raw_json == payload
