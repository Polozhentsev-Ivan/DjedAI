"""LLM-описания каталога (B1-6) с подставным клиентом модели.

Клиент записывает вызовы и отвечает по входному JSON, поэтому проверяются состав входа,
пакетность, кэш и поведение при сбоях без обращения к настоящей модели.
"""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest

from djedai.agent.client import ModelResponse
from djedai.agent.prompts import load_prompt
from djedai.contracts.catalog import DatasetCatalog
from djedai.data import descriptions
from djedai.data.descriptions import (
    DESCRIPTIONS_TABLE,
    MAX_DESCRIPTION_CHARS,
    DatasetSummary,
    TableDescriptions,
    describe_catalog,
)

EXAMPLE = (
    Path(__file__).parents[3] / "src" / "djedai" / "contracts" / "examples" / "dataset_catalog.json"
)


def answer_all(payload: dict, model: type) -> ModelResponse:
    if model is DatasetSummary:
        names = ", ".join(t["table"] for t in payload["tables"])
        return ModelResponse(status="success", data=DatasetSummary(summary=f"Данные: {names}"))
    return ModelResponse(
        status="success",
        data=TableDescriptions(
            table=f"Строка таблицы {payload['table']}",
            columns=[
                {"name": c["name"], "description": f"Смысл колонки {c['name']}"}
                for c in payload["columns"]
            ],
        ),
    )


class FakeClient:
    model = "fake-model"

    def __init__(self, respond=answer_all) -> None:
        self.respond = respond
        self.calls: list[tuple[str, dict, type]] = []

    def generate_typed_response(self, system_prompt, user_prompt, response_model, **kwargs):
        payload = json.loads(user_prompt)
        self.calls.append((system_prompt, payload, response_model))
        return self.respond(payload, response_model)


def error(error_type: str) -> ModelResponse:
    return ModelResponse(status="error", error_type=error_type, message="сбой")


def blank_catalog(**changes) -> DatasetCatalog:
    """Пример каталога без LLM-полей: как после профилирования."""
    data = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    data["summary"] = None
    for table in data["tables"]:
        table["description"] = None
        for column in table["columns"]:
            column["description"] = None
    data.update(changes)
    return DatasetCatalog.model_validate(data)


def only_clients() -> DatasetCatalog:
    clients = blank_catalog().tables[1].model_dump(mode="json")
    return blank_catalog(tables=[clients], relations=[], transformations=[], warnings=[])


@pytest.fixture(autouse=True)
def no_data_sharing(monkeypatch):
    """По умолчанию значения не передаются; локальный .env на тесты не влияет."""
    monkeypatch.delenv("ALLOW_DATA_SHARING", raising=False)
    monkeypatch.setattr("djedai.agent.config.dotenv_values", lambda *args, **kwargs: {})


@pytest.fixture
def share_data(monkeypatch):
    monkeypatch.setenv("ALLOW_DATA_SHARING", "true")


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "dataset.duckdb"


@pytest.fixture
def con(db_path):
    connection = duckdb.connect(str(db_path))
    yield connection
    connection.close()


def cached(con) -> int:
    return con.execute(f"SELECT count(*) FROM {DESCRIPTIONS_TABLE}").fetchone()[0]


def test_columns_tables_and_summary_are_described(con):
    catalog = blank_catalog()
    client = FakeClient()
    report = describe_catalog(con, catalog, client)

    described = report.catalog
    assert report.model_calls == len(catalog.tables) + 1
    assert report.warnings == ()
    for table in described.tables:
        assert table.description == f"Строка таблицы {table.table_id}"
        assert all(c.description == f"Смысл колонки {c.name}" for c in table.columns)
    assert described.summary == "Данные: orders, clients"
    # статистика и структура не меняются
    assert described.model_dump(exclude={"summary", "tables"}) == catalog.model_dump(
        exclude={"summary", "tables"}
    )
    prompt = described.to_prompt()
    assert "— Смысл колонки amount" in prompt and "Данные: orders, clients" in prompt


