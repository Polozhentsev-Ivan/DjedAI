from djedai.analytics.tools import segment_profile


def _by_kind(result, kind):
    return [f for f in result.facts if f.kind == kind]


def test_basic_profile_by_city():
    result = segment_profile("test", ["city"], measures=["age"])
    assert result.status == "ok"
    assert result.data.columns == [
        "city",
        "segment_size",
        "age_mean",
        "age_median",
        "age_min",
        "age_max",
        "age_std",
    ]
    by_city = {row[0]: row for row in result.data.rows}
    assert by_city["Москва"][1] == 2
    assert by_city["Москва"][2] == 25.0  # mean
    assert by_city["СПб"][1] == 1
    assert by_city["Казань"][1] == 1
    assert by_city["Казань"][2] is None  # mean of NULL


def test_no_measures_counts_only():
    result = segment_profile("test", ["city"])
    assert result.status == "ok"
    assert result.data.columns == ["city", "segment_size"]
    assert sum(r[1] for r in result.data.rows) == 4


def test_summary_facts_present_dub():
    result = segment_profile("test", ["city"], measures=["age"])
    assert _by_kind(result, "segment_summary")
    assert _by_kind(result, "segment_sample")
    assert _by_kind(result, "segment_measure")


def test_top_n_limits_and_truncates():
    result = segment_profile("test", ["city"], top_n=1)
    assert result.status == "ok"
    assert len(result.data.rows) == 1
    assert result.data.truncated is True


def test_empty_result_status_dub():
    result = segment_profile("test", ["city"], filter="city = 'Нет такого'")
    assert result.status == "empty"
    assert _by_kind(result, "empty_result")


def test_invalid_column_returns_error_dub():
    result = segment_profile("test", ["city"], measures=["nonexistent"])
    assert result.status == "error"
    assert result.error.code == "invalid_column"


def test_non_numeric_measure_returns_error():
    result = segment_profile("test", ["city"], measures=["city"])
    assert result.status == "error"
    assert result.error.code == "invalid_column"


def test_invalid_filter_returns_error_dub():
    result = segment_profile(
        "test",
        ["city"],
        filter="1=1; DROP TABLE test",
    )
    assert result.status == "error"
    assert result.error.code == "invalid_filter"


def test_empty_group_by_returns_error():
    result = segment_profile("test", [])
    assert result.status == "error"
    assert result.error.code == "invalid_parameter"
