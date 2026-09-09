"""Колонка events.updated_at для инкрементальной синхронизации

Мобильному приложению нужно понимать, что изменилось с прошлой загрузки.
По `created_at` это не определить: правка модератора (перенос даты, смена
аудитории) не меняет момент создания, и клиент продолжал бы показывать
устаревшую карточку.

Существующим строкам проставляется now() — при первой синхронизации клиент
получит их все, что и требуется: у него ещё ничего нет.

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "events",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_events_updated_at", "events", ["updated_at"])


def downgrade() -> None:
    op.drop_index("ix_events_updated_at", table_name="events")
    op.drop_column("events", "updated_at")
