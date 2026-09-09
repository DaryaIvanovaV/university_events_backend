"""Очередь ingest_jobs + составной индекс под запрос фида

Добавляет:
  * таблицу `ingest_jobs` — очередь сообщений, ожидающих обработки LLM
    (см. app/worker.py);
  * индекс ix_events_status_date: основной запрос фида это
    WHERE status=? AND date>=? ORDER BY date, и составной индекс покрывает
    его лучше, чем два отдельных.

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON_TYPE = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "ingest_jobs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        # --- Полезная нагрузка ---
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("source_channel", sa.String(length=128), nullable=True),
        sa.Column("source_message_id", sa.Integer(), nullable=True),
        sa.Column("reference_date", sa.Date(), nullable=True),
        # --- Исполнение ---
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("locked_by", sa.String(length=64), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        # --- Результат ---
        sa.Column("event_ids", JSON_TYPE, nullable=False),
        sa.Column("skipped", JSON_TYPE, nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ingest_jobs_status", "ingest_jobs", ["status"])
    # Запрос захвата воркером: status + available_at, порядок по id.
    op.create_index(
        "ix_jobs_claim", "ingest_jobs", ["status", "available_at", "id"]
    )
    # Поиск уже стоящей в очереди задачи по тому же посту (антидубль).
    op.create_index(
        "ix_jobs_source", "ingest_jobs", ["source_channel", "source_message_id"]
    )

    op.create_index("ix_events_status_date", "events", ["status", "date"])


def downgrade() -> None:
    op.drop_index("ix_events_status_date", table_name="events")
    op.drop_index("ix_jobs_source", table_name="ingest_jobs")
    op.drop_index("ix_jobs_claim", table_name="ingest_jobs")
    op.drop_index("ix_ingest_jobs_status", table_name="ingest_jobs")
    op.drop_table("ingest_jobs")
