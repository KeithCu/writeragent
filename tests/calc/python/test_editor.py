# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for the Calc Python cell editor controller."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from plugin.calc.python import editor as ed
from plugin.calc.python.editor import confirm_unsaved_cell_edit, format_cell_a1
from plugin.calc.python.formula_edit import PyFormula


def test_confirm_unsaved_cell_edit_maps_yes_no_cancel():
    box = MagicMock()
    toolkit = MagicMock()
    toolkit.createMessageBox.return_value = box
    smgr = MagicMock()
    smgr.createInstanceWithContext.return_value = toolkit
    ctx = MagicMock()
    ctx.getServiceManager.return_value = smgr
    desktop = MagicMock()
    desktop.getCurrentFrame.return_value.getContainerWindow.return_value = MagicMock()
    with patch("plugin.scripting.editor_host.get_desktop", return_value=desktop):
        box.execute.return_value = 2
        assert confirm_unsaved_cell_edit(ctx, "A1") == "save"
        box.execute.return_value = 3
        assert confirm_unsaved_cell_edit(ctx, "A1") == "discard"
        box.execute.return_value = 0
        assert confirm_unsaved_cell_edit(ctx, "A1") == "cancel"


def test_monaco_cell_open_delegates_unsaved_guard_to_launch():
    """Cell open no longer confirms locally. launch_monaco_editor asks for every mode."""
    cell = MagicMock()
    cell.getCellAddress.return_value = SimpleNamespace(Column=0, Row=0)
    with patch.object(ed, "launch_monaco_editor") as launch:
        ed._launch_editor_with_code(
            MagicMock(),
            MagicMock(),
            cell,
            initial_code="x",
            parsed_parts=None,
            exe="/bin/python",
        )
    launch.assert_called_once()
    load = launch.call_args.kwargs["load_message"]
    assert load["mode"] == "calc_cell"
    assert load["cell_address"] == "A1"
    assert load["code"] == "x"


def test_format_cell_a1():
    cell = MagicMock()
    cell.getCellAddress.return_value = SimpleNamespace(Column=0, Row=0)
    assert format_cell_a1(cell) == "A1"
    cell.getCellAddress.return_value = SimpleNamespace(Column=26, Row=9)
    assert format_cell_a1(cell) == "AA10"


def test_monaco_follow_ref_load_shows_code_cell_and_keeps_data():
    formula_cell = MagicMock()
    formula_cell.getCellAddress.return_value = SimpleNamespace(Column=1, Row=0, Sheet=0)
    code_cell = MagicMock()
    code_cell.getCellAddress.return_value = SimpleNamespace(Column=0, Row=0, Sheet=0)
    parts = PyFormula.parse("=PY($A$1; C1:C10)")
    assert parts is not None
    captured: dict = {}

    def fake_launch(_ctx, *, exe, load_message, on_save, on_closed=None):
        captured["load_message"] = load_message
        captured["on_save"] = on_save
        return True

    with patch.object(ed, "launch_monaco_editor", side_effect=fake_launch):
        ed._launch_editor_with_code(
            MagicMock(),
            MagicMock(),
            formula_cell,
            initial_code="result = 42",
            parsed_parts=parts,
            exe="/bin/python",
            code_cell=code_cell,
            code_ref="$A$1",
        )
    load_msg = captured["load_message"]
    assert load_msg["follow_code_ref"] is True
    assert load_msg["save_as_plain"] is True
    assert load_msg["code"] == "result = 42"
    assert load_msg["cell_address"] == "A1"
    assert load_msg["data_binding"] == "C1:C10"
    with patch.object(ed, "apply_cell_save", return_value={"type": "saved", "ok": True}) as mock_save:
        captured["on_save"]("result = 99", True, "C1:C10")
    kwargs = mock_save.call_args.kwargs
    assert kwargs["save_as_plain"] is True
    assert kwargs["code_cell"] is code_cell
    assert kwargs["code_ref"] == "$A$1"
    assert kwargs["data_binding_text"] == "C1:C10"
    assert kwargs["new_code"] == "result = 99"


