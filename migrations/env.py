"""
Окружение Alembic.

URL базы берётся из настроек приложения (`DATABASE_URL`), а не из alembic.ini —
чтобы пароль не лежал в репозитории и чтобы миграции ходили ровно туда же,
куда и backend.

`render_as_batch=True` нужен для SQLite: он не умеет ALTER COLUMN / DROP
COLUMN, и Alembic обходит это пересозданием таблицы с копированием данных.
На PostgreSQL флаг игнорируется.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context

from app.config import settings
from app.db.database import make_engine
from app.models.db import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Генерация SQL без подключения (alembic upgrade --sql)."""
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Обычный режим: подключаемся и накатываем."""
    connectable = make_engine()
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
