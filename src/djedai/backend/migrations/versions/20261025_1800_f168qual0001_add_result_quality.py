"""Store the data quality report of each tool result.

Revision ID: f168qual0001
Revises: f166metr0001
"""

import sqlalchemy as sa
from alembic import op

revision = "f168qual0001"
down_revision = "f166metr0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("results", sa.Column("quality", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("results") as batch:
        batch.drop_column("quality")
