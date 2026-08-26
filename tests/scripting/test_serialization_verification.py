# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Verification and contract tests for payload_codec split_grid serialization.

Excluded from default ``make test`` (CrossHair can take minutes). Run: ``make slowtests``
(contracts + CrossHair here; extensive A/B Hypothesis in ``test_serialization_ab.py``).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import deal
import pytest

pytestmark = pytest.mark.slow

from hypothesis import given, strategies as st

from plugin.scripting.payload_codec import (
    BINARY_MIN_CELLS,
    PAYLOAD_MULTI_DATA,
    PAYLOAD_SPLIT_GRID,
    SPLIT_GRID_WIRE_DTYPE,
    _flatten_grid_to_components,
    child_unpack_split_grid,
    column_kinds_for_grid,
    host_pack_data,
    host_pack_multi_data,
    host_pack_split_grid,
    host_unpack_data,
    host_unpack_split_grid,
    is_multi_data,
    is_split_grid,
    should_use_binary_envelope,
)
from tests.scripting.serialization_ab_support import flatten_semantic_cells

# Test cases representing structural variety of inputs
VERIFICATION_GRIDS = [
    # 1. Empty grid
    [],
    # 2. 1D numeric list
    [1.5, 2.5, 3.5, 4.5, 5.5],
    # 3. 1D mixed list with string and None
    [1.5, "banana", None, 4.5],
    # 4. 2D rectangular numeric grid
    [[1.0, 2.0], [3.0, 4.0], [5.0, 6.0]],
    # 5. 2D mixed grid
    [
        [1.5, "apple", True],
        [2.5, "banana", False],
        [3.5, None, True],
    ],
]

CROSSHAIR_MODULE = "plugin/scripting/payload_codec.py"


def _find_crosshair() -> str | None:
    crosshair_path = shutil.which("crosshair")
    if crosshair_path:
        return crosshair_path
    venv_bin_ch = Path(".venv/bin/crosshair")
    if venv_bin_ch.exists():
        return str(venv_bin_ch)
    return None


@given(
    st.lists(
        st.one_of(
            st.none(),
            st.booleans(),
            st.integers(min_value=-1000, max_value=1000),
            st.floats(min_value=-1000.0, max_value=1000.0, allow_nan=False),
            st.text(max_size=20),
        ),
        min_size=0,
        max_size=30,
    )
)
def test_hypothesis_split_grid_roundtrip_invariant(grid: list) -> None:
    """Hypothesis property test proving host_unpack(host_pack(grid)) preserves flatten_semantic_cells."""
    envelope = host_pack_split_grid(grid)
    assert isinstance(envelope, dict)
    reconstructed = host_unpack_split_grid(envelope)
    assert flatten_semantic_cells(reconstructed) == flatten_semantic_cells(grid)



def test_serialization_contracts_runtime_and_invariants() -> None:
    """Verify that serialization contracts and invariants hold true for a variety of test grids."""
    for grid in VERIFICATION_GRIDS:
        # A. host_pack_split_grid
        envelope = host_pack_split_grid(grid)
        assert isinstance(envelope, dict)
        assert envelope.get("__wa_payload__") == PAYLOAD_SPLIT_GRID
        assert envelope.get("dtype") == SPLIT_GRID_WIRE_DTYPE
        assert isinstance(envelope.get("column_kinds"), list)
        assert isinstance(envelope.get("shape"), list)
        assert isinstance(envelope.get("strings"), dict)
        assert isinstance(envelope.get("buffer"), bytes)

        # B. host_unpack_split_grid (egress policy: buffer NaN preserved, not coerced to None)
        reconstructed = host_unpack_split_grid(envelope)
        assert isinstance(reconstructed, list)
        assert flatten_semantic_cells(reconstructed) == flatten_semantic_cells(grid)

        # C. child_unpack_split_grid (ingress: mixed grids restore None literally)
        pytest.importorskip("numpy")
        child_unpacked = child_unpack_split_grid(envelope)
        if envelope.get("strings"):
            assert child_unpacked == grid
        else:
            assert child_unpacked is not None


def test_jagged_grid_raises_value_error() -> None:
    """Jagged 2D grids must raise ValueError via @deal.raises on _flatten_grid_to_components."""
    jagged = [[1.0, 2.0], [3.0]]
    with pytest.raises(ValueError, match="Uneven row lengths"):
        _flatten_grid_to_components(jagged)


def test_column_kinds_for_grid_contract_kinds() -> None:
    """column_kinds_for_grid kinds are only int/float/bool (deal.ensure); empty/cover edges."""
    assert column_kinds_for_grid([]) == []
    assert column_kinds_for_grid([0]) == ["int"]
    kinds = column_kinds_for_grid([[1, "x", True], [2, "y", False]])
    assert kinds == ["int", "int", "bool"]
    assert all(k in ("int", "float", "bool") for k in kinds)


