"""Persist the consistency of the last dashboard refresh (#97, #169).

Revision ID: f169dash0001
Revises: f169read0001
"""

import sqlalchemy as sa
from alembic import op

revision = "f169dash0001"
down_revision = "f169read0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "dashboards",
        sa.Column(
            "consistency",
            sa.String(length=16),
            nullable=False,
            server_default="independent",
        ),
    )


def downgrade() -> None:
    with op.batch_alter_table("dashboards", recreate="never") as batch:
        batch.drop_column("consistency")
