"""Ревизия каталога и свежесть источника (B1-11, #169)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from djedai.contracts import CatalogRevision, DatasetCatalog, SchemaChange, SourceFreshness

EXAMPLES = Path(__file__).parents[3] / "src" / "djedai" / "contracts" / "examples"
FULL = json.loads((EXAMPLES / "dataset_catalog.json").read_text(encoding="utf-8"))
MINIMAL = json.loads((EXAMPLES / "dataset_catalog_minimal.json").read_text(encoding="utf-8"))
PROFILED = "2026-10-21T09:30:00Z"
ADDED = SchemaChange(kind="added_column", table="public.orders", column="note", after="VARCHAR")
DROPPED = SchemaChange(kind="dropped_column", table="public.orders", column="amount")


def test_catalog_before_revisioning_still_validates():
    assert "revision" not in MINIMAL
    assert DatasetCatalog.model_validate(MINIMAL).revision is None


def test_revision_round_trips_in_catalog():
    catalog = DatasetCatalog.model_validate(FULL)
    assert catalog.revision == CatalogRevision.model_validate(FULL["revision"])
    assert catalog.revision.number == 2
    assert DatasetCatalog.model_validate_json(catalog.model_dump_json()) == catalog


@pytest.mark.parametrize(
    "change",
    [{"number": 0}, {"schema_fingerprint": "abc"}, {"profiled_at": "2026-10-21T09:30:00"}],
    ids=["revision 0", "short fingerprint", "naive time"],
)
def test_revision_rejects_invalid_values(change):
    with pytest.raises(ValidationError):
        CatalogRevision.model_validate({**FULL["revision"], **change})


def test_changes_are_classified_and_described():
    assert ADDED.compatible and not DROPPED.compatible
    assert ADDED.describe() == "добавлена колонка public.orders.note"
    changed = SchemaChange(
        kind="type_changed", table="t", column="a", before="INTEGER", after="BIGINT"
    )
    assert changed.describe() == "тип t.a изменился: INTEGER → BIGINT"


def test_states_require_their_explanation():
    assert SourceFreshness(state="fresh", profiled_at=PROFILED).changes == []
    assert SourceFreshness(state="drifted", profiled_at=PROFILED, changes=[ADDED])
    assert SourceFreshness(state="incompatible", profiled_at=PROFILED, changes=[ADDED, DROPPED])
    assert SourceFreshness(state="unknown", reason="каталог собран до версионирования")


@pytest.mark.parametrize(
    "data",
    [
        {"state": "drifted", "profiled_at": PROFILED},
        {"state": "drifted", "profiled_at": PROFILED, "changes": [DROPPED]},
        {"state": "incompatible", "profiled_at": PROFILED, "changes": [ADDED]},
        {"state": "fresh", "profiled_at": PROFILED, "changes": [ADDED]},
        {"state": "aged", "profiled_at": PROFILED},
        {"state": "unknown"},
        {"state": "fresh"},
    ],
    ids=[
        "drift без изменений",
        "drift с несовместимым",
        "incompatible без несовместимого",
        "fresh с изменениями",
        "aged без причины",
        "unknown без причины",
        "fresh без ревизии",
    ],
)
def test_inconsistent_states_are_rejected(data):
    with pytest.raises(ValidationError):
        SourceFreshness.model_validate(data)