def test_should_use_binary_envelope_force_contracts() -> None:
    """force=always/never overrides cell threshold (deal.ensure); cover-style empty shape.

    Empty tuple must stay False under force=auto (len, not bool(shape) — CrossHair
    SymbolicBool TypeError on check-all deep 32900105768). (0,) is a non-empty shape.
    """
    assert should_use_binary_envelope((), force="always") is True
    assert should_use_binary_envelope((BINARY_MIN_CELLS,), force="never") is False
    assert should_use_binary_envelope((), min_cells=0, force="auto") is False
    assert should_use_binary_envelope((0,), min_cells=0, force="auto") is True


def test_host_pack_data_force_min_cells_preconditions() -> None:
    """Pack entrypoints share should_use_binary_envelope force/min_cells pre so CrossHair cannot invent bad kwargs."""
    from tests.strip_bundle import deal_pre_present

    if not deal_pre_present(host_pack_data):
        pytest.skip("@deal.pre stripped in release bundle; body does not re-check force/min_cells")
    with pytest.raises(deal.PreContractError):
        host_pack_data([[1.0]], min_cells=-1)
    with pytest.raises(deal.PreContractError):
        host_pack_data([[1.0]], force="")
    with pytest.raises(deal.PreContractError):
        host_pack_multi_data([[[1.0]]], force="nope")
    with pytest.raises(deal.PreContractError):
        host_pack_multi_data([[[1.0]]], min_cells=-5)


def test_host_unpack_data_plain_dict_recurses() -> None:
    """Plain dicts recurse; dict subclasses are left as-is (avoids CrossHair AttrDict __ch_pytype__)."""
    assert host_unpack_data({"a": 1, "b": [2, 3]}) == {"a": 1, "b": [2, 3]}

    class MappingLike(dict):
        pass

    nested = MappingLike(x=1)
    assert host_unpack_data(nested) is nested


def test_host_pack_multi_data_structure_and_roundtrip() -> None:
    """multi_data envelope: detector, items length, host_unpack_data semantic cells per item."""
    grids = [
        [],
        [1.5, 2.5],
        [[1.0, "x"], [None, True]],
    ]
    envelope = host_pack_multi_data(grids, force="always")
    assert is_multi_data(envelope) is True
    assert envelope.get("__wa_payload__") == PAYLOAD_MULTI_DATA
    items = envelope["items"]
    assert len(items) == len(grids)
    assert all(is_split_grid(item) for item in items)

    unpacked = host_unpack_data(envelope)
    assert isinstance(unpacked, list)
    assert len(unpacked) == len(grids)
    for got, src in zip(unpacked, grids, strict=True):
        assert flatten_semantic_cells(got) == flatten_semantic_cells(src)


def test_host_pack_multi_data_empty_list() -> None:
    envelope = host_pack_multi_data([])
    assert is_multi_data(envelope) is True
    assert envelope["items"] == []
    assert host_unpack_data(envelope) == []


_CROSSHAIR_ERROR_RE = re.compile(r": error:")

_CROSSHAIR_FQN_TARGETS = (
    "plugin.scripting.payload_codec.is_split_grid",
    "plugin.scripting.payload_codec.is_multi_data",
    "plugin.scripting.payload_codec.host_pack_multi_data",
)


@pytest.mark.slow
@pytest.mark.parametrize("target", _CROSSHAIR_FQN_TARGETS)
def test_crosshair_envelope_fqn_if_available(target: str) -> None:
    crosshair_path = _find_crosshair()
    if not crosshair_path:
        pytest.skip("CrossHair concolic execution engine is not installed.")
    result = subprocess.run(
        [crosshair_path, "check", "-v", "--report_all", target],
        capture_output=True,
        text=True,
        timeout=300,
    )
    combined = f"{result.stdout}\n{result.stderr}".strip()
    print(f"CrossHair output ({target}):\n{combined}")
    errors = [line for line in combined.splitlines() if _CROSSHAIR_ERROR_RE.search(line)]
    assert not errors, "CrossHair counterexamples found:\n" + "\n".join(errors)
    if result.returncode == 2:
        pytest.fail(f"CrossHair internal error (exit 2):\n{combined}")


def test_crosshair_verification_if_available() -> None:
    """Run CrossHair concolic verification if the tool is installed in the environment."""
    crosshair_path = _find_crosshair()
    if not crosshair_path:
        pytest.skip("CrossHair concolic execution engine is not installed.")

    result = subprocess.run(
        [
            crosshair_path,
            "check",
            "-v",
            "--report_all",
            CROSSHAIR_MODULE,
        ],
        capture_output=True,
        text=True,
        timeout=3600,
    )

    combined = f"{result.stdout}\n{result.stderr}".strip()
    print(f"CrossHair output:\n{combined}")

    errors = [line for line in combined.splitlines() if _CROSSHAIR_ERROR_RE.search(line)]
    assert not errors, "CrossHair counterexamples found:\n" + "\n".join(errors)

    if result.returncode == 2:
        pytest.fail(f"CrossHair internal error (exit 2):\n{combined}")
