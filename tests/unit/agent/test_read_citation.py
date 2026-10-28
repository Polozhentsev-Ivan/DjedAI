"""Источник, ревизия и время чтения в ответе — без участия модели (B1-11, #169)."""

from __future__ import annotations

from djedai.agent.graph import cite_metrics
from djedai.contracts.agent import Block, PlannedCall, Understanding
from djedai.contracts.freshness import ReadProvenance
from djedai.contracts.tool_result import ToolResult

ANSWER = Block(type="text", markdown="Сумма — 30.")
FRESH = {"state": "fresh", "profiled_at": "2026-10-26T09:00:00Z"}


def _read(snapshot: str = "run_2:1", finished: str = "2026-10-26T14:03:10Z", **change):
    return ReadProvenance.model_validate(
        {
            "revision_known": True,
            "dataset_id": "ds_1",
            "catalog_revision": 3,
            "schema_fingerprint": "a" * 64,
            "source_fingerprint": "b" * 64,
            "transform_version": "2026.10.1",
            "read_mode": "live",
            "consistency": "turn_snapshot",
            "snapshot_id": snapshot,
            "started_at": "2026-10-26T14:03:09Z",
            "finished_at": finished,
            "freshness": FRESH,
            **change,
        }
    )


def _result(result_id: str, read: ReadProvenance | None) -> ToolResult:
    return ToolResult(result_id=result_id, status="ok", tool="run_sql", read=read)


def _state(results, history=()) -> dict:
    return {
        "plan": [PlannedCall(tool="run_sql", args={"sql": "SELECT 1"})] if results else [],
        "results": list(results),
        "history_results": list(history),
        "focus_result_ids": [r.result_id for r in history],
        "understanding": Understanding(intent="data_question" if results else "modify_previous"),
        "answer_blocks": [ANSWER],
    }


def _texts(state) -> list[str]:
    return [block.markdown for block in cite_metrics(state).get("answer_blocks", [ANSWER])[1:]]


def test_live_read_is_cited_with_time_and_revision():
    (cited,) = _texts(_state([_result("r_1", _read())]))
    assert cited == (
        "**Как посчитано.** Данные: живое чтение источника 26.10.2026 14:03 UTC, каталог r3."
    )


def test_snapshot_cites_its_profile_time():
    read = _read(read_mode="snapshot", consistency="immutable", snapshot_id="r3")
    (cited,) = _texts(_state([_result("r_1", read)]))
    assert "снимок датасета, каталог r3 (профиль от 26.10.2026 09:00 UTC)" in cited


def test_saved_result_is_marked_as_not_recomputed():
    old = _result(
        "r_old",
        _read(
            snapshot="run_1:1",
            started_at="2026-10-20T07:59:59Z",
            finished="2026-10-20T08:00:00Z",
        ),
    )
    (cited,) = _texts(_state([], history=[old]))
    assert "сохранённый расчёт от 20.10.2026 08:00 UTC без пересчёта" in cited


def test_stale_profile_and_drift_become_data_limitations():
    aged = {**FRESH, "state": "aged", "reason": "Профиль каталога построен 30 ч назад"}
    limits, cited = _texts(_state([_result("r_1", _read(freshness=aged))]))
    assert limits.startswith("**Ограничения данных.**")
    assert "Профиль каталога построен 30 ч назад." in limits
    assert "каталог r3" in cited


def test_unknown_version_of_an_old_result_is_stated():
    limits, cited = _texts(_state([], history=[_result("r_old", ReadProvenance.unknown())]))
    assert "версия неизвестна" in limits
    assert "версия каталога и время чтения не сохранены" in cited


def test_reads_of_different_source_states_are_not_hidden():
    first = _result("r_1", _read(snapshot="run_2:1"))
    second = _result("r_2", _read(snapshot="run_2:2"))
    limits, _ = _texts(_state([first, second]))
    assert "прочитали источник в разных состояниях" in limits

    same = _result("r_3", _read(snapshot="run_2:1"))
    assert len(_texts(_state([first, same]))) == 1  # one snapshot: no limitation


def test_results_without_provenance_add_nothing():
    assert cite_metrics(_state([_result("r_1", None)])) == {}
