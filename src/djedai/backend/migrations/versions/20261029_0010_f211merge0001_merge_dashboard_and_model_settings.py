"""Merge dashboard metadata and model settings without rewriting deployed revisions.

Revision ID: f211merge0001
Revises: f178sens0001, f209model0001
"""

revision = "f211merge0001"
down_revision = ("f178sens0001", "f209model0001")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
