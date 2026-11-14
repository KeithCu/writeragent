# WriterAgent - venv_sandbox force keyword tests
# Copyright (c) 2026 KeithCu
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests verifying venv_sandbox.run_sandboxed_code preserves force='auto' by default."""

from __future__ import annotations

from plugin.scripting.payload_codec import is_split_grid
from tests.scripting.sandbox_support import sandbox_run
import pytest


def test_run_sandboxed_code_default_force_returns_split_grid() -> None:
    pytest.importorskip("numpy")
    code = "import numpy as np\nresult = np.arange(300).reshape(30, 10)"
    out = sandbox_run(code)
    assert out["status"] == "ok"
    assert is_split_grid(out["result"])


def test_run_sandboxed_code_force_never_returns_plain_lists() -> None:
    pytest.importorskip("numpy")
    code = "import numpy as np\nresult = np.arange(300).reshape(30, 10)"
    out = sandbox_run(code, force="never")
    assert out["status"] == "ok"
    assert not is_split_grid(out["result"])
    assert isinstance(out["result"], list)
