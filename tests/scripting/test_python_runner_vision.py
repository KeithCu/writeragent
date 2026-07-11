# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for Run Python Script vision venv path."""

from __future__ import annotations

import builtins
import sys
from unittest.mock import MagicMock, patch

from plugin.scripting.python_runner import execute_and_insert_result
from plugin.vision.vision_templates import get_vision_script_templates
from plugin.tests.testing_utils import setup_uno_mocks

setup_uno_mocks()


def _vision_params_for(helper: str) -> dict:
    from plugin.scripting.helper_domain import parse_run_import_call_spec

    call_spec = parse_run_import_call_spec(get_vision_script_templates()[helper], run_name="run_vision") or {}
    return call_spec.get("params") if isinstance(call_spec.get("params"), dict) else {}


@patch("plugin.vision.vision_egress.insert_vision_result")
@patch("plugin.scripting.python_runner.run_code_in_user_venv")
@patch("plugin.vision.vision_runner.resolve_vision_image_bytes")
def test_execute_and_insert_vision_venv_path(mock_resolve, mock_venv, mock_insert):
    ctx = MagicMock()
    doc = MagicMock()

    with patch("plugin.scripting.python_runner.is_writer", return_value=True), patch(
        "plugin.scripting.python_runner.is_calc", return_value=False
    ), patch("plugin.vision.vision_runner.supports_vision_manual", return_value=True):
        mock_resolve.return_value = b"png-bytes"
        mock_venv.return_value = {
            "status": "ok",
            "result": {
                "status": "ok",
                "helper": "extract_text",
                "html": "<p>line1</p><p>line2</p>",
                "metrics": {"line_count": 2},
            },
        }
        code = get_vision_script_templates()["extract_text"]
        outcome = execute_and_insert_result(ctx, doc, code)

    assert outcome["ok"] is True
    assert "extract_text" in outcome["status_ok_text"]
    assert "HTML" in outcome["status_ok_text"]
    mock_resolve.assert_called_once()
    mock_venv.assert_called_once()
    assert mock_venv.call_args.kwargs["bindings"] == {"image": b"png-bytes"}
    mock_insert.assert_called_once()
    assert mock_insert.call_args.kwargs["params"] is not None


@patch("plugin.vision.vision_egress.insert_vision_result")
@patch("plugin.scripting.python_runner.run_code_in_user_venv")
@patch("plugin.vision.vision_runner.resolve_vision_image_bytes")
def test_execute_and_insert_vision_venv_path_calc(mock_resolve, mock_venv, mock_insert):
    ctx = MagicMock()
    doc = MagicMock()

    with patch("plugin.scripting.python_runner.is_writer", return_value=False), patch(
        "plugin.scripting.python_runner.is_calc", return_value=True
    ), patch("plugin.vision.vision_runner.supports_vision_manual", return_value=True):
        mock_resolve.return_value = b"png-bytes"
        mock_venv.return_value = {
            "status": "ok",
            "result": {
                "status": "ok",
                "helper": "extract_text",
                "html": "<p>line1</p><p>line2</p>",
                "metrics": {"line_count": 2},
            },
        }
        code = get_vision_script_templates()["extract_text"]
        outcome = execute_and_insert_result(ctx, doc, code)

    assert outcome["ok"] is True
    assert "extract_text" in outcome["status_ok_text"]
    mock_resolve.assert_called_once()
    mock_venv.assert_called_once()
    mock_insert.assert_called_once()


@patch("plugin.vision.vision_egress.insert_vision_result")
@patch("plugin.scripting.python_runner.run_code_in_user_venv")
@patch("plugin.vision.vision_runner.resolve_vision_image_bytes")
def test_execute_and_insert_vision_structure_writer(mock_resolve, mock_venv, mock_insert):
    ctx = MagicMock()
    doc = MagicMock()

    with patch("plugin.scripting.python_runner.is_writer", return_value=True), patch(
        "plugin.scripting.python_runner.is_calc", return_value=False
    ), patch("plugin.vision.vision_runner.supports_vision_manual", return_value=True):
        mock_resolve.return_value = b"png-bytes"
        mock_venv.return_value = {
            "status": "ok",
            "result": {
                "status": "ok",
                "helper": "extract_structure",
                "html": "<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>",
                "metrics": {"block_count": 2, "table_count": 1},
                "tables": [{"name": "table_1", "columns": ["A", "B"], "rows": [["1", "2"]]}],
                "blocks": [],
                "warnings": [],
            },
        }
        code = get_vision_script_templates()["extract_structure"]
        outcome = execute_and_insert_result(ctx, doc, code)

    assert outcome["ok"] is True
    assert "extract_structure" in outcome["status_ok_text"]
    assert "HTML" in outcome["status_ok_text"]
    mock_insert.assert_called_once()
    assert mock_insert.call_args.kwargs["params"] is not None


