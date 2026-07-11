# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for Run Python Script vision fast path."""

from __future__ import annotations

import builtins
import sys
from unittest.mock import MagicMock, patch

from plugin.scripting.python_runner import execute_and_insert_result
from plugin.vision.vision_templates import get_vision_script_templates, parse_vision_script_header
from plugin.tests.testing_utils import setup_uno_mocks

setup_uno_mocks()


def _vision_params_for(helper: str) -> dict:
    meta = parse_vision_script_header(get_vision_script_templates()[helper])
    assert meta is not None
    return meta.params


@patch("plugin.vision.vision_egress.insert_vision_result")
@patch("plugin.vision.vision_runner.run_trusted_vision")
def test_execute_and_insert_vision_fast_path(mock_run, mock_insert):
    ctx = MagicMock()
    doc = MagicMock()

    with patch("plugin.scripting.python_runner.is_writer", return_value=True), patch(
        "plugin.scripting.python_runner.is_calc", return_value=False
    ), patch("plugin.vision.vision_runner.supports_vision_manual", return_value=True):
        mock_run.return_value = {
            "status": "ok",
            "helper": "extract_text",
            "html": "<p>line1</p><p>line2</p>",
            "metrics": {"line_count": 2},
        }
        code = get_vision_script_templates()["extract_text"]
        outcome = execute_and_insert_result(ctx, doc, code)

    assert outcome["ok"] is True
    assert "extract_text" in outcome["status_ok_text"]
    assert "HTML" in outcome["status_ok_text"]
    mock_run.assert_called_once()
    mock_insert.assert_called_once_with(ctx, doc, mock_run.return_value, params=_vision_params_for("extract_text"))


@patch("plugin.vision.vision_egress.insert_vision_result")
@patch("plugin.vision.vision_runner.run_trusted_vision")
def test_execute_and_insert_vision_fast_path_calc(mock_run, mock_insert):
    ctx = MagicMock()
    doc = MagicMock()

    with patch("plugin.scripting.python_runner.is_writer", return_value=False), patch(
        "plugin.scripting.python_runner.is_calc", return_value=True
    ), patch("plugin.vision.vision_runner.supports_vision_manual", return_value=True):
        mock_run.return_value = {
            "status": "ok",
            "helper": "extract_text",
            "html": "<p>line1</p><p>line2</p>",
            "metrics": {"line_count": 2},
        }
        code = get_vision_script_templates()["extract_text"]
        outcome = execute_and_insert_result(ctx, doc, code)

    assert outcome["ok"] is True
    assert "extract_text" in outcome["status_ok_text"]
    mock_run.assert_called_once()
    mock_insert.assert_called_once_with(ctx, doc, mock_run.return_value, params=_vision_params_for("extract_text"))


@patch("plugin.vision.vision_egress.insert_vision_result")
@patch("plugin.vision.vision_runner.run_trusted_vision")
def test_execute_and_insert_vision_structure_writer(mock_run, mock_insert):
    ctx = MagicMock()
    doc = MagicMock()

    with patch("plugin.scripting.python_runner.is_writer", return_value=True), patch(
        "plugin.scripting.python_runner.is_calc", return_value=False
    ), patch("plugin.vision.vision_runner.supports_vision_manual", return_value=True):
        mock_run.return_value = {
            "status": "ok",
            "helper": "extract_structure",
            "html": "<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>",
            "metrics": {"block_count": 2, "table_count": 1},
            "tables": [{"name": "table_1", "columns": ["A", "B"], "rows": [["1", "2"]]}],
            "blocks": [],
            "warnings": [],
        }
        code = get_vision_script_templates()["extract_structure"]
        outcome = execute_and_insert_result(ctx, doc, code)

    assert outcome["ok"] is True
    assert "extract_structure" in outcome["status_ok_text"]
    assert "HTML" in outcome["status_ok_text"]
    mock_insert.assert_called_once_with(ctx, doc, mock_run.return_value, params=_vision_params_for("extract_structure"))