def test_table_call_sends_names_samples_and_stats(con, share_data):
    client = FakeClient()
    describe_catalog(con, blank_catalog(), client)

    system, payload, model = client.calls[0]
    assert model is TableDescriptions
    assert "Russian" in system
    assert payload["table"] == "orders" and payload["original_name"] == "Заказы_2024.csv"
    amount = next(c for c in payload["columns"] if c["name"] == "amount")
    assert amount["original_name"] == "Сумма"
    assert amount["currency"] == "RUB"
    assert {"min", "max"} <= amount["stats"].keys()
    order_id = next(c for c in payload["columns"] if c["name"] == "order_id")
    assert order_id["sample_values"] == ["000123", "000124", "000125", "000126", "000127"]
    sent = json.dumps(payload, ensure_ascii=False)
    for absent in ("description", "parse", "failed_examples", "encoding", "physical_type"):
        assert absent not in sent

    _, summary_input, model = client.calls[-1]
    assert model is DatasetSummary
    assert summary_input["relations"] == ["orders.client_id → clients.id (N:1)"]
    assert summary_input["tables"][0]["description"] == "Строка таблицы orders"
    assert summary_input["tables"][0]["periods"][0]["column"] == "created_at"


def test_second_read_uses_cache(con, db_path):
    first = describe_catalog(con, blank_catalog(), FakeClient())
    con.close()

    reopened = duckdb.connect(str(db_path))
    client = FakeClient()
    second = describe_catalog(reopened, blank_catalog(), client)
    reopened.close()

    assert (second.model_calls, client.calls) == (0, [])
    assert second.catalog == first.catalog


def test_changed_table_is_described_again(con, share_data):
    describe_catalog(con, blank_catalog(), FakeClient())
    data = blank_catalog().model_dump(mode="json", by_alias=True)
    data["tables"][1]["columns"][1]["sample_values"] = [True]
    client = FakeClient()
    describe_catalog(con, DatasetCatalog.model_validate(data), client)

    # пересчитана только изменённая таблица; вход summary (описания таблиц) не изменился
    assert [payload.get("table", "summary") for _, payload, _ in client.calls] == ["clients"]


def test_other_model_does_not_reuse_cache(con):
    describe_catalog(con, blank_catalog(), FakeClient())
    client = FakeClient()
    client.model = "other-model"
    assert describe_catalog(con, blank_catalog(), client).model_calls == 3


@pytest.mark.parametrize("failure", ["TIMEOUT", "SERVER_ERROR", "exception"])
def test_unavailable_model_keeps_statistical_catalog(con, failure):
    def respond(payload, model):
        if failure == "exception":
            raise ConnectionError("нет связи")
        return error(failure)

    catalog = blank_catalog()
    client = FakeClient(respond)
    report = describe_catalog(con, catalog, client)

    assert len(client.calls) == 1  # после отказа модель больше не вызывается
    described = report.catalog
    assert described.tables == catalog.tables
    assert described.summary is None
    expected = "CLIENT_ERROR" if failure == "exception" else failure
    (warning,) = report.warnings
    assert f"модель недоступна ({expected})" in warning
    assert described.warnings == [*catalog.warnings, warning]
    assert "Таблица orders" in described.to_prompt() and warning in described.to_prompt()
    assert cached(con) == 0
    # после восстановления модели описания создаются
    assert describe_catalog(con, catalog, FakeClient()).catalog.summary is not None


def test_rejected_answer_for_one_table(con):
    def respond(payload, model):
        if payload.get("table") == "orders":
            return error("SCHEMA_MISMATCH")
        return answer_all(payload, model)

    report = describe_catalog(con, blank_catalog(), FakeClient(respond))

    orders, clients = report.catalog.tables
    assert orders.description is None and all(c.description is None for c in orders.columns)
    assert clients.description is not None and all(c.description for c in clients.columns)
    assert report.catalog.summary == "Данные: orders, clients"
    assert report.warnings == (
        "Описания таблицы orders не сгенерированы: ответ модели не принят (SCHEMA_MISMATCH)",
    )
    assert cached(con) == 2  # clients и summary; отказ не кэшируется


