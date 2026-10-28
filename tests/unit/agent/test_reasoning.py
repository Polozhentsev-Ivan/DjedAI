import pytest

from djedai.agent.reasoning import (
    PROVIDER_DEFAULT,
    actual_reasoning_effort,
    normalize_reasoning_profile,
)


def test_partial_profile_fills_other_stages_with_provider_default():
    assert normalize_reasoning_profile({"understand": "none", "verify": "high"}) == {
        "understand": "none",
        "plan": PROVIDER_DEFAULT,
        "visualize": PROVIDER_DEFAULT,
        "compose": PROVIDER_DEFAULT,
        "verify": "high",
    }


def test_unknown_stage_is_rejected():
    with pytest.raises(ValueError, match="Неизвестная стадия reasoning"):
        normalize_reasoning_profile({"sql": "none"})


def test_unknown_effort_is_rejected():
    with pytest.raises(ValueError, match="Неизвестный режим reasoning"):
        normalize_reasoning_profile({"plan": "turbo"})


@pytest.mark.parametrize(
    "setting,expected",
    [
        ("provider_default", None),
        ("none", "none"),
        ("low", "low"),
        ("medium", "medium"),
        ("high", "high"),
    ],
)
def test_actual_effort_is_omitted_only_for_provider_default(setting, expected):
    assert actual_reasoning_effort(setting) == expected
