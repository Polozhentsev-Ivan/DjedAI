import sys
from pathlib import Path

import pytest

from djedai.agent.config import get_agent_client
from djedai.agent.graph import VERIFICATION_ERROR_MARKDOWN, build_agent_graph
from djedai.contracts.agent import Message

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.eval_runner import open_dataset  # noqa: E402


@pytest.mark.integration
@pytest.mark.llm
def test_live_agent_api_call(tmp_path: Path):
    """
    Интеграционный тест: Вызов реальной модели через граф.
    Проверяет валидность JSON Schema и прохождение стейта через узлы.
    Команда: uv run pytest -m "integration and llm" tests/integration/test_live_api.py -v -s

    Датасет подаётся через production ingestion, чтобы у модели были настоящие
    факты и строки. С #167 неподтверждённое число не публикуется: если модель
    не смогла подтвердить числа, корректный исход — явная ошибка проверки.
    """
    client = get_agent_client()
    assert client.client.api_key, "MODEL_API_KEY не установлен в .env!"

    data_file = tmp_path / "sales.csv"
    data_file.write_text(
        "id;revenue;date\n1;1000;2024-01-01\n2;2000;2024-01-02\n",
        encoding="utf-8-sig",
    )
    dataset = open_dataset(
        data_file,
        {"dataset_id": "live_sales", "source": {"file_name": data_file.name}},
    )
    try:
        graph = build_agent_graph()
        config = {
            "configurable": {
                "thread_id": "live_test",
                "client": client,
                "catalog_loader": dataset.load_catalog,
                "tool_dispatcher": dataset.dispatch,
            }
        }

        initial_state = {
            "chat_id": "live_test_chat",
            "dataset_id": dataset.catalog.dataset_id,
            "messages": [
                Message(
                    role="user",
                    content=(
                        "Show the total revenue as a chart. "
                        "In the text, state only the total number."
                    ),
                )
            ],
        }

        result = graph.invoke(initial_state, config=config)
    finally:
        dataset.close()

    blocks = result.get("answer_blocks", [])
    errors = [block for block in blocks if block.type == "error"]
    if errors:
        # Явная ошибка проверки чисел — допустимый исход для неподтверждённого
        # черновика. Ошибка API или таймаут сюда не относятся: у них отчёт
        # проверки не имеет статуса failed.
        report = result.get("verification")
        assert report is not None and report.status == "failed", (
            f"API call failed: {blocks}; verification={report}"
        )
        assert any(
            block.type == "error" and block.markdown == VERIFICATION_ERROR_MARKDOWN
            for block in errors
        ), f"API call failed: {blocks}; verification={report}"
        assert all(block.type not in ("text", "chart") for block in blocks)
    else:
        assert result.get("verification") is None or result["verification"].status in {
            "verified",
            "corrected",
            "not_applicable",
        }

    # Интент должен быть определен успешно, без fallback-ошибки
    assert result["understanding"].intent in ["visualize", "data_question", "dashboard"]

    # Блоки ответа сформированы успешно
    assert len(blocks) >= 1