def test_unknown_missing_and_non_russian_descriptions(con):
    def respond(payload, model):
        if model is DatasetSummary:
            return answer_all(payload, model)
        columns = [
            {"name": "ghost", "description": "Колонки нет в таблице"},
            {"name": payload["columns"][0]["name"], "description": "Customer identifier"},
            {"name": payload["columns"][1]["name"], "description": "  Флаг \n VIP  "},
        ]
        return ModelResponse(
            status="success", data=TableDescriptions(table="Клиенты", columns=columns)
        )

    catalog = only_clients()
    report = describe_catalog(con, catalog, FakeClient(respond))

    (clients,) = report.catalog.tables
    assert clients.description == "Клиенты"
    assert [c.description for c in clients.columns] == [None, "Флаг VIP"]
    assert report.warnings == ("Модель не описала в clients: id",)


def test_long_description_is_cut_by_word(con):
    long_text = "Очень длинное описание " * 30

    def respond(payload, model):
        if model is DatasetSummary:
            return answer_all(payload, model)
        columns = [{"name": c["name"], "description": long_text} for c in payload["columns"]]
        return ModelResponse(status="success", data=TableDescriptions(table="Т", columns=columns))

    report = describe_catalog(con, blank_catalog(), FakeClient(respond))
    text = report.catalog.tables[0].columns[0].description
    assert len(text) <= MAX_DESCRIPTION_CHARS and text.endswith("…")
    assert long_text.startswith(text[:-1])


def test_existing_descriptions_are_kept(con):
    data = blank_catalog().model_dump(mode="json", by_alias=True)
    data["summary"] = "Описание от пользователя"
    data["tables"][0]["columns"][0]["description"] = "Номер заказа из учётной системы"
    clients = data["tables"][1]
    clients["description"] = "Клиенты"
    for column in clients["columns"]:
        column["description"] = "Уже описано"
    client = FakeClient()
    report = describe_catalog(con, DatasetCatalog.model_validate(data), client)

    assert [payload["table"] for _, payload, _ in client.calls] == ["orders"]  # без summary
    orders = report.catalog.tables[0]
    assert orders.columns[0].description == "Номер заказа из учётной системы"
    assert orders.columns[1].description == "Смысл колонки client_id"
    assert report.catalog.summary == "Описание от пользователя"


def test_wide_table_is_described_in_parts(con, monkeypatch):
    monkeypatch.setattr("djedai.data.descriptions.MAX_COLUMNS_PER_CALL", 2)
    client = FakeClient()
    report = describe_catalog(con, blank_catalog(), client)

    parts = [
        [c["name"] for c in p["columns"]] for _, p, m in client.calls if m is TableDescriptions
    ]
    assert parts == [
        ["order_id", "client_id"],
        ["created_at", "amount"],
        ["status"],
        ["id", "is_vip"],
    ]
    orders = report.catalog.tables[0]
    assert all(c.description for c in orders.columns)


def test_empty_catalog_needs_no_model(con):
    catalog = blank_catalog(tables=[], relations=[], transformations=[], warnings=[])
    report = describe_catalog(con, catalog, FakeClient())
    assert (report.model_calls, report.catalog.summary) == (0, None)


def test_values_are_not_shared_when_disabled(con, share_data):
    client = FakeClient()
    report = describe_catalog(con, blank_catalog(), client, share_values=False)

    sent = json.dumps([payload for _, payload, _ in client.calls], ensure_ascii=False)
    for value in ("sample_values", "top_values", "000123", "C-0007", "доставлен"):
        assert value not in sent
    amount = next(c for c in client.calls[0][1]["columns"] if c["name"] == "amount")
    assert amount["original_name"] == "Сумма" and "max" in amount["stats"]
    assert report.catalog.tables[0].columns[0].description is not None
    # с примерами вход таблиц другой — их описания пересчитываются; вход summary тот же
    assert describe_catalog(con, blank_catalog(), FakeClient()).model_calls == 2


def test_prompt_change_invalidates_cache(con, monkeypatch):
    describe_catalog(con, blank_catalog(), FakeClient())
    original = descriptions.load_prompt
    monkeypatch.setattr(
        descriptions,
        "load_prompt",
        lambda name: original(name) + "\nNew rule." if name == "describe_table" else original(name),
    )
    client = FakeClient()
    describe_catalog(con, blank_catalog(), client)
    assert [payload.get("table", "summary") for _, payload, _ in client.calls] == [
        "orders",
        "clients",
    ]