def test_open_unquoted_ref_loads_followed_cell_text():
    ctx = MagicMock()
    doc = MagicMock()
    formula_cell = MagicMock()
    formula_cell.getCellAddress.return_value = SimpleNamespace(Column=1, Row=0, Sheet=0)
    code_cell = MagicMock()
    code_cell.getCellAddress.return_value = SimpleNamespace(Column=0, Row=0, Sheet=0)
    code_cell.getString.return_value = "result = 42"
    parts = PyFormula.parse("=PY($A$1; C1:C10)")
    assert parts is not None
    captured: dict = {}

    def fake_launch(_ctx, *, exe, load_message, on_save, on_closed=None):
        captured["load_message"] = load_message
        return True

    with patch.object(
        ed, "_get_active_calc_cell", return_value=(doc, formula_cell)
    ), patch.object(
        ed, "_load_cell_editor_code", return_value=("$A$1", parts, "=PY($A$1; C1:C10)")
    ), patch.object(
        ed, "_resolve_code_ref_cell", return_value=code_cell
    ), patch.object(
        ed, "monaco_editor_available", return_value=("/venv/bin/python", True)
    ), patch.object(
        ed, "launch_monaco_editor", side_effect=fake_launch
    ), patch("plugin.calc.python.editor.install_calc_cell_context_menu"):
        ed.open_python_cell_editor(ctx)

    load_msg = captured["load_message"]
    assert load_msg["code"] == "result = 42"
    assert load_msg["follow_code_ref"] is True
    assert load_msg["save_as_plain"] is True
    assert load_msg["cell_address"] == "A1"


def test_open_quoted_a1_does_not_follow_code_cell():
    ctx = MagicMock()
    doc = MagicMock()
    cell = MagicMock()
    cell.getCellAddress.return_value = SimpleNamespace(Column=1, Row=0, Sheet=0)
    parts = PyFormula.parse('=PY("A1")')
    assert parts is not None
    captured: dict = {}

    def fake_launch(_ctx, *, exe, load_message, on_save, on_closed=None):
        captured["load_message"] = load_message
        return True

    with patch.object(
        ed, "_get_active_calc_cell", return_value=(doc, cell)
    ), patch.object(
        ed, "_load_cell_editor_code", return_value=("A1", parts, '=PY("A1")')
    ), patch.object(
        ed, "_resolve_code_ref_cell"
    ) as resolve, patch.object(
        ed, "monaco_editor_available", return_value=("/venv/bin/python", True)
    ), patch.object(
        ed, "launch_monaco_editor", side_effect=fake_launch
    ), patch("plugin.calc.python.editor.install_calc_cell_context_menu"):
        ed.open_python_cell_editor(ctx)

    resolve.assert_not_called()
    load_msg = captured["load_message"]
    assert load_msg["code"] == "A1"
    assert load_msg["follow_code_ref"] is False
    assert load_msg["save_as_plain"] is False


def _open_with_formula(formula, *, resolve_cell=None, initial_code=None):
    ctx = MagicMock()
    doc = MagicMock()
    cell = MagicMock()
    cell.getCellAddress.return_value = SimpleNamespace(Column=1, Row=0, Sheet=0)
    parts = PyFormula.parse(formula)
    assert parts is not None
    code = initial_code if initial_code is not None else parts.code
    captured: dict = {}

    def fake_launch(_ctx, *, exe, load_message, on_save, on_closed=None):
        captured["load_message"] = load_message
        return True

    resolve_target = resolve_cell if resolve_cell is not None else MagicMock()
    with patch.object(
        ed, "_get_active_calc_cell", return_value=(doc, cell)
    ), patch.object(
        ed, "_load_cell_editor_code", return_value=(code, parts, formula)
    ), patch.object(
        ed, "_resolve_code_ref_cell", return_value=resolve_target
    ) as resolve, patch.object(
        ed, "monaco_editor_available", return_value=("/venv/bin/python", True)
    ), patch.object(
        ed, "launch_monaco_editor", side_effect=fake_launch
    ), patch.object(
        ed, "msgbox"
    ) as boxed, patch("plugin.calc.python.editor.install_calc_cell_context_menu"):
        ed.open_python_cell_editor(ctx)
    captured["resolve"] = resolve
    captured["msgbox"] = boxed
    captured["cell"] = cell
    captured["parts"] = parts
    return captured


