from djedai.analytics.tools import describe_column, describe_dataset, value_counts


def test_describe_dataset():
    result = describe_dataset()
    assert result.status == "ok"
    assert "test" in result.data["tables"]
    assert "В датасете доступны таблицы" in result.facts[0]["text"]


def test_describe_column():
    result = describe_column("test", "city")
    assert result.status == "ok"
    assert result.data["total"] == 4
    assert result.data["non_null"] == 4
    assert result.data["distinct"] == 3  # Москва, СПб, Казань


def test_describe_column_invalid():
    result = describe_column("test", "invalid_column")
    assert result.status == "error"
    assert result.error.code == "invalid_column"
    assert "Доступные колонки" in result.error.suggestion


def test_value_counts():
    result = value_counts("test", "city")
    assert result.status == "ok"
    # Москва встречается 2 раза, должна быть первой
    assert result.data["rows"][0][0] == "Москва"
    assert result.data["rows"][0][1] == 2
    assert "Значение 'Москва' встречается 2 раз" in result.facts[0]["text"]
