"""Persist chat summary and the current focus results for follow-up questions.

Revision ID: e5b1c7a2d304
Revises: e161chat0001
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e5b1c7a2d304"
down_revision: str | None = "e161chat0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("chats", sa.Column("focus_result_ids", sa.JSON(), nullable=True))


def downgrade() -> None:
    # A native DROP COLUMN avoids recreating the chats table: on SQLite a batch
    # rebuild would cascade-delete messages that reference the old table.
    op.drop_column("chats", "focus_result_ids")
