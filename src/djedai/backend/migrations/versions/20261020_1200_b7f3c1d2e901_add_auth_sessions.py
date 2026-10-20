"""Add persistent, revocable user sessions (AUTH-1).

Revision ID: b7f3c1d2e901
Revises: a5c2f58e6aea
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7f3c1d2e901"
down_revision: str | None = "a5c2f58e6aea"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # users и FK владельцев уже созданы в B5-2. Не пересоздаём их: миграция
    # сохраняет существующих владельцев, ресурсы и пользователей без пароля.
    op.create_table(
        "sessions",
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("token_hash"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.CheckConstraint("length(token_hash) = 64", name="ck_sessions_token_hash_length"),
        sa.CheckConstraint("expires_at > created_at", name="ck_sessions_positive_lifetime"),
    )
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])
    op.create_index("ix_sessions_expires_at", "sessions", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_sessions_expires_at", table_name="sessions")
    op.drop_index("ix_sessions_user_id", table_name="sessions")
    op.drop_table("sessions")
