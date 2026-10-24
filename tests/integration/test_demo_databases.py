"""#100: наполненные демо-БД совпадают с контрольными значениями.

Нужны демо-БД профиля ``demo`` (``infra/README.md``) после ``scripts/seed_demo_*.py`` и DSN/URL
ролей ``djedai_reader``: ``DJEDAI_TEST_PG_DSN`` для PostgreSQL и ``DJEDAI_TEST_CH_URL``
для ClickHouse. Без переменной проверка своей БД пропускается.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import verify_demo_db  # noqa: E402

pytestmark = pytest.mark.integration
TARGETS = {
    "postgres": ("DJEDAI_TEST_PG_DSN", verify_demo_db.PostgresBackend),
    "clickhouse": ("DJEDAI_TEST_CH_URL", verify_demo_db.ClickHouseBackend),
}


@pytest.mark.parametrize("name", TARGETS)
def test_demo_database_matches_expectations(name):
    variable, factory = TARGETS[name]
    target = os.environ.get(variable)
    if not target:
        pytest.skip(f"{variable} не задан (infra/README.md, демо-БД)")
    spec = json.loads(verify_demo_db.EXPECTATIONS.read_text(encoding="utf-8"))
    backend = factory(target)
    try:
        outcomes = verify_demo_db.verify(backend, spec)
    finally:
        backend.close()
    assert [(o.check, o.actual, o.error) for o in outcomes if not o.ok] == []
