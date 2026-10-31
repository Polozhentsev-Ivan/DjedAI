"""Persist the deterministic numeric verification report of each assistant answer.

Revision ID: f167verify01
Revises: f211merge0001
"""

import sqlalchemy as sa
from alembic import op

revision = "f167verify01"
down_revision = "f211merge0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("verification", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("messages") as batch:
        batch.drop_column("verification")
