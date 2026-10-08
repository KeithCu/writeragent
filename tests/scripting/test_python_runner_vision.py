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


def _vision_params_for(helper: str) -> dict:
    from plugin.scripting.helper_domain import parse_run_import_call_spec

    call_spec = parse_run_import_call_spec(get_vision_script_templates()[helper], run_name="run_vision") or {}
    return call_spec.get("params") if isinstance(call_spec.get("params"), dict) else {}


@patch("plugin.vision.vision_egress.insert_vision_result")
@patch("plugin.vision.vision_runner.run_and_insert_vision_for_selection")
def test_execute_and_insert_vision_venv_path(mock_orchestrator, mock_insert):
    ctx = MagicMock()
    doc = MagicMock()

    with patch("plugin.scripting.python_runner.is_writer", return_value=True), patch(
        "plugin.scripting.python_runner.is_calc", return_value=False
    ), patch("plugin.vision.vision_runner.supports_vision_manual", return_value=True):
        mock_orchestrator.return_value = {"status": "ok", "images_processed": 1, "helper": "extract_text", "individual_results": [{"status": "ok", "helper": "extract_text", "html": "<p>line1</p><p>line2</p>", "metrics": {"line_count": 2}}]}
        code = get_vision_script_templates()["extract_text"]
        outcome = execute_and_insert_result(ctx, doc, code)

    assert outcome["ok"] is True
    assert "extract_text" in outcome["status_ok_text"]
    assert "HTML" in outcome["status_ok_text"]
    mock_orchestrator.assert_called_once()
    mock_insert.assert_called_once()
    assert mock_insert.call_args.kwargs["params"] is not None


@patch("plugin.vision.vision_egress.insert_vision_result")
@patch("plugin.vision.vision_runner.run_and_insert_vision_for_selection")
def test_execute_and_insert_vision_venv_path_calc(mock_orchestrator, mock_insert):
    ctx = MagicMock()
    doc = MagicMock()

    with patch("plugin.scripting.python_runner.is_writer", return_value=False), patch(
        "plugin.scripting.python_runner.is_calc", return_value=True
    ), patch("plugin.vision.vision_runner.supports_vision_manual", return_value=True):
        mock_orchestrator.return_value = {"status": "ok", "images_processed": 1, "helper": "extract_text", "individual_results": [{"status": "ok", "helper": "extract_text", "html": "<p>line1</p><p>line2</p>", "metrics": {"line_count": 2}}]}
        code = get_vision_script_templates()["extract_text"]
        outcome = execute_and_insert_result(ctx, doc, code)

    assert outcome["ok"] is True
    assert "extract_text" in outcome["status_ok_text"]
    mock_orchestrator.assert_called_once()
    mock_insert.assert_called_once()


@patch("plugin.vision.vision_egress.insert_vision_result")
@patch("plugin.vision.vision_runner.run_and_insert_vision_for_selection")
def test_execute_and_insert_vision_structure_writer(mock_orchestrator, mock_insert):
    ctx = MagicMock()
    doc = MagicMock()

    with patch("plugin.scripting.python_runner.is_writer", return_value=True), patch(
        "plugin.scripting.python_runner.is_calc", return_value=False
    ), patch("plugin.vision.vision_runner.supports_vision_manual", return_value=True):
        mock_orchestrator.return_value = {"status": "ok", "images_processed": 1, "helper": "extract_structure", "individual_results": [{"status": "ok", "helper": "extract_structure", "html": "<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>", "metrics": {"block_count": 2, "table_count": 1}, "tables": [{"name": "table_1", "columns": ["A", "B"], "rows": [["1", "2"]]}], "blocks": [], "warnings": []}]}
        code = get_vision_script_templates()["extract_structure"]
        outcome = execute_and_insert_result(ctx, doc, code)

    assert outcome["ok"] is True
    assert "extract_structure" in outcome["status_ok_text"]
    assert "HTML" in outcome["status_ok_text"]
    mock_insert.assert_called_once()
    assert mock_insert.call_args.kwargs["params"] is not None


