"""
Подключение к БД: engine, фабрика сессий, инициализация схемы.

Настройки пула вынесены в конфиг и различаются по диалекту:

* `pool_pre_ping=True` включаем всегда. В Docker это не «на всякий случай»:
  контейнер `db` перезапускается — все соединения в пуле становятся мёртвыми,
  и без пинга каждый следующий запрос падал бы 500-й («server closed the
  connection unexpectedly»), пока пул не обновится сам.
* `pool_size` / `max_overflow` передаём ТОЛЬКО для серверных БД. У SQLite
  in-memory пул — SingletonThreadPool, он этих аргументов не принимает и
  падает с TypeError, а на нём держатся все тесты.
* `pool_recycle` закрывает соединения раньше, чем это сделает сервер или
  NAT в docker-сети.
"""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.models.db import Base


def _engine_kwargs(url: str) -> dict:
    """Аргументы create_engine, зависящие от диалекта."""
    if url.startswith("sqlite"):
        return {
            # Нужен только для SQLite при работе из нескольких потоков (uvicorn).
            "connect_args": {"check_same_thread": False},
            "pool_pre_ping": True,
        }
    return {
        "pool_pre_ping": True,
        "pool_size": settings.db_pool_size,
        "max_overflow": settings.db_max_overflow,
        "pool_recycle": settings.db_pool_recycle_s,
    }


def make_engine(url: str | None = None) -> Engine:
    """Собирает engine по URL (по умолчанию — из настроек)."""
    url = url or settings.database_url
    return create_engine(url, future=True, echo=settings.db_echo, **_engine_kwargs(url))


engine = make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db() -> None:
    """
    Создать таблицы, если их ещё нет.

    Рабочий путь накатки схемы — Alembic (`alembic upgrade head`), он умеет
    ALTER для уже существующих таблиц, а `create_all` — нет. Эта функция
    осталась для тестов и быстрого локального старта на пустой SQLite.
    """
    Base.metadata.create_all(bind=engine)


def get_session() -> Iterator[Session]:
    """
    FastAPI-зависимость: выдаёт сессию и гарантированно закрывает её.

    Явный rollback на исключении: без него незакоммиченная транзакция едет
    в пул вместе с соединением, и следующий запрос может получить чужое
    «грязное» состояние.
    """
    session = SessionLocal()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