@patch("plugin.vision.vision_egress.insert_vision_result")
@patch("plugin.vision.vision_runner.run_trusted_vision")
def test_execute_and_insert_vision_structure_calc(mock_run, mock_insert):
    ctx = MagicMock()
    doc = MagicMock()

    with patch("plugin.scripting.python_runner.is_writer", return_value=False), patch(
        "plugin.scripting.python_runner.is_calc", return_value=True
    ), patch("plugin.vision.vision_runner.supports_vision_manual", return_value=True):
        mock_run.return_value = {
            "status": "ok",
            "helper": "extract_structure",
            "html": "<table><tr><td>1</td></tr></table>",
            "metrics": {"block_count": 0, "table_count": 1},
            "tables": [{"name": "table_1", "columns": ["A"], "rows": [["1"]]}],
            "blocks": [],
            "warnings": [],
        }
        code = get_vision_script_templates()["extract_structure"]
        outcome = execute_and_insert_result(ctx, doc, code)

    assert outcome["ok"] is True
    mock_run.assert_called_once()
    call_kwargs = mock_run.call_args.kwargs
    assert call_kwargs["params"].get("image_name") == ""


@patch("plugin.vision.vision_runner.run_trusted_vision")
def test_execute_and_insert_vision_forwards_image_name(mock_run):
    ctx = MagicMock()
    doc = MagicMock()

    with patch("plugin.scripting.python_runner.is_writer", return_value=True), patch(
        "plugin.vision.vision_runner.supports_vision_manual", return_value=True
    ), patch("plugin.vision.vision_egress.insert_vision_result"):
        mock_run.return_value = {
            "status": "ok",
            "helper": "extract_text",
            "html": "<p>hi</p>",
            "metrics": {"line_count": 1},
        }
        code = '# writeragent:vision helper=extract_text params={"lang":"en","image_name":"Photo1"}\n'
        outcome = execute_and_insert_result(ctx, doc, code)

    assert outcome["ok"] is True
    mock_run.assert_called_once()
    assert mock_run.call_args.kwargs["params"]["image_name"] == "Photo1"


@patch("plugin.vision.vision_runner.run_trusted_vision")
def test_execute_and_insert_vision_rejects_unsupported_doc(mock_run):
    ctx = MagicMock()
    doc = MagicMock()
    code = get_vision_script_templates()["extract_text"]

    with patch("plugin.vision.vision_runner.supports_vision_manual", return_value=False):
        outcome = execute_and_insert_result(ctx, doc, code)

    assert outcome["ok"] is False
    assert "Writer or Calc" in outcome["message"]
    mock_run.assert_not_called()


@patch("plugin.vision.vision_runner.run_trusted_vision")
def test_execute_and_insert_vision_surfaces_no_image_selected(mock_run):
    from plugin.framework.errors import ToolExecutionError

    ctx = MagicMock()
    doc = MagicMock()
    code = get_vision_script_templates()["extract_text"]
    mock_run.side_effect = ToolExecutionError("Select an embedded image, then Run again.", code="NO_IMAGE_SELECTED")

    with patch("plugin.scripting.python_runner.is_writer", return_value=True), patch(
        "plugin.vision.vision_runner.supports_vision_manual", return_value=True
    ):
        outcome = execute_and_insert_result(ctx, doc, code)

    assert outcome["ok"] is False
    assert "embedded image" in outcome["message"]


def _import_without_prompts(name, globals=None, locals=None, fromlist=(), level=0):
    """Block ``plugin.framework.prompts`` to simulate a LibrePy install."""
    if name == "plugin.framework.prompts" or name.startswith("plugin.framework.prompts."):
        raise ImportError(f"No module named {name!r}")
    if fromlist:
        for item in fromlist:
            full = f"{name}.{item}" if name else item
            if full == "plugin.framework.prompts" or full.startswith("plugin.framework.prompts."):
                raise ImportError(f"No module named {full!r}")
    return _REAL_IMPORT(name, globals, locals, fromlist, level)


_REAL_IMPORT = builtins.__import__


@patch("plugin.vision.vision_egress.insert_vision_result")
@patch("plugin.vision.vision_runner.run_trusted_vision")
def test_execute_and_insert_vision_fast_path_without_prompts_module(mock_run, mock_insert):
    """Run Python Script vision path must not require framework.prompts (LibrePy)."""
    sys.modules.pop("plugin.framework.prompts", None)
    ctx = MagicMock()
    doc = MagicMock()

    with patch("builtins.__import__", side_effect=_import_without_prompts):
        with patch("plugin.scripting.python_runner.is_writer", return_value=True), patch(
            "plugin.scripting.python_runner.is_calc", return_value=False
        ), patch("plugin.vision.vision_runner.supports_vision_manual", return_value=True):
            mock_run.return_value = {
                "status": "ok",
                "helper": "extract_text",
                "html": "<p>line1</p>",
                "metrics": {"line_count": 1},
            }
            code = get_vision_script_templates()["extract_text"]
            outcome = execute_and_insert_result(ctx, doc, code)

    assert outcome["ok"] is True
    mock_run.assert_called_once()
