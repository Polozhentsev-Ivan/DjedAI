"""Persist CSV source files, analytical database location and processing progress.

Revision ID: c4d9f51a6240
Revises: b7f3c1d2e901
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4d9f51a6240"
down_revision: str | None = "b7f3c1d2e901"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("datasets", sa.Column("duckdb_path", sa.Text(), nullable=True))
    op.add_column("datasets", sa.Column("raw_files", sa.JSON(), nullable=True))
    op.add_column("datasets", sa.Column("progress", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("datasets") as batch:
        batch.drop_column("progress")
        batch.drop_column("raw_files")
        batch.drop_column("duckdb_path")
