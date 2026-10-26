"""Store the last source freshness check and re-profiling state of each dataset.

Revision ID: f169rev0001
Revises: f192merge0001
"""

import sqlalchemy as sa
from alembic import op

revision = "f169rev0001"
down_revision = "f192merge0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("datasets", sa.Column("freshness", sa.JSON(), nullable=True))
    op.add_column("datasets", sa.Column("refresh", sa.JSON(), nullable=True))


def downgrade() -> None:
    # Recreating "datasets" in SQLite would cascade-delete its chats and results;
    # ALTER TABLE ... DROP COLUMN (SQLite 3.35+) keeps the table and its rows.
    with op.batch_alter_table("datasets", recreate="never") as batch:
        batch.drop_column("refresh")
        batch.drop_column("freshness")
