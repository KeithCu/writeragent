# WriterAgent - tests for notebook cell execution

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from plugin.notebook.cell_registry import NotebookCodeCell, NotebookDocState, cell_id_to_hex, new_code_cell_entry
from plugin.notebook.notebook_runner import (
    execute_code,
    format_run_output_text,
    init_registry_execution_counter,
    read_code_from_field,
    run_cell,
    run_cell_target_url,
)
from plugin.tests.testing_utils import setup_uno_mocks

setup_uno_mocks()


def test_format_run_output_text_stdout_and_result():
    text = format_run_output_text({"status": "ok", "stdout": "hi\n", "result": 42})
    assert "hi" in text
    assert "42" in text


def test_format_run_output_text_error_traceback():
    text = format_run_output_text(
        {"status": "error", "traceback": "\x1b[31mValueError\x1b[0m: bad", "stdout": ""}
    )
    assert "ValueError" in text
    assert "\x1b" not in text


def test_format_run_output_text_skips_image_result():
    wire = {"__wa_payload__": "image", "data": b"x", "format": "png"}
    with patch("plugin.notebook.notebook_runner.is_image_payload", return_value=True):
        text = format_run_output_text({"status": "ok", "result": wire, "stdout": ""})
    assert text == ""


def test_read_code_from_field_finds_textfield():
    field_model = MagicMock()
    field_model.Name = "nb_cell_0_code"
    field_model.Text = "x = 1\n"

    portion = MagicMock()
    portion.getPropertyValue.return_value = "Frame"
    portion.TextField = field_model

    para = MagicMock()
    para.createEnumeration.return_value = _enum_of([portion])

    doc = MagicMock()
    doc.getText.return_value.createEnumeration.return_value = _enum_of([para])

    assert read_code_from_field(doc, "nb_cell_0_code") == "x = 1\n"


def _enum_of(items):
    enum = MagicMock()
    enum.hasMoreElements.side_effect = [True] * len(items) + [False]
    enum.nextElement.side_effect = items
    return enum


def test_run_cell_target_url():
    cell = new_code_cell_entry(0, None, "nb_cell_0_code")
    url = run_cell_target_url(cell.cell_id)
    assert url == f"org.extension.writeragent:notebook.run_cell.{cell_id_to_hex(cell.cell_id)}"


def test_init_registry_execution_counter():
    c0 = new_code_cell_entry(0, 3, "nb_cell_0_code")
    c1 = new_code_cell_entry(1, None, "nb_cell_1_code")
    state = NotebookDocState(code_cells=[c0, c1])
    init_registry_execution_counter(state)
    assert state.next_execution_count == 4


def test_execute_code_uses_blocking_pump():
    ctx = MagicMock()
    doc = MagicMock()
    worker_result = {"status": "ok", "result": 1, "stdout": ""}

    with (
        patch("plugin.notebook.notebook_runner.notebook_session_id", return_value="notebook:test"),
        patch("plugin.notebook.notebook_runner.run_blocking_in_thread", return_value=worker_result) as pump,
        patch("plugin.notebook.notebook_runner.run_code_in_user_venv") as run_venv,
    ):
        out = execute_code(ctx, doc, "x = 1")
        assert out == worker_result
        pump.assert_called_once()
        run_venv.assert_not_called()


def test_run_cell_updates_registry_and_execution_count():
    ctx = MagicMock()
    cell = new_code_cell_entry(0, None, "nb_cell_0_code")
    state = NotebookDocState(code_cells=[cell], next_execution_count=5)
    doc = MagicMock()

    with (
        patch("plugin.notebook.notebook_runner.load_registry", return_value=state),
        patch("plugin.notebook.notebook_runner.read_code_from_field", return_value="print(1)"),
        patch(
            "plugin.notebook.notebook_runner.execute_code",
            return_value={"status": "ok", "result": None, "stdout": "1\n"},
        ),
        patch("plugin.notebook.notebook_runner.clear_cell_output"),
        patch("plugin.notebook.notebook_runner.apply_run_result"),
        patch("plugin.notebook.notebook_runner.update_in_prompt"),
        patch("plugin.notebook.notebook_runner.save_registry") as save_reg,
        patch("plugin.notebook.notebook_runner.flush_ui_idle"),
    ):
        result = run_cell(ctx, doc, cell.cell_id)

    assert result.status == "ok"
    assert result.execution_count == 5
    assert cell.execution_count == 5
    assert state.next_execution_count == 6
    save_reg.assert_called_once_with(doc, state)


def test_run_cell_empty_code():
    ctx = MagicMock()
    cell = new_code_cell_entry(0, None, "nb_cell_0_code")
    state = NotebookDocState(code_cells=[cell])
    doc = MagicMock()

    with (
        patch("plugin.notebook.notebook_runner.load_registry", return_value=state),
        patch("plugin.notebook.notebook_runner.read_code_from_field", return_value="   "),
    ):
        result = run_cell(ctx, doc, cell.cell_id)
    assert result.status == "error"
    assert "empty" in result.message.lower()


def test_shared_notebook_session_via_sandbox():
    from plugin.scripting.venv_sandbox import clear_all_sandbox_sessions
    from plugin.scripting.worker_harness import _execute_request

    clear_all_sandbox_sessions()
    sid = "notebook:test-runner"
    r1 = _execute_request("x = 41\nresult = x + 1", None, session_id=sid)
    assert r1["status"] == "ok"
    r2 = _execute_request("result = x + 1", None, session_id=sid)
    assert r2["status"] == "ok"
    assert r2["result"] == 42
    clear_all_sandbox_sessions()


@pytest.mark.parametrize(
    "hex_id,expected_ok",
    [
        ("abc", False),
        ("0" * 32, True),
    ],
)
def test_cell_id_hex_round_trip(hex_id, expected_ok):
    from plugin.notebook.cell_registry import cell_id_from_hex

    restored = cell_id_from_hex(hex_id)
    if not expected_ok:
        assert restored is None
        return
    assert restored is not None
    assert cell_id_to_hex(restored) == hex_id
