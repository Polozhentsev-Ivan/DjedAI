"""Store versioned business metric definitions and their provenance in results.

Revision ID: f166metr0001
Revises: e161chat0001
"""

import sqlalchemy as sa
from alembic import op

revision = "f166metr0001"
down_revision = "e161chat0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "metric_definitions",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column(
            "owner_id",
            sa.String(length=64),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "dataset_id",
            sa.String(length=64),
            sa.ForeignKey("datasets.dataset_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("metric_id", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("definition", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("dataset_id", "metric_id", "version", name="uq_metric_version"),
    )
    op.create_index("ix_metric_definitions_owner_id", "metric_definitions", ["owner_id"])
    op.create_index("ix_metric_definitions_dataset_id", "metric_definitions", ["dataset_id"])
    op.create_table(
        "metric_proposals",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column(
            "owner_id",
            sa.String(length=64),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "dataset_id",
            sa.String(length=64),
            sa.ForeignKey("datasets.dataset_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "message_id",
            sa.String(length=64),
            sa.ForeignKey("messages.message_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("option", sa.Text(), nullable=False),
        sa.Column("definition", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_metric_proposals_owner_id", "metric_proposals", ["owner_id"])
    op.create_index("ix_metric_proposals_dataset_id", "metric_proposals", ["dataset_id"])
    op.create_index("ix_metric_proposals_message_id", "metric_proposals", ["message_id"])
    op.add_column("results", sa.Column("metric", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("results") as batch:
        batch.drop_column("metric")
    op.drop_table("metric_proposals")
    op.drop_table("metric_definitions")
