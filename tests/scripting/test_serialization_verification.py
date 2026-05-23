# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Verification and contract tests for payload_codec split_grid serialization."""

from __future__ import annotations

import math
import shutil
import subprocess
import sys
from pathlib import Path
import pytest

from plugin.scripting import payload_codec
from plugin.scripting.payload_codec import (
    PAYLOAD_SPLIT_GRID,
    SPLIT_GRID_WIRE_DTYPE,
    host_pack_split_grid,
    host_unpack_split_grid,
    child_unpack_split_grid,
)

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

        # B. host_unpack_split_grid (equivalence/roundtrip contract)
        reconstructed = host_unpack_split_grid(envelope)
        assert isinstance(reconstructed, list)
        assert reconstructed == grid

        # C. child_unpack_split_grid
        np = pytest.importorskip("numpy")
        child_unpacked = child_unpack_split_grid(envelope)
        assert child_unpacked is not None


def test_crosshair_verification_if_available() -> None:
    """Run CrossHair concolic verification if the tool is installed in the environment."""
    # Find crosshair executable
    crosshair_path = shutil.which("crosshair")
    if not crosshair_path:
        # Check in .venv/bin/
        venv_bin_ch = Path(".venv/bin/crosshair")
        if venv_bin_ch.exists():
            crosshair_path = str(venv_bin_ch)

    if not crosshair_path:
        pytest.skip("CrossHair concolic execution engine is not installed.")

    # Run crosshair check on payload_codec.py
    result = subprocess.run(
        [
            crosshair_path,
            "check",
            "plugin/scripting/payload_codec.py",
            "--per_condition_timeout=5",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )

    print(f"CrossHair stdout:\n{result.stdout}")
    if result.stderr:
        print(f"CrossHair stderr:\n{result.stderr}")

    assert result.returncode == 0, f"CrossHair verification failed:\n{result.stdout}\n{result.stderr}"
