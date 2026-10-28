"""Store installation-wide model settings chosen in the interface (#209).

Revision ID: f209model0001
Revises: f169read0001
"""

import sqlalchemy as sa
from alembic import op

revision = "f209model0001"
down_revision = "f169read0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "model_settings",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("base_url", sa.Text(), nullable=True),
        sa.Column("model_name", sa.String(length=255), nullable=True),
        # Fernet ciphertext only; the plaintext key is never stored.
        sa.Column("api_key_ciphertext", sa.Text(), nullable=True),
        sa.Column(
            "updated_by",
            sa.String(length=64),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("model_settings")