@patch("plugin.vision.vision_egress.insert_vision_result")
@patch("plugin.vision.vision_runner.run_and_insert_vision_for_selection")
def test_execute_and_insert_vision_structure_calc(mock_orchestrator, mock_insert):
    ctx = MagicMock()
    doc = MagicMock()

    with patch("plugin.scripting.python_runner.is_writer", return_value=False), patch(
        "plugin.scripting.python_runner.is_calc", return_value=True
    ), patch("plugin.vision.vision_runner.supports_vision_manual", return_value=True):
        mock_orchestrator.return_value = {"status": "ok", "images_processed": 1, "helper": "extract_structure", "individual_results": [{"status": "ok", "helper": "extract_structure", "html": "<table><tr><td>1</td></tr></table>", "metrics": {"block_count": 0, "table_count": 1}, "tables": [{"name": "table_1", "columns": ["A"], "rows": [["1"]]}], "blocks": [], "warnings": []}]}
        code = get_vision_script_templates()["extract_structure"]
        outcome = execute_and_insert_result(ctx, doc, code)

    assert outcome["ok"] is True
    mock_orchestrator.assert_called_once()
    params = mock_insert.call_args.kwargs["params"]
    assert params.get("image_name") is None


@patch("plugin.vision.vision_runner.run_and_insert_vision_for_selection")
def test_execute_and_insert_vision_forwards_image_name(mock_orchestrator):
    ctx = MagicMock()
    doc = MagicMock()

    with patch("plugin.scripting.python_runner.is_writer", return_value=True), patch(
        "plugin.vision.vision_runner.supports_vision_manual", return_value=True
    ), patch("plugin.scripting.python_runner.run_code_in_user_venv", return_value={"status": "ok", "result": None}), patch(
        "plugin.vision.vision_egress.insert_vision_result"
    ):
        mock_orchestrator.return_value = {"status": "ok", "images_processed": 1, "helper": "extract_text", "individual_results": [{"status": "ok", "helper": "extract_text", "html": "<p>line1</p>", "metrics": {"line_count": 1}}]}
        code = (
            'from writeragent.vision import run_vision\n'
            'result = run_vision({"helper": "extract_text", "params": {"image_name": "Photo1"}}, image, {})\n'
        )
        outcome = execute_and_insert_result(ctx, doc, code)

    assert outcome["ok"] is True
    mock_orchestrator.assert_called_once()
    assert mock_orchestrator.call_args.kwargs["params"]["image_name"] == "Photo1"


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
@patch("plugin.vision.vision_runner.run_and_insert_vision_for_selection")
def test_execute_and_insert_vision_venv_path_without_prompts_module(mock_orchestrator, mock_insert):
    """Run Python Script vision path must not require framework.prompts (LibrePy)."""
    sys.modules.pop("plugin.framework.prompts", None)
    ctx = MagicMock()
    doc = MagicMock()

    with patch("builtins.__import__", side_effect=_import_without_prompts):
        with patch("plugin.scripting.python_runner.is_writer", return_value=True), patch(
            "plugin.scripting.python_runner.is_calc", return_value=False
        ), patch("plugin.vision.vision_runner.supports_vision_manual", return_value=True):
            mock_orchestrator.return_value = {"status": "ok", "images_processed": 1, "helper": "extract_text", "individual_results": [{"status": "ok", "helper": "extract_text", "html": "<p>line1</p>", "metrics": {"line_count": 1}}]}
            code = get_vision_script_templates()["extract_text"]
            outcome = execute_and_insert_result(ctx, doc, code)

    assert outcome["ok"] is True
    mock_orchestrator.assert_called_once()


@patch("plugin.vision.vision_runner.run_and_insert_vision_for_selection")
@patch("plugin.doc.visual_helpers.graphic_objects_in_selection")
def test_execute_and_insert_vision_multi_image_host_loop(mock_pairs, mock_orchestrator):
    ctx = MagicMock()
    doc = MagicMock()
    mock_pairs.return_value = [("A", MagicMock()), ("B", MagicMock())]
    mock_orchestrator.return_value = {
        "status": "ok",
        "helper": "extract_text",
        "full_text": "a\n\nb",
        "images_processed": 2,
        "inserted": True,
    }
    code = get_vision_script_templates()["extract_text"]

    with patch("plugin.scripting.python_runner.is_writer", return_value=True), patch(
        "plugin.scripting.python_runner.is_calc", return_value=False
    ), patch("plugin.vision.vision_runner.supports_vision_manual", return_value=True), patch(
        "plugin.scripting.python_runner.run_code_in_user_venv"
    ), patch("plugin.vision.vision_runner.resolve_vision_image_bytes") as mock_resolve, patch(
        "plugin.scripting.client.run_vision"
    ) as mock_run_vision:
        mock_resolve.return_value = b"png"
        mock_run_vision.return_value = {
            "status": "ok",
            "html": "<p>mock</p>",
            "full_text": "mock",
            "context": {"image_name": "A"}
        }

        with patch("plugin.vision.vision_egress.insert_vision_result"):
            outcome = execute_and_insert_result(ctx, doc, code)

    assert outcome["ok"] is True
    assert "2 images" in outcome["status_ok_text"]
    mock_orchestrator.assert_called_once()
    mock_run_vision.assert_not_called()


