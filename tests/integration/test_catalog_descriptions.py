import json
from pathlib import Path

import duckdb
import pytest

from djedai.agent.config import get_agent_client
from djedai.contracts.catalog import DatasetCatalog
from djedai.data.descriptions import describe_catalog

EXAMPLE = Path(__file__).parents[2] / "src/djedai/contracts/examples/dataset_catalog.json"


@pytest.mark.integration
@pytest.mark.llm
def test_live_model_describes_example_catalog():
    """
    Интеграционный тест: описания примерного каталога настоящей моделью.
    Проверяет схему ответа, русский текст и кэш повторного чтения.
    Команда: uv run pytest -m llm tests/integration/test_catalog_descriptions.py -v -s
    """
    try:
        client = get_agent_client()
    except ValueError as error:
        pytest.skip(str(error))
    data = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    data["summary"] = None
    for table in data["tables"]:
        table["description"] = None
        for column in table["columns"]:
            column["description"] = None
    catalog = DatasetCatalog.model_validate(data)
    connection = duckdb.connect(":memory:")

    report = describe_catalog(connection, catalog, client)
    print(report.catalog.to_prompt())

    assert not any("не сгенерирован" in w for w in report.warnings), report.warnings
    assert report.catalog.summary
    assert all(t.description for t in report.catalog.tables)
    assert describe_catalog(connection, catalog, client).model_calls == 0