@pytest.mark.parametrize(
    "value, expected",
    [
        pytest.param("=PY(A1:A10)", "A1:A10", id="test_open_range_code_arg_does_not_follow"),
        pytest.param("=PY(A1+B1)", "A1+B1", id="test_open_a1_plus_b1_does_not_follow"),
        pytest.param("=PY(sp.prime(100))", "sp.prime(100)", id="test_open_sp_prime_does_not_follow"),
    ],
)
def test_open_range_code_arg_does_not_follow(value, expected):
    captured = _open_with_formula(value)
    captured["resolve"].assert_not_called()
    assert captured["load_message"]["follow_code_ref"] is False
    assert captured["load_message"]["code"] == expected


def test_open_relative_a1_follows_code_cell():
    code_cell = MagicMock()
    code_cell.getCellAddress.return_value = SimpleNamespace(Column=0, Row=0, Sheet=0)
    code_cell.getString.return_value = "result = 11"
    captured = _open_with_formula("=PY(A1)", resolve_cell=code_cell, initial_code="A1")
    captured["resolve"].assert_called_once()
    assert captured["load_message"]["code"] == "result = 11"
    assert captured["load_message"]["follow_code_ref"] is True


@pytest.mark.parametrize(
    "Sheet, value, value_2, initial_code, expected",
    [
        pytest.param(1, "result = 7", "=PY(Sheet2.A1; C1:C2)", "Sheet2.A1", "result = 7", id="test_open_sheet2_follows_code_cell"),
        pytest.param(0, "result = 5", "=PYTHON($A$1; C1:C2)", "$A$1", "result = 5", id="test_open_python_alias_follows_code_cell"),
    ],
)
def test_open_sheet2_follows_code_cell(Sheet, value, value_2, initial_code, expected):
    code_cell = MagicMock()
    code_cell.getCellAddress.return_value = SimpleNamespace(Column=0, Row=0, Sheet=Sheet)
    code_cell.getString.return_value = value
    captured = _open_with_formula(
        value_2,
        resolve_cell=code_cell,
        initial_code=initial_code,
    )
    captured["resolve"].assert_called_once()
    assert captured["load_message"]["code"] == expected
    assert captured["load_message"]["follow_code_ref"] is True
    assert captured["load_message"]["data_binding"] == "C1:C2"


def test_open_missing_code_cell_shows_error():
    ctx = MagicMock()
    doc = MagicMock()
    cell = MagicMock()
    formula = "=PY(Missing.A1)"
    parts = PyFormula.parse(formula)
    assert parts is not None
    with patch.object(
        ed, "_get_active_calc_cell", return_value=(doc, cell)
    ), patch.object(
        ed, "_load_cell_editor_code", return_value=("Missing.A1", parts, formula)
    ), patch.object(
        ed, "_resolve_code_ref_cell", return_value=None
    ), patch.object(
        ed, "launch_monaco_editor"
    ) as launch, patch.object(
        ed, "msgbox"
    ) as boxed, patch("plugin.calc.python.editor.install_calc_cell_context_menu"):
        ed.open_python_cell_editor(ctx)
    launch.assert_not_called()
    boxed.assert_called_once()
    assert "Missing.A1" in boxed.call_args.args[2]
