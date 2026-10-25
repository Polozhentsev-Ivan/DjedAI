"""Per-stage reasoning profile shared by configuration, transport and benchmarks."""

from __future__ import annotations

from collections.abc import Mapping
from typing import get_args

from djedai.config import ReasoningEffort

REASONING_STAGES = ("understand", "plan", "visualize", "compose", "verify")
REASONING_EFFORTS = frozenset(get_args(ReasoningEffort))
PROVIDER_DEFAULT: ReasoningEffort = "provider_default"


def normalize_reasoning_profile(
    profile: Mapping[str, ReasoningEffort] | None = None,
) -> dict[str, ReasoningEffort]:
    """Validate a partial stage profile and fill omitted stages with provider_default.

    Unknown stages and values are rejected: a typo must not silently fall back to
    the provider default.
    """
    normalized = {stage: PROVIDER_DEFAULT for stage in REASONING_STAGES}
    for stage, effort in (profile or {}).items():
        if stage not in normalized:
            raise ValueError(
                f"Неизвестная стадия reasoning: {stage!r}; "
                f"допустимые стадии: {', '.join(REASONING_STAGES)}"
            )
        if effort not in REASONING_EFFORTS:
            raise ValueError(
                f"Неизвестный режим reasoning для {stage}: {effort!r}; "
                f"допустимые значения: {', '.join(sorted(REASONING_EFFORTS))}"
            )
        normalized[stage] = effort
    return normalized


def actual_reasoning_effort(effort: ReasoningEffort) -> str | None:
    """Return the value sent to the provider, or None when the parameter is omitted."""
    return None if effort == PROVIDER_DEFAULT else effort
