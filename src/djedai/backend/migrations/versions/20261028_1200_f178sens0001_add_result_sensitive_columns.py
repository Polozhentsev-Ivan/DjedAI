"""Store output columns whose values inherited sensitivity from a source result.

Revision ID: f178sens0001
Revises: f169dash0001
"""

import sqlalchemy as sa
from alembic import op

revision = "f178sens0001"
down_revision = "f169dash0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("results", sa.Column("sensitive_columns", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("results", recreate="never") as batch:
        batch.drop_column("sensitive_columns")
