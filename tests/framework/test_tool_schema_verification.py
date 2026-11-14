# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""deal / Hypothesis / CrossHair (FQN) for tool schema normalize helpers."""

from __future__ import annotations

import copy

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from plugin.framework.tool_schema import _normalize_schema_for_strict_providers
from tests.harness.crosshair import assert_crosshair_clean

_CROSSHAIR_TARGET = "plugin.framework.tool_schema._normalize_schema_for_strict_providers"


_scalar = st.sampled_from(["string", "integer", "number", "boolean"])
_prop = st.fixed_dictionaries({"type": _scalar})
_schema = st.fixed_dictionaries(
    {
        "type": st.just("object"),
        "properties": st.dictionaries(st.sampled_from(["a", "b", "c"]), _prop, min_size=0, max_size=3),
        "required": st.lists(st.sampled_from(["a", "b", "c"]), max_size=3, unique=True),
    }
)


@given(schema=_schema)
@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_hypothesis_normalize_idempotent(schema: dict) -> None:
    # Compare against independent copies: if normalize mutated its input,
    # ``twice == once`` would compare one object with itself and always pass.
    original = copy.deepcopy(schema)
    once = _normalize_schema_for_strict_providers(schema)
    assert schema == original, "normalize must not mutate its input"
    once_snapshot = copy.deepcopy(once)
    twice = _normalize_schema_for_strict_providers(once)
    assert once == once_snapshot, "normalize must not mutate its input"
    assert twice is not once
    assert twice == once_snapshot
    assert isinstance(once, dict)
    assert once.get("required") != []


@pytest.mark.slow
def test_crosshair_normalize_schema_fqn_if_available() -> None:
    assert_crosshair_clean(_CROSSHAIR_TARGET)
