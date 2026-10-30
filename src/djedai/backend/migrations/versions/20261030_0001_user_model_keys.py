"""Add operator-assigned model keys bound to application users."""

import sqlalchemy as sa
from alembic import op

revision = "userkeys0001"
down_revision = "f211merge0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_model_keys",
        sa.Column(
            "user_id",
            sa.String(64),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("api_key_ciphertext", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("user_model_keys")
