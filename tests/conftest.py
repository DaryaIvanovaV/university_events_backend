"""
Общие фикстуры тестов.

БД — SQLite в памяти на StaticPool: обычный in-memory даёт КАЖДОМУ соединению
свою пустую базу, и данные, записанные фикстурой, не увидел бы HTTP-запрос
через TestClient. StaticPool держит одно соединение на весь тест.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.db import Base


@pytest.fixture()
def engine():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def db_session(engine):
    Session = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    session = Session()
    yield session
    session.close()


@pytest.fixture()
def client(engine, db_session, monkeypatch):
    """
    TestClient с подменённой сессией БД.

    Три подмены обязательны:
      * init_db — иначе lifespan создал бы таблицы в реальном ./events.db,
        то есть тесты писали бы в рабочую базу разработчика;
      * main.engine — его напрямую использует проба /health/ready, минуя
        зависимость get_session;
      * ключи API — их могли задать в локальном .env, и тогда все запросы
        в тестах отвечали бы 401. Проверка самой аутентификации живёт в
        tests/test_api_security.py и включает ключи явно.
    """
    from fastapi.testclient import TestClient

    from app import main
    from app.config import settings
    from app.db.database import get_session

    monkeypatch.setattr(main, "init_db", lambda: None)
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(settings, "ingest_api_key", "", raising=False)
    monkeypatch.setattr(settings, "admin_api_key", "", raising=False)

    main.app.dependency_overrides[get_session] = lambda: db_session
    with TestClient(main.app) as test_client:
        yield test_client
    main.app.dependency_overrides.clear()
