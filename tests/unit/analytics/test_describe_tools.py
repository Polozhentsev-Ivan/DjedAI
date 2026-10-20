from djedai.analytics.tools import describe_column, describe_dataset, value_counts

# ---------- helpers ----------


def _profile(r) -> dict:
    """Склеивает все Fact(kind in {profile, numeric_profile}) в один dict."""
    out = {}
    for f in r.facts:
        if f.kind in ("profile", "numeric_profile"):
            out.update(f.values)
    return out


def _by_kind(r, kind):
    return [f for f in r.facts if f.kind == kind]


def _test_overview(r):
    """Fact с обзором таблицы 'test'."""
    return next(
        f for f in r.facts if f.kind == "table_overview" and f.values.get("table_name") == "test"
    )


# ---------- describe_dataset ----------


def test_describe_dataset():
    r = describe_dataset()
    assert r.status == "ok"
    assert r.data is not None
    # Табличное представление: одна строка на таблицу
    assert r.data.columns == ["table_name", "row_count", "columns"]
    test_row = next(row for row in r.data.rows if row[0] == "test")
    assert test_row[1] == 4
    # Число строк и колонки — в fact.values
    ov = _test_overview(r)
    assert ov.values["row_count"] == 4
    assert len(ov.values["columns"]) == 3
    assert set(ov.values["columns"]) == {"id", "city", "age"}


def test_columns_have_types():
    ov = _test_overview(describe_dataset())
    types = ov.values["column_types"]
    assert "INT" in types["id"].upper()
    assert "VARCHAR" in types["city"].upper()


def test_facts_contain_table_summary():
    r = describe_dataset()
    joined = " ".join(f.text for f in r.facts)
    assert "Таблица 'test'" in joined
    assert "4" in joined
    assert all(col in joined for col in ("id", "city", "age"))


# ---------- describe_column ----------


def test_describe_column():
    r = describe_column("test", "city")
    assert r.status == "ok"
    p = _profile(r)
    assert p["total"] == 4
    assert p["non_null"] == 4
    assert p["distinct"] == 3


def test_basic_counts():
    p = _profile(describe_column("test", "age"))
    assert p["total"] == 4
    assert p["non_null"] == 3
    assert p["distinct"] == 2
    assert p["null_count"] == 1


def test_range_and_center():
    p = _profile(describe_column("test", "age"))
    assert p["min"] == 25
    assert p["max"] == 30
    assert p["mean"] == round(80 / 3, 2)
    assert p["median"] == 25


def test_std_present():
    p = _profile(describe_column("test", "age"))
    assert "std" in p
    assert p["std"] is not None
    assert 2.0 <= float(p["std"]) <= 3.5


def test_median_in_facts():
    r = describe_column("test", "age")
    joined = " ".join(f.text for f in r.facts)
    assert "Медиана" in joined and "25" in joined


def test_frequencies_present():
    r = describe_column("test", "age")
    assert len(_by_kind(r, "frequency")) >= 1
    # И табличное представление есть
    assert r.data is not None and r.data.rows


def test_examples_present():
    r = describe_column("test", "age")
    examples = _by_kind(r, "examples")
    assert len(examples) == 1
    assert len(examples[0].values["examples"]) >= 1


def test_city_has_frequencies():
    r = describe_column("test", "city")
    freqs = _by_kind(r, "frequency")
    top = {f.values["value"]: f.values["count"] for f in freqs}
    assert top.get("Москва") == 2


def test_describe_column_numeric_full_profile():
    r = describe_column("test", "age")
    p = _profile(r)
    assert p["total"] == 4
    assert p["non_null"] == 3
    assert p["null_count"] == 1
    assert p["min"] == 25
    assert p["max"] == 30
    assert p["median"] == 25
    assert "std" in p
    assert _by_kind(r, "frequency")
    assert _by_kind(r, "examples")
    assert any("Медиана" in f.text for f in r.facts)


def test_describe_column_invalid():
    r = describe_column("test", "invalid_column")
    assert r.status == "error"
    assert r.error.code == "invalid_column"
    assert "Доступные колонки" in r.error.suggestion


# ---------- value_counts ----------


def test_value_counts():
    r = value_counts("test", "city")
    assert r.status == "ok"
    assert r.data is not None
    assert r.data.rows[0][0] == "Москва"
    assert r.data.rows[0][1] == 2
    assert "Москва" in r.facts[0].text and "2 раз" in r.facts[0].text


def test_value_counts_with_filter():
    r = value_counts("test", "city", filter="age > 20")
    assert r.status == "ok"
    rows = {row[0]: row[1] for row in r.data.rows}
    assert rows == {"Москва": 2, "СПб": 1}


def test_accepts_filter_keyword():
    r = value_counts("test", "city", filter="age > 100")
    assert r.status == "empty"
    assert "не найдены" in r.facts[0].text


# ---------- injection ----------


def test_sql_injection_protection():
    r = describe_column("test'); DELETE FROM test; --", "age")
    assert r.status == "error"


def test_describe_column_injection_does_not_delete():
    describe_column("test'); DELETE FROM test; --", "age")
    assert _test_overview(describe_dataset()).values["row_count"] == 4


def test_value_counts_injection_does_not_delete():
    value_counts("test'); DELETE FROM test; --", "city")
    assert _test_overview(describe_dataset()).values["row_count"] == 4


def test_column_injection_does_not_break_schema():
    describe_column("test", "age'); DROP TABLE test; --")
    ov = _test_overview(describe_dataset())
    assert ov.values["row_count"] == 4
    assert set(ov.values["columns"]) == {"id", "city", "age"}


def test_injection_data_integrity():
    describe_column("test'); DELETE FROM test; --", "age")
    assert _test_overview(describe_dataset()).values["row_count"] == 4, "Инъекция удалила данные!"
