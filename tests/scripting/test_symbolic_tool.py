# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for symbolic_math chat tool."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from plugin.calc.symbolic_math import SymbolicMathTool
from plugin.tests.testing_utils import setup_uno_mocks

setup_uno_mocks()


@pytest.fixture
def writer_ctx():
    ctx = MagicMock()
    ctx.doc = MagicMock()
    ctx.ctx = MagicMock()
    ctx.doc_type = "writer"
    return ctx


@patch("plugin.calc.symbolic_math.insert_symbolic_result_into_doc")
@patch("plugin.framework.queue_executor.execute_on_main_thread")
@patch("plugin.scripting.symbolic_runner.run_trusted_symbolic")
def test_symbolic_math_happy_path(mock_run, mock_main_thread, mock_insert, writer_ctx):
    mock_run.return_value = {
        "status": "ok",
        "helper": "symbolic_simplify",
        "latex": "2",
        "text": "2",
    }
    mock_main_thread.side_effect = lambda fn, *args, **kwargs: fn(*args, **kwargs)

    tool = SymbolicMathTool()
    result = tool.execute(writer_ctx, helper="symbolic_simplify", params={"expression": "(x+1)**2-x**2-2*x"})

    assert result["status"] == "ok"
    assert result.get("math_inserted") is True
    mock_run.assert_called_once()


def test_symbolic_math_requires_helper(writer_ctx):
    tool = SymbolicMathTool()
    result = tool.execute(writer_ctx)
    assert result["status"] == "error"


def test_symbolic_math_in_python_domain():
    from plugin.main import get_tools

    registry = get_tools()
    doc = MagicMock()
    doc.supportsService.return_value = True
    names = {t.name for t in registry.get_tools(doc=doc, active_domain="python", exclude_tiers=())}
    assert "symbolic_math" in names
    assert "run_venv_python_script" in names
