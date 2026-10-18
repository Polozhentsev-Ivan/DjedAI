from src.djedai.contracts.tool_result import ToolError, ToolResult


def test_tool_result_creation():
    # Проверяем, что "коробка" создается без ошибок
    result = ToolResult(result_id="r_1", status="ok", facts=[{"text": "Выручка 100 рублей"}])
    assert result.result_id == "r_1"
    assert result.status == "ok"
    assert len(result.facts) == 1


def test_tool_error():
    # Проверяем, что ошибка корректно прикрепляется
    err = ToolError(code="bad_sql", message="Ошибка синтаксиса", suggestion="Проверьте запрос")
    result = ToolResult(result_id="r_2", status="error", error=err)
    assert result.error.code == "bad_sql"
