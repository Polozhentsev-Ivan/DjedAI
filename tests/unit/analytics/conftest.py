import duckdb
import pytest


@pytest.fixture
def conn():
    """9 «нормальных» значений 10..12 и один явный выброс 100."""
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE metrics (id INT, value DOUBLE)")
    rows = [(i, 10 + (i % 3)) for i in range(1, 10)] + [(10, 100.0)]
    # id=1..9: value ∈ {10, 11, 12}; id=10: value=100.0
    con.executemany("INSERT INTO metrics VALUES (?, ?)", rows)
    return con


@pytest.fixture
def contract_check():
    """Компараторы для контрактных проверок инструментов (#70)."""

    def check_ok(result):
        assert result.status == "ok", (
            f"status={result.status}, error={result.error}, facts={[f.kind for f in result.facts]}"
        )

    def check_error(result, code):
        assert result.status == "error", f"ожидалась ошибка {code}, status={result.status}"
        assert result.error is not None
        assert result.error.code == code, (
            f"ожидался код {code}, получен {result.error.code}: {result.error.message}"
        )

    def check_chart(result, type_, x=None, y=None):
        hint = result.chart_hint
        assert hint is not None, "chart_hint отсутствует"
        assert hint.type == type_, f"chart_hint.type={hint.type}, ожидалось {type_}"
        if x is not None:
            assert hint.x == x, f"chart_hint.x={hint.x}, ожидалось {x}"
        if y is not None:
            assert hint.y == y, f"chart_hint.y={hint.y}, ожидалось {y}"

    def check_fact(result, kind):
        found = [f for f in result.facts if f.kind == kind]
        assert found, f"нет fact kind={kind}, есть {[f.kind for f in result.facts]}"
        return found[0]

    class _Contract:
        ok = staticmethod(check_ok)
        error = staticmethod(check_error)
        chart = staticmethod(check_chart)
        fact = staticmethod(check_fact)

    return _Contract