@pytest.mark.parametrize("model", [TableDescriptions, DatasetSummary])
def test_answer_schemas_fit_strict_guided_decoding(model):
    """``strict: true`` требует у каждого объекта все поля в required и запрет лишних."""

    def objects(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                yield node
            for value in node.values():
                yield from objects(value)
        elif isinstance(node, list):
            for value in node:
                yield from objects(value)

    found = list(objects(model.model_json_schema()))
    assert found
    for node in found:
        assert node["additionalProperties"] is False
        assert set(node["required"]) == set(node["properties"])


def test_prompts_exist_and_ask_for_russian():
    for name in ("describe_table", "describe_dataset"):
        text = load_prompt(name)
        assert text != "Промпт не найден."
        assert "Russian" in text and "JSON" in text


def test_rejected_summary_is_not_cached(con):
    def english_summary(payload, model):
        if model is DatasetSummary:
            return ModelResponse(status="success", data=DatasetSummary(summary="Customer dataset"))
        return answer_all(payload, model)

    first = describe_catalog(con, blank_catalog(), FakeClient(english_summary))
    assert first.catalog.summary is None
    assert "не на русском" in " ".join(first.warnings)

    client = FakeClient()
    second = describe_catalog(con, blank_catalog(), client)
    # таблицы из кэша, summary запрошен снова и принят
    assert [m for _, _, m in client.calls] == [DatasetSummary]
    assert second.catalog.summary == "Данные: orders, clients"
    assert describe_catalog(con, blank_catalog(), FakeClient()).model_calls == 0


@pytest.mark.parametrize("broken", ["column", "table"])
def test_incomplete_table_answer_is_not_cached(con, broken):
    def partly_english(payload, model):
        response = answer_all(payload, model)
        if model is TableDescriptions and payload["table"] == "clients":
            data = response.data.model_dump()
            if broken == "column":
                data["columns"][0]["description"] = "Customer identifier"
            else:
                data["table"] = "Customers"
            response = ModelResponse(status="success", data=TableDescriptions(**data))
        return response

    first = describe_catalog(con, only_clients(), FakeClient(partly_english))
    (clients,) = first.catalog.tables
    assert None in (clients.description, clients.columns[0].description)
    assert cached(con) == 1  # только summary

    client = FakeClient()
    (clients,) = describe_catalog(con, only_clients(), client).catalog.tables
    assert [p.get("table") for _, p, m in client.calls if m is TableDescriptions] == ["clients"]
    assert clients.description == "Строка таблицы clients"
    assert all(c.description == f"Смысл колонки {c.name}" for c in clients.columns)


def test_cached_answer_for_other_missing_fields_is_not_reused(con):
    """Ответ, полный для каталога с заполненными полями, не блокирует пустой каталог."""

    def only_vip(payload, model):
        if model is DatasetSummary:
            return answer_all(payload, model)
        columns = [{"name": "is_vip", "description": "Признак VIP-клиента"}]
        return ModelResponse(
            status="success", data=TableDescriptions(table="Customers", columns=columns)
        )

    data = only_clients().model_dump(mode="json", by_alias=True)
    data["summary"] = "Клиенты магазина"
    data["tables"][0]["description"] = "Клиенты"
    data["tables"][0]["columns"][0]["description"] = "Идентификатор клиента"
    partial = describe_catalog(con, DatasetCatalog.model_validate(data), FakeClient(only_vip))
    assert partial.catalog.tables[0].columns[1].description == "Признак VIP-клиента"

    blank = only_clients().model_copy(update={"summary": "Клиенты магазина"})
    client = FakeClient()
    (clients,) = describe_catalog(con, blank, client).catalog.tables
    assert len(client.calls) == 1
    assert clients.description == "Строка таблицы clients"
    assert all(c.description == f"Смысл колонки {c.name}" for c in clients.columns)
    # полный ответ заменил неполный: теперь оба каталога читаются из кэша
    assert describe_catalog(con, blank, FakeClient()).model_calls == 0
