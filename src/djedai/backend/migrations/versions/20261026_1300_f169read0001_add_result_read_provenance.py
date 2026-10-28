"""Store what and when each tool result read; old results get an explicit unknown.

Revision ID: f169read0001
Revises: f169rev0001
"""

import sqlalchemy as sa
from alembic import op

revision = "f169read0001"
down_revision = "f169rev0001"
branch_labels = None
depends_on = None

# ReadProvenance.unknown() at the time of this migration; frozen so that it never
# depends on application code that may change later.
UNKNOWN = {
    "revision_known": False,
    "dataset_id": None,
    "catalog_revision": None,
    "schema_fingerprint": None,
    "source_fingerprint": None,
    "transform_version": None,
    "read_mode": "unknown",
    "consistency": "unknown",
    "snapshot_id": None,
    "started_at": None,
    "finished_at": None,
    "freshness": {
        "state": "unknown",
        "profiled_at": None,
        "checked_at": None,
        "changes": [],
        "reason": "Версия каталога и время чтения этого результата не сохранены: версия неизвестна",
    },
}


def upgrade() -> None:
    op.add_column("results", sa.Column("read", sa.JSON(), nullable=True))
    results = sa.table("results", sa.column("read", sa.JSON()))
    # «Версия неизвестна» is stated explicitly, not left as NULL meaning «not applicable».
    op.execute(results.update().values(read=UNKNOWN))


def downgrade() -> None:
    with op.batch_alter_table("results", recreate="never") as batch:
        batch.drop_column("read")
