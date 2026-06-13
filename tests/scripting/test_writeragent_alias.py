# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the writeragent to plugin import redirection alias."""

from __future__ import annotations

import sys
import pytest
from unittest.mock import MagicMock, patch

from plugin.framework.uno_bootstrap import register_alias_importer


def test_alias_importer_redirects_writeragent():
    register_alias_importer()

    # Import top-level writeragent (should map to plugin)
    import writeragent
    import plugin
    assert writeragent is plugin

    # Import submodules
    from writeragent.scripting.viz import run_viz
    from plugin.scripting.viz import run_viz as real_run_viz
    assert run_viz is real_run_viz

    # Check sys.modules populated correctly
    assert "writeragent.scripting.viz" in sys.modules
    m1 = sys.modules["writeragent.scripting.viz"]
    m2 = sys.modules["plugin.scripting.viz"]
    assert m1 is m2 or m1.__file__ == m2.__file__


def test_sandboxed_code_resolves_writeragent_imports():
    from plugin.scripting.venv.venv_sandbox import run_sandboxed_code

    code = (
        "from writeragent.scripting.analysis import coerce_to_dataframe\n"
        "result = coerce_to_dataframe"
    )
    # run_sandboxed_code executes within LocalPythonExecutor.
    # We pass data=None and execute.
    response = run_sandboxed_code(code)
    assert response["status"] == "ok"
    assert response["result"] is not None
