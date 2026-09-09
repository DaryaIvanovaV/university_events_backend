"""Таблица events — состояние схемы до перехода на Alembic

Эта ревизия описывает таблицу ровно такой, какой её создавал `create_all`
(включая колонку meeting_code и GIN-индекс по raw_json на PostgreSQL).

Если база уже работает и таблица events в ней есть, накатывать ревизию не
нужно — достаточно отметить её как применённую:

    alembic stamp 0001

На пустой базе `alembic upgrade head` выполнит её обычным порядком.

Revision ID: 0001
Revises:
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Тот же портируемый тип, что и в app/models/db.py: JSONB на PostgreSQL,
# generic JSON на SQLite.
JSON_TYPE = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "events",
        sa.Column("id", sa.Integer(), nullable=False),
        # --- Поля, извлечённые LLM ---
        sa.Column("event_title", sa.String(length=512), nullable=False),
        sa.Column("date", sa.Date(), nullable=True),
        sa.Column("time", sa.String(length=16), nullable=True),
        sa.Column("location", sa.String(length=256), nullable=True),
        sa.Column("organizer", sa.String(length=256), nullable=True),
        sa.Column("target_audience", sa.String(length=256), nullable=True),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("description", sa.String(length=2048), nullable=True),
        sa.Column("language", sa.String(length=8), nullable=True),
        sa.Column("link", sa.String(length=512), nullable=True),
        sa.Column("meeting_code", sa.String(length=128), nullable=True),
        # --- Модерация ---
        sa.Column("status", sa.String(length=16), nullable=False),
        # --- Источник и дедупликация ---
        sa.Column("source_channel", sa.String(length=128), nullable=True),
        sa.Column("source_message_id", sa.Integer(), nullable=True),
        sa.Column("raw_text", sa.String(length=8192), nullable=True),
        sa.Column("raw_json", JSON_TYPE, nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_channel", "source_message_id", name="uq_source_message"
        ),
    )
    op.create_index("ix_events_content_hash", "events", ["content_hash"])
    op.create_index("ix_events_status", "events", ["status"])
    op.create_index("ix_events_date", "events", ["date"])
    op.create_index("ix_events_type", "events", ["event_type"])

    # GIN по raw_json ускоряет запросы по содержимому (@>, ?, ?|). Только на
    # PostgreSQL: у SQLite такого индекса нет.
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_events_raw_json_gin "
            "ON events USING gin (raw_json)"
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS ix_events_raw_json_gin")
    op.drop_index("ix_events_type", table_name="events")
    op.drop_index("ix_events_date", table_name="events")
    op.drop_index("ix_events_status", table_name="events")
    op.drop_index("ix_events_content_hash", table_name="events")
    op.drop_table("events")
