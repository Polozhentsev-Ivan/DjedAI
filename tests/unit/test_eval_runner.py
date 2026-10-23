import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.eval_runner import check_fact, run_eval  # noqa: E402


def test_check_fact_exact_match():
    fact = {"value": 1000}
    # Значение есть в тексте
    assert check_fact(fact, "Revenue is 1000 RUB", "") is True
    # Значения нет в тексте, но есть в сырых данных
    assert check_fact(fact, "Revenue calculated", '{"revenue": 1000}') is True
    # Значения нет нигде
    assert check_fact(fact, "Revenue is 999", '{"revenue": 999}') is False


def test_check_fact_nested_list():
    fact = {"value": [["success", 50], ["error", 2]]}
    # Проверка должна развернуть список (flatten) и найти все компоненты: success, 50, error, 2
    assert check_fact(fact, "We had 50 success and 2 error events.", "") is True
    # Чего-то не хватает
    assert check_fact(fact, "We had 50 success events.", "") is False


@patch("scripts.eval_runner.get_agent_client")
@patch("scripts.eval_runner.build_agent_graph")
@patch("scripts.eval_runner.yaml.load")
def test_run_eval_skips_unsupported(mock_yaml_load, mock_build_graph, mock_get_client, tmp_path):
    mock_yaml_load.return_value = [
        {
            "id": "unsupported_case",
            "dataset": "ds_1",
            "expect": {
                "requirements": ["dashboards"]  # dashboards пока не поддерживаются
            },
        }
    ]

    mock_graph = MagicMock()
    mock_build_graph.return_value = mock_graph

    dummy_yaml = tmp_path / "dummy.yaml"
    dummy_yaml.write_text("dummy")

    output_file = tmp_path / "report.json"

    report = run_eval(dummy_yaml, output_file)

    mock_graph.invoke.assert_not_called()
    assert report["summary"]["skipped"] == 1
    assert report["summary"]["total"] == 0
    assert report["cases"][0]["status"] == "skipped"


@patch("scripts.eval_runner.get_agent_client")
@patch("scripts.eval_runner.build_agent_graph")
@patch("scripts.eval_runner.yaml.load")
def test_run_eval_success_scenario(mock_yaml_load, mock_build_graph, mock_get_client, tmp_path):
    mock_yaml_load.return_value = [
        {
            "id": "success_case",
            "dataset": "ds_1",
            "expect": {
                "outcome": "answer",
                "tools_any": ["run_sql"],
                "facts": [{"key": "count", "value": 42}],
            },
        }
    ]

    mock_graph = MagicMock()

    class MockToolResult:
        def __init__(self):
            self.tool = "run_sql"
            self.data = {"count": 42}

    class MockBlock:
        def __init__(self):
            self.markdown = "The answer is 42."

    mock_graph.invoke.return_value = {"results": [MockToolResult()], "answer_blocks": [MockBlock()]}
    mock_build_graph.return_value = mock_graph

    # СОЗДАЕМ РЕАЛЬНЫЙ ФАЙЛ-ПУСТЫШКУ
    dummy_yaml = tmp_path / "dummy.yaml"
    dummy_yaml.write_text("dummy")

    output_file = tmp_path / "report.json"
    report = run_eval(dummy_yaml, output_file)

    assert report["summary"]["passed"] == 1
    assert report["summary"]["failed"] == 0
    assert report["cases"][0]["status"] == "passed"
    assert report["cases"][0]["used_tools"] == ["run_sql"]
