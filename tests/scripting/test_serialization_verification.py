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

import pytest

pytestmark = pytest.mark.slow

from plugin.scripting.payload_codec import (
    PAYLOAD_SPLIT_GRID,
    SPLIT_GRID_WIRE_DTYPE,
    _flatten_grid_to_components,
    child_unpack_split_grid,
    host_pack_split_grid,
    host_unpack_split_grid,
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


_CROSSHAIR_ERROR_RE = re.compile(r": error:")


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
    assert not errors, f"CrossHair counterexamples found:\n" + "\n".join(errors)

    if result.returncode == 2:
        pytest.fail(f"CrossHair internal error (exit 2):\n{combined}")
