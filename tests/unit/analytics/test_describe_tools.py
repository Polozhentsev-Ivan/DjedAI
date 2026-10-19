from djedai.analytics.tools import describe_column, describe_dataset, value_counts


def test_describe_dataset():
    result = describe_dataset()
    assert result.status == "ok"
    table_names = [t["table_name"] for t in result.data["tables"]]
    assert "test" in table_names

    # Дополнительно проверим, что мы получили строки и колонки (это то, что просил Иван)
    test_table = next(t for t in result.data["tables"] if t["table_name"] == "test")
    assert test_table["row_count"] == 4
    assert len(test_table["columns"]) == 3  # id, city, age


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


def test_sql_injection_protection():
    # Пытаемся удалить таблицу через имя таблицы
    result = describe_column("test'); DELETE FROM test; --", "age")
    assert result.status == "error"
    assert result.error.code == "invalid_column"  # Или db_error


def test_value_counts_with_filter():
    # Проверяем, что фильтр реально работает (Москва с age > 20 - это 1 строка)
    result = value_counts("test", "city", filter="age > 20")
    assert result.status == "ok"

    # Проверяем пустой результат
    empty_result = value_counts("test", "city", filter="age > 100")
    assert empty_result.status == "empty"
    assert "не найдены" in empty_result.facts[0]["text"]


def test_describe_column_numeric_full_profile():
    result = describe_column("test", "age")
    assert result.status == "ok"
    assert result.data["total"] == 4
    assert result.data["non_null"] == 3
    assert result.data["null_count"] == 1
    assert result.data["min"] == 25
    assert result.data["max"] == 30
    assert result.data["median"] == 25

    assert "std" in result.data
    assert "frequencies" in result.data
    assert "examples" in result.data
    assert any("Медиана" in f["text"] for f in result.facts)


def test_basic_counts():
    """[GREEN] Базовые счётчики уже есть."""
    r = describe_column("test", "age")
    assert r.status == "ok"
    assert r.data["total"] == 4
    assert r.data["non_null"] == 3
    assert r.data["distinct"] == 2
    assert r.data["null_count"] == 1


def test_range_and_center():
    """[GREEN] min / max / mean / median уже вычисляются."""
    r = describe_column("test", "age")
    assert r.data["min"] == 25
    assert r.data["max"] == 30
    # ages: 25, 25, 30 → mean = 80/3 ≈ 26.67
    assert r.data["mean"] == round(80 / 3, 2)
    assert r.data["median"] == 25


def test_std_present():
    """[RED] std отсутствует — добавить STDDEV_POP/STDDEV_SAMP в запрос."""
    r = describe_column("test", "age")
    assert "std" in r.data, "Ожидается поле std (стандартное отклонение)"
    assert r.data["std"] is not None
    # pop ≈ 2.36, sample ≈ 2.89 — берём широкий коридор
    assert 2.0 <= float(r.data["std"]) <= 3.5


def test_median_in_facts():
    """[RED] median есть в data, но в facts не попадает."""
    r = describe_column("test", "age")
    joined = " ".join(f["text"] for f in r.facts)
    assert "Медиана" in joined and "25" in joined, f"Медиана должна быть в facts, сейчас: {r.facts}"


def test_frequencies_present():
    """[RED] частот в профиле нет (ожидаем топ значений, как в value_counts)."""
    r = describe_column("test", "age")
    assert "frequencies" in r.data or "value_counts" in r.data, (
        "Ожидается блок частот значений колонки"
    )


def test_examples_present():
    """[RED] примеров значений в профиле нет."""
    r = describe_column("test", "age")
    assert "examples" in r.data, "Ожидается несколько примеров значений"
    assert isinstance(r.data["examples"], list)
    assert len(r.data["examples"]) >= 1


def test_city_has_frequencies():
    """[RED] Ожидаем хотя бы топ частот (Москва ×2, СПб ×1, Казань ×1)."""
    r = describe_column("test", "city")
    assert "frequencies" in r.data or "value_counts" in r.data, (
        "Для категориальной колонки тоже нужен блок частот"
    )
    freqs = r.data.get("frequencies") or r.data.get("value_counts") or []
    top = {row[0]: row[1] for row in freqs}
    assert top.get("Москва") == 2


def test_accepts_filter_keyword():
    """[RED] Сейчас TypeError: unexpected keyword 'filter'."""
    r = value_counts("test", "city", filter="age > 100")
    assert r.status == "empty"


def test_filter_actually_filters():
    """[GREEN] Фильтр реально ограничивает данные."""
    r = value_counts("test", "city", filter="age > 20")
    assert r.status == "ok"
    # После фильтра age > 20: Москва(25), СПб(30), Москва(25).
    # Казань с age=NULL отпадает → остаётся Москва=2, СПб=1.
    rows = {row[0]: row[1] for row in r.data["rows"]}
    assert rows == {"Москва": 2, "СПб": 1}, f"Неожиданные данные: {rows}"


def test_describe_column_injection_does_not_delete():
    """[GREEN] Код безопасен — но проверим целостность данных."""
    result = describe_column("test'); DELETE FROM test; --", "age")
    assert result.status == "error"

    check = describe_dataset()
    test_table = next(t for t in check.data["tables"] if t["table_name"] == "test")
    assert test_table["row_count"] == 4, (
        f"Инъекция испортила данные: строк {test_table['row_count']}"
    )


def test_value_counts_injection_does_not_delete():
    """[GREEN] То же для value_counts."""
    result = value_counts("test'); DELETE FROM test; --", "city")
    assert result.status == "error"

    check = describe_dataset()
    test_table = next(t for t in check.data["tables"] if t["table_name"] == "test")
    assert test_table["row_count"] == 4


def test_column_injection_does_not_break_schema():
    """[GREEN] Инъекция через имя колонки не должна менять схему."""
    result = describe_column("test", "age'); DROP TABLE test; --")
    assert result.status == "error"
    # Таблица и её колонки должны остаться на месте
    check = describe_dataset()
    test_table = next(t for t in check.data["tables"] if t["table_name"] == "test")
    assert test_table["row_count"] == 4
    assert {c["name"] for c in test_table["columns"]} == {"id", "city", "age"}


def test_columns_have_types():
    """[GREEN] Типы колонок должны быть в обзоре."""
    r = describe_dataset()
    test_table = next(t for t in r.data["tables"] if t["table_name"] == "test")
    types = {c["name"]: c["type"] for c in test_table["columns"]}
    assert "INT" in types["id"].upper()
    assert "VARCHAR" in types["city"].upper()


def test_facts_contain_table_summary():
    """[GREEN] Факты должны описывать таблицу с числом строк и колонками."""
    r = describe_dataset()
    joined = " ".join(f["text"] for f in r.facts)
    assert "Таблица 'test'" in joined
    assert "4" in joined
    assert all(col in joined for col in ("id", "city", "age"))


def test_injection_data_integrity():
    # [P1] Пытаемся удалить таблицу через имя таблицы
    result = describe_column("test'); DELETE FROM test; --", "age")
    assert result.status == "error"

    # ГЛАВНАЯ ПРОВЕРКА: таблица должна остаться целой!
    check = describe_dataset()
    test_table = next(t for t in check.data["tables"] if t["table_name"] == "test")
    assert test_table["row_count"] == 4, "Инъекция удалила данные!"
