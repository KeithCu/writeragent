# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""deal / Hypothesis / CrossHair (FQN) for json_utils.safe_json_loads."""

from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from plugin.framework.json_utils import safe_json_loads

_CROSSHAIR_ERROR_RE = re.compile(r": error:")
_CROSSHAIR_TARGET = "plugin.framework.json_utils.safe_json_loads"


def _json_values_equal(a: Any, b: Any) -> bool:
    """Deep equality that treats NaN == NaN (json.loads allows non-RFC NaN)."""
    if isinstance(a, float) and isinstance(b, float) and math.isnan(a) and math.isnan(b):
        return True
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_json_values_equal(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_json_values_equal(a[k], b[k]) for k in a)
    return a == b


def _find_crosshair() -> str | None:
    crosshair_path = shutil.which("crosshair")
    if crosshair_path:
        return crosshair_path
    venv_bin_ch = Path(".venv/bin/crosshair")
    if venv_bin_ch.exists():
        return str(venv_bin_ch)
    return None


def test_non_str_returns_default() -> None:
    assert safe_json_loads(None, default={"x": 1}) == {"x": 1}
    assert safe_json_loads(123, default="d") == "d"
    assert safe_json_loads(["a"], default=None) is None


def test_empty_whitespace_returns_default() -> None:
    assert safe_json_loads("", default=42) == 42
    assert safe_json_loads("   \n\t", default=42) == 42
    assert safe_json_loads(b"", default=7) == 7


@given(
    value=st.one_of(
        st.none(),
        st.booleans(),
        st.integers(min_value=-1000, max_value=1000),
        st.floats(allow_nan=False, allow_infinity=False, width=32),
        st.text(max_size=20),
        st.lists(st.integers(min_value=-10, max_value=10), max_size=4),
        st.dictionaries(st.text(min_size=1, max_size=6, alphabet="abc"), st.integers(min_value=-10, max_value=10), max_size=3),
    )
)
@settings(max_examples=80)
def test_hypothesis_valid_json_round_trip(value) -> None:
    encoded = json.dumps(value)
    assert safe_json_loads(encoded, strict=True) == value


@given(garbage=st.text(max_size=30).filter(lambda s: s.strip() != ""))
@settings(max_examples=60)
def test_hypothesis_strict_garbage_returns_default(garbage: str) -> None:
    # safe_json_loads in strict mode maps json.loads failures AND None return to default
    try:
        parsed = json.loads(garbage)
    except (json.JSONDecodeError, ValueError, TypeError):
        assert safe_json_loads(garbage, default="SENTINEL", strict=True) == "SENTINEL"
        return
    if parsed is None:
        assert safe_json_loads(garbage, default="SENTINEL", strict=True) == "SENTINEL"
    else:
        assert _json_values_equal(safe_json_loads(garbage, default="SENTINEL", strict=True), parsed)


def test_strict_rejects_single_quoted() -> None:
    assert safe_json_loads("{'a': 1}", default="d", strict=True) == "d"


@pytest.mark.slow
def test_crosshair_safe_json_loads_fqn_if_available() -> None:
    crosshair_path = _find_crosshair()
    if not crosshair_path:
        pytest.skip("CrossHair concolic execution engine is not installed.")
    result = subprocess.run(
        [crosshair_path, "check", "-v", "--report_all", _CROSSHAIR_TARGET],
        capture_output=True,
        text=True,
        timeout=300,
    )
    combined = f"{result.stdout}\n{result.stderr}".strip()
    print(f"CrossHair output:\n{combined}")
    errors = [line for line in combined.splitlines() if _CROSSHAIR_ERROR_RE.search(line)]
    assert not errors, "CrossHair counterexamples found:\n" + "\n".join(errors)
    if result.returncode == 2:
        pytest.fail(f"CrossHair internal error (exit 2):\n{combined}")