@patch("plugin.vision.vision_egress.insert_vision_result")
@patch("plugin.scripting.python_runner.run_code_in_user_venv")
@patch("plugin.vision.vision_runner.resolve_vision_image_bytes")
def test_execute_and_insert_vision_structure_calc(mock_resolve, mock_venv, mock_insert):
    ctx = MagicMock()
    doc = MagicMock()

    with patch("plugin.scripting.python_runner.is_writer", return_value=False), patch(
        "plugin.scripting.python_runner.is_calc", return_value=True
    ), patch("plugin.vision.vision_runner.supports_vision_manual", return_value=True):
        mock_resolve.return_value = b"png-bytes"
        mock_venv.return_value = {
            "status": "ok",
            "result": {
                "status": "ok",
                "helper": "extract_structure",
                "html": "<table><tr><td>1</td></tr></table>",
                "metrics": {"block_count": 0, "table_count": 1},
                "tables": [{"name": "table_1", "columns": ["A"], "rows": [["1"]]}],
                "blocks": [],
                "warnings": [],
            },
        }
        code = get_vision_script_templates()["extract_structure"]
        outcome = execute_and_insert_result(ctx, doc, code)

    assert outcome["ok"] is True
    mock_resolve.assert_called_once()
    mock_venv.assert_called_once()
    params = mock_insert.call_args.kwargs["params"]
    assert params.get("image_name") == ""


@patch("plugin.vision.vision_runner.resolve_vision_image_bytes")
def test_execute_and_insert_vision_forwards_image_name(mock_resolve):
    ctx = MagicMock()
    doc = MagicMock()

    with patch("plugin.scripting.python_runner.is_writer", return_value=True), patch(
        "plugin.vision.vision_runner.supports_vision_manual", return_value=True
    ), patch("plugin.scripting.python_runner.run_code_in_user_venv", return_value={"status": "ok", "result": None}), patch(
        "plugin.vision.vision_egress.insert_vision_result"
    ):
        mock_resolve.return_value = b"png-bytes"
        code = (
            'from writeragent.vision.venv.vision import run_vision\n'
            'result = run_vision({"helper": "extract_text", "params": {"image_name": "Photo1"}}, image, {})\n'
        )
        outcome = execute_and_insert_result(ctx, doc, code)

    assert outcome["ok"] is True
    mock_resolve.assert_called_once()
    assert mock_resolve.call_args.kwargs["image_name"] == "Photo1"


@patch("plugin.scripting.python_runner.run_code_in_user_venv")
def test_execute_and_insert_vision_rejects_unsupported_doc(mock_venv):
    ctx = MagicMock()
    doc = MagicMock()
    code = get_vision_script_templates()["extract_text"]

    with patch("plugin.vision.vision_runner.supports_vision_manual", return_value=False):
        outcome = execute_and_insert_result(ctx, doc, code)

    assert outcome["ok"] is False
    assert "Writer or Calc" in outcome["message"]
    mock_venv.assert_not_called()


@patch("plugin.vision.vision_runner.resolve_vision_image_bytes")
def test_execute_and_insert_vision_surfaces_no_image_selected(mock_resolve):
    from plugin.framework.errors import ToolExecutionError

    ctx = MagicMock()
    doc = MagicMock()
    code = get_vision_script_templates()["extract_text"]
    mock_resolve.side_effect = ToolExecutionError("Select an embedded image, then Run again.", code="NO_IMAGE_SELECTED")

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
@patch("plugin.scripting.python_runner.run_code_in_user_venv")
@patch("plugin.vision.vision_runner.resolve_vision_image_bytes")
def test_execute_and_insert_vision_venv_path_without_prompts_module(mock_resolve, mock_venv, mock_insert):
    """Run Python Script vision path must not require framework.prompts (LibrePy)."""
    sys.modules.pop("plugin.framework.prompts", None)
    ctx = MagicMock()
    doc = MagicMock()

    with patch("builtins.__import__", side_effect=_import_without_prompts):
        with patch("plugin.scripting.python_runner.is_writer", return_value=True), patch(
            "plugin.scripting.python_runner.is_calc", return_value=False
        ), patch("plugin.vision.vision_runner.supports_vision_manual", return_value=True):
            mock_resolve.return_value = b"png-bytes"
            mock_venv.return_value = {
                "status": "ok",
                "result": {
                    "status": "ok",
                    "helper": "extract_text",
                    "html": "<p>line1</p>",
                    "metrics": {"line_count": 1},
                },
            }
            code = get_vision_script_templates()["extract_text"]
            outcome = execute_and_insert_result(ctx, doc, code)

    assert outcome["ok"] is True
    mock_resolve.assert_called_once()
    mock_venv.assert_called_once()
