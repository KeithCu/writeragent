# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for units chat tool."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from plugin.calc.units import UnitsTool
from plugin.tests.testing_utils import setup_uno_mocks

setup_uno_mocks()


@pytest.fixture
def writer_ctx():
    ctx = MagicMock()
    ctx.doc = MagicMock()
    ctx.ctx = MagicMock()
    ctx.doc_type = "writer"
    return ctx


@patch("plugin.calc.units.insert_units_result_into_doc")
@patch("plugin.framework.queue_executor.execute_on_main_thread")
@patch("plugin.scripting.units.run_trusted_units")
def test_units_happy_path(mock_run, mock_main_thread, mock_insert, writer_ctx):
    mock_run.return_value = {
        "status": "ok",
        "helper": "convert_quantity",
        "formatted": "36 kilometer / hour",
        "text": "36 kilometer / hour",
        "magnitude": 36.0,
    }
    mock_main_thread.side_effect = lambda fn, *args, **kwargs: fn(*args, **kwargs)

    tool = UnitsTool()
    result = tool.execute(
        writer_ctx,
        helper="convert_quantity",
        params={"value": "10", "from_unit": "m/s", "to_unit": "km/h"},
    )

    assert result["status"] == "ok"
    assert result.get("units_inserted") is True
    mock_run.assert_called_once()


def test_units_requires_helper(writer_ctx):
    tool = UnitsTool()
    result = tool.execute(writer_ctx)
    assert result["status"] == "error"


def test_units_in_python_domain():
    from plugin.main import get_tools

    with patch("plugin.framework.uno_context.get_desktop", return_value=None):
        registry = get_tools()
    doc = MagicMock()
    doc.supportsService.return_value = True
    names = {t.name for t in registry.get_tools(doc=doc, active_domain="python", exclude_tiers=())}
    assert "units" in names
