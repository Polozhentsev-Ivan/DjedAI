"""Persist the outcome of each accepted chat question.

Revision ID: e161chat0001
Revises: d8e103060001
"""

import sqlalchemy as sa
from alembic import op

revision = "e161chat0001"
down_revision = "d8e103060001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("turn", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("messages") as batch:
        batch.drop_column("turn")
