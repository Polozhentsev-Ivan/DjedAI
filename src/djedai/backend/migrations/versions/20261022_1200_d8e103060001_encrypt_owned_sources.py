"""Store authenticated encrypted credentials for owned external datasets.

Revision ID: d8e103060001
Revises: c4d9f51a6240
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d8e103060001"
down_revision: str | None = "c4d9f51a6240"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("datasets", sa.Column("source_secret", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("datasets") as batch:
        batch.drop_column("source_secret")
