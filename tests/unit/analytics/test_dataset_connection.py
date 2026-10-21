from pathlib import Path

import duckdb
import pytest

from djedai.analytics.registry import execute_tool
from djedai.analytics.tools import value_counts


def test_all_tools_use_injected_dataset_and_leave_connection_open(tmp_path: Path):
    path = tmp_path / "dataset.duckdb"
    with duckdb.connect(str(path)) as connection:
        connection.execute("CREATE TABLE uploaded (amount INTEGER, segment VARCHAR)")
        connection.execute("INSERT INTO uploaded VALUES (10, 'A'), (20, 'A'), (30, 'B')")
    with duckdb.connect(str(path), read_only=True) as connection:
        sql = execute_tool(
            "run_sql", {"sql": "SELECT COUNT(*) FROM uploaded"}, connection=connection
        )
        assert sql.data.rows == [[3]]
        assert (
            execute_tool("run_sql", {"sql": "SELECT * FROM test"}, connection=connection).status
            == "error"
        )
        overview = execute_tool("describe_dataset", {}, connection=connection)
        assert overview.data.rows == [["uploaded", 3, "amount, segment"]]
        column = execute_tool(
            "describe_column", {"table": "uploaded", "column": "amount"}, connection=connection
        )
        assert column.facts[0].values["total"] == 3
        counts = execute_tool(
            "value_counts", {"table": "uploaded", "column": "segment"}, connection=connection
        )
        assert counts.data.rows == [["A", 2], ["B", 1]]
        assert (
            execute_tool(
                "run_sql", {"sql": "DROP TABLE uploaded"}, connection=connection
            ).error.code
            == "forbidden_operation"
        )
        assert connection.execute("SELECT SUM(amount) FROM uploaded").fetchone() == (60,)


def test_two_datasets_do_not_share_test_data():
    for count in (3, 7):
        with duckdb.connect() as connection:
            connection.execute(f"CREATE TABLE uploaded AS SELECT * FROM range({count})")
            result = execute_tool(
                "run_sql", {"sql": "SELECT COUNT(*) FROM uploaded"}, connection=connection
            )
            assert result.data.rows == [[count]]


@pytest.mark.parametrize("through_registry", [False, True])
def test_value_counts_blocks_external_files_on_its_first_call(
    tmp_path: Path, through_registry: bool
):
    external = tmp_path / "outside.csv"
    external.write_text("value\n1\n", encoding="utf-8")
    with duckdb.connect() as connection:
        connection.execute("CREATE TABLE uploaded (segment VARCHAR)")
        connection.execute("INSERT INTO uploaded VALUES ('A')")
        params = {
            "table": "uploaded",
            "column": "segment",
            "filter": f"EXISTS (SELECT 1 FROM read_csv('{external.as_posix()}'))",
        }
        result = (
            execute_tool("value_counts", params, connection=connection)
            if through_registry
            else value_counts(**params, connection=connection)
        )
        assert result.status == "error"
        assert result.error.code == "db_error"
        assert connection.execute("SELECT COUNT(*) FROM uploaded").fetchone() == (1,)