@patch("plugin.vision.vision_runner.run_and_insert_vision_for_selection")
@patch("plugin.doc.visual_helpers.graphic_objects_in_selection")
def test_execute_and_insert_vision_single_image_in_text_range_uses_host_loop(mock_pairs, mock_orchestrator):
    """Text range with one image: selection export fails; host orchestrator must run."""
    ctx = MagicMock()
    doc = MagicMock()
    mock_pairs.return_value = [("OnlyImg", MagicMock())]
    mock_orchestrator.return_value = {
        "status": "ok",
        "helper": "extract_text",
        "full_text": "one",
        "images_processed": 1,
        "inserted": True,
    }
    code = get_vision_script_templates()["extract_text"]

    with patch("plugin.scripting.python_runner.is_writer", return_value=True), patch(
        "plugin.scripting.python_runner.is_calc", return_value=False
    ), patch("plugin.vision.vision_runner.supports_vision_manual", return_value=True), patch(
        "plugin.scripting.python_runner.run_code_in_user_venv"
    ), patch("plugin.vision.vision_runner.resolve_vision_image_bytes") as mock_resolve, patch(
        "plugin.scripting.client.run_vision"
    ) as mock_run_vision:
        mock_resolve.return_value = b"png"
        mock_run_vision.return_value = {
            "status": "ok",
            "html": "<p>mock</p>",
            "full_text": "mock",
            "context": {"image_name": "OnlyImg"}
        }

        with patch("plugin.vision.vision_egress.insert_vision_result"):
            outcome = execute_and_insert_result(ctx, doc, code)

    assert outcome["ok"] is True
    assert "Inserted formatted HTML" in outcome["status_ok_text"]
    assert "2 images" not in outcome["status_ok_text"]
    mock_orchestrator.assert_called_once()
    mock_run_vision.assert_not_called()


def test_execute_and_insert_vision_partial_failure_inserts_completed_results():
    """Bug 1: When image N fails in a multi-image run, images 1..N-1 are inserted before error is returned."""
    ctx = MagicMock()
    doc = MagicMock()
    code = get_vision_script_templates()["extract_text"]

    partial_error = {
        "status": "error",
        "message": "Image 2 OCR failed",
        "failed_image": "Img2",
        "images_processed": 1,
        "partial": True,
        "inserted": False,
        "individual_results": [
            {
                "status": "ok",
                "helper": "extract_text",
                "html": "<p>Image 1 text</p>",
                "image_name": "Img1",
                "context": {"image_name": "Img1"},
            }
        ],
    }

    with (
        patch("plugin.scripting.python_runner.is_writer", return_value=True),
        patch("plugin.scripting.python_runner.is_calc", return_value=False),
        patch("plugin.vision.vision_runner.supports_vision_manual", return_value=True),
        patch("plugin.vision.vision_runner.run_and_insert_vision_for_selection", return_value=partial_error),
        patch("plugin.vision.vision_egress.insert_vision_result") as mock_insert,
    ):
        outcome = execute_and_insert_result(ctx, doc, code)

    assert outcome["ok"] is False
    assert "Image 2 OCR failed" in outcome["message"]
    # Image 1 result must have been inserted before returning the error
    mock_insert.assert_called_once()
    assert mock_insert.call_args.args[2]["image_name"] == "Img1"


def test_prepare_rps_execution_vision_runs_before_calc_data():
    """Bug 2: Vision branch runs before Calc py_data resolution to avoid erroring on graphic selections."""
    ctx = MagicMock()
    doc = MagicMock()
    code = get_vision_script_templates()["extract_text"]

    with (
        patch("plugin.scripting.python_runner.is_calc", return_value=True),
        patch("plugin.vision.vision_runner.supports_vision_manual", return_value=True),
        patch("plugin.calc.calc_addin_data._resolve_python_data", side_effect=RuntimeError("selection is not a range")) as mock_calc_data,
        patch("plugin.vision.vision_runner.run_and_insert_vision_for_selection", return_value={"status": "ok", "individual_results": []}),
    ):
        from plugin.scripting.python_runner import _prepare_rps_execution

        prepared = _prepare_rps_execution(ctx, doc, code)

    assert prepared.get("early_outcome") is None
    assert prepared.get("is_vision_selection") is True
    # Calc data resolution must not have run for vision scripts
    mock_calc_data.assert_not_called()

