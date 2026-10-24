"""Persist chat summary and the current focus results for follow-up questions.

Revision ID: e5b1c7a2d304
Revises: d8e103060001
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e5b1c7a2d304"
down_revision: str | None = "d8e103060001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("chats", sa.Column("focus_result_ids", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("chats") as batch:
        batch.drop_column("focus_result_ids")
