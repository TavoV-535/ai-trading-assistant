"""create event_log table

Revision ID: 0001
Revises:
Create Date: 2026-07-20
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "event_log",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("source", sa.String(length=100), nullable=True),
        sa.Column("correlation_id", sa.Uuid(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_event_log_event_id", "event_log", ["event_id"], unique=True)
    op.create_index("ix_event_log_event_type", "event_log", ["event_type"])
    op.create_index("ix_event_log_correlation_id", "event_log", ["correlation_id"])
    op.create_index("ix_event_log_created_at", "event_log", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_event_log_created_at", table_name="event_log")
    op.drop_index("ix_event_log_correlation_id", table_name="event_log")
    op.drop_index("ix_event_log_event_type", table_name="event_log")
    op.drop_index("ix_event_log_event_id", table_name="event_log")
    op.drop_table("event_log")
