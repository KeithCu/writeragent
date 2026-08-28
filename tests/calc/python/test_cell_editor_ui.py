# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for the native Edit Python in Cell dialog."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import plugin.calc.python.cell_editor_ui as ui


class _FakeModel:
    def __init__(self, text: str = "", enabled: bool = True) -> None:
        self.Text = text
        self.Label = text
        self.Enabled = enabled
        self.HelpText = ""
        self.State = 0


class _FakeControl:
    def __init__(self, text: str = "", state: int = 0) -> None:
        self._text = text
        self._state = state
        self._model = _FakeModel(text)
        self._model.State = state
        self.action_listeners: list = []
        self.item_listeners: list = []
        self.focus = False

    def getText(self) -> str:
        return self._text

    def setText(self, text: str) -> None:
        self._text = text
        self._model.Text = text

    def getState(self) -> int:
        return self._state

    def setState(self, value: int) -> None:
        self._state = int(value)
        self._model.State = self._state

    def getModel(self) -> _FakeModel:
        return self._model

    def setEnable(self, enabled: bool) -> None:
        self._model.Enabled = bool(enabled)

    def setFocus(self) -> None:
        self.focus = True

    def addActionListener(self, listener: object) -> None:
        self.action_listeners.append(listener)

    def addItemListener(self, listener: object) -> None:
        self.item_listeners.append(listener)

    def addTextListener(self, listener: object) -> None:
        self.action_listeners.append(listener)


class _FakeDialog:
    def __init__(self) -> None:
        self.controls = {
            "BtnSave": _FakeControl(),
            "BtnCancel": _FakeControl(),
            "CellAddr": _FakeControl(""),
            "ChkPlainText": _FakeControl(state=0),
            "DataLbl": _FakeControl("Data:"),
            "DataEdit": _FakeControl(""),
            "StatusEdit": _FakeControl("Status: Ready"),
            "CodeEdit": _FakeControl(""),
        }
        self.visible = False
        self.disposed = False
        self.top_listeners: list = []

    def getControl(self, name: str) -> _FakeControl:
        return self.controls[name]

    def setVisible(self, visible: bool) -> None:
        self.visible = bool(visible)

    def addTopWindowListener(self, listener: object) -> None:
        self.top_listeners.append(listener)

    def dispose(self) -> None:
        self.disposed = True


def setup_function() -> None:
    ui.reset_native_cell_editor_for_tests()


def teardown_function() -> None:
    ui.reset_native_cell_editor_for_tests()


def _open_native(**kwargs):
    dlg = _FakeDialog()
    with patch.object(ui, "load_writeragent_dialog_detail", return_value=(dlg, None)):
        opened, detail = ui.show_native_python_cell_editor(
            MagicMock(),
            doc=kwargs.get("doc", MagicMock()),
            cell=kwargs.get("cell", MagicMock()),
            initial_code=kwargs.get("initial_code", "print(1)"),
            parsed_parts=kwargs.get("parsed_parts", None),
            code_cell=kwargs.get("code_cell"),
            code_ref=kwargs.get("code_ref"),
        )
    assert opened is True
    assert detail is None
    return dlg, ui._active


def test_native_load_plain_cell_checks_save_without_py():
    dlg, inst = _open_native(initial_code="print(1)", parsed_parts=None)
    assert dlg.controls["CodeEdit"].getText() == "print(1)"
    assert dlg.controls["ChkPlainText"].getState() == 1
    assert dlg.controls["DataEdit"]._model.Enabled is False
    assert "Ready" in dlg.controls["StatusEdit"].getText()
    assert inst is not None
    assert inst.is_open


def test_native_load_formula_cell_unchecks_plain():
    parts = SimpleNamespace(data_suffix="; A1:B2")
    with patch(
        "plugin.calc.python.formula_edit.format_data_binding_display",
        return_value="A1:B2",
    ):
        dlg, unused = _open_native(initial_code="result = 1", parsed_parts=parts)
    assert dlg.controls["ChkPlainText"].getState() == 0
    assert dlg.controls["DataEdit"].getText() == "A1:B2"
    assert dlg.controls["DataEdit"]._model.Enabled is True


def test_native_save_formula_mode():
    dlg, inst = _open_native(initial_code="result = 2", parsed_parts=None)
    dlg.controls["ChkPlainText"].setState(0)
    inst._sync_data_enabled()
    dlg.controls["DataEdit"].setText("C1:C2")
    dlg.controls["CodeEdit"].setText("result = 3")
    with patch(
        "plugin.calc.python.editor._apply_cell_save",
        return_value={"type": "saved", "ok": True, "save_as_plain": False},
    ) as mock_save:
        inst._save()
    mock_save.assert_called_once()
    kwargs = mock_save.call_args.kwargs
    assert kwargs["new_code"] == "result = 3"
    assert kwargs["save_as_plain"] is False
    assert kwargs["data_binding_text"] == "C1:C2"
    assert "Saved." in dlg.controls["StatusEdit"].getText()


def test_native_save_plain_mode():
    dlg, inst = _open_native(initial_code="print(1)", parsed_parts=None)
    with patch(
        "plugin.calc.python.editor._apply_cell_save",
        return_value={
            "type": "saved",
            "ok": True,
            "save_as_plain": True,
            "status_ok_text": "Saved without =PY().",
        },
    ) as mock_save:
        inst._save()
    assert mock_save.call_args.kwargs["save_as_plain"] is True
    assert mock_save.call_args.kwargs["data_binding_text"] is None
    assert "Saved without =PY()." in dlg.controls["StatusEdit"].getText()


def test_native_save_error_status():
    dlg, inst = _open_native(initial_code="x", parsed_parts=None)
    with patch(
        "plugin.calc.python.editor._apply_cell_save",
        return_value={"type": "error", "message": "could not rewrite"},
    ):
        inst._save()
    assert "could not rewrite" in dlg.controls["StatusEdit"].getText()


def test_native_retarget_reloads_code():
    dlg, inst = _open_native(initial_code="a = 1", parsed_parts=None)
    cell_b = MagicMock()
    inst.retarget(doc=MagicMock(), cell=cell_b, initial_code="a = 2", parsed_parts=None)
    assert dlg.controls["CodeEdit"].getText() == "a = 2"
    assert inst._cell is cell_b


def test_native_cancel_disposes():
    dlg, inst = _open_native(initial_code="x", parsed_parts=None)
    inst.close()
    assert dlg.disposed is True
    assert inst.is_open is False
    assert ui._active is None


def test_native_window_closing_does_not_dispose():
    dlg, inst = _open_native(initial_code="x", parsed_parts=None)
    assert dlg.top_listeners
    dlg.top_listeners[0].windowClosing(None)
    assert dlg.disposed is False
    assert inst.is_open is False
    assert ui._active is None


def test_native_second_open_retargets_same_dialog():
    dlg1, inst1 = _open_native(initial_code="first", parsed_parts=None)
    with patch.object(ui, "load_writeragent_dialog_detail") as mock_load:
        opened, unused = ui.show_native_python_cell_editor(
            MagicMock(),
            doc=MagicMock(),
            cell=MagicMock(),
            initial_code="second",
            parsed_parts=None,
        )
    assert opened is True
    mock_load.assert_not_called()
    assert ui._active is inst1
    assert dlg1.controls["CodeEdit"].getText() == "second"


def test_native_dirty_retarget_cancel_keeps_code():
    dlg, inst = _open_native(initial_code="first", parsed_parts=None)
    inst._dirty = True
    with patch("plugin.calc.python.editor.confirm_unsaved_cell_edit", return_value="cancel"):
        opened, unused = ui.show_native_python_cell_editor(
            MagicMock(),
            doc=MagicMock(),
            cell=MagicMock(),
            initial_code="second",
            parsed_parts=None,
        )
    assert opened is True
    assert dlg.controls["CodeEdit"].getText() == "first"


def test_native_dirty_retarget_save_then_loads_new_cell():
    dlg, inst = _open_native(initial_code="first", parsed_parts=None)
    inst._dirty = True
    with patch("plugin.calc.python.editor.confirm_unsaved_cell_edit", return_value="save"), patch.object(
        inst, "_save", side_effect=lambda: setattr(inst, "_dirty", False)
    ):
        ui.show_native_python_cell_editor(
            MagicMock(),
            doc=MagicMock(),
            cell=MagicMock(),
            initial_code="second",
            parsed_parts=None,
        )
    assert dlg.controls["CodeEdit"].getText() == "second"


def test_native_dirty_retarget_save_error_keeps_cell():
    dlg, inst = _open_native(initial_code="first", parsed_parts=None)
    inst._dirty = True
    with patch("plugin.calc.python.editor.confirm_unsaved_cell_edit", return_value="save"), patch.object(
        inst, "_save"
    ):
        ui.show_native_python_cell_editor(
            MagicMock(),
            doc=MagicMock(),
            cell=MagicMock(),
            initial_code="second",
            parsed_parts=None,
        )
    assert dlg.controls["CodeEdit"].getText() == "first"
    assert inst._dirty is True


def test_confirm_unsaved_cell_edit_maps_yes_no_cancel():
    from plugin.calc.python.editor import confirm_unsaved_cell_edit

    box = MagicMock()
    toolkit = MagicMock()
    toolkit.createMessageBox.return_value = box
    smgr = MagicMock()
    smgr.createInstanceWithContext.return_value = toolkit
    ctx = MagicMock()
    ctx.getServiceManager.return_value = smgr
    desktop = MagicMock()
    desktop.getCurrentFrame.return_value.getContainerWindow.return_value = MagicMock()
    with patch("plugin.framework.uno_context.get_desktop", return_value=desktop):
        box.execute.return_value = 2
        assert confirm_unsaved_cell_edit(ctx, "A1") == "save"
        box.execute.return_value = 3
        assert confirm_unsaved_cell_edit(ctx, "A1") == "discard"
        box.execute.return_value = 0
        assert confirm_unsaved_cell_edit(ctx, "A1") == "cancel"


def test_native_dirty_retarget_discard_loads_new_cell():
    dlg, inst = _open_native(initial_code="first", parsed_parts=None)
    inst._dirty = True
    with patch("plugin.calc.python.editor.confirm_unsaved_cell_edit", return_value="discard"):
        ui.show_native_python_cell_editor(
            MagicMock(),
            doc=MagicMock(),
            cell=MagicMock(),
            initial_code="second",
            parsed_parts=None,
        )
    assert dlg.controls["CodeEdit"].getText() == "second"
    assert inst._dirty is False


def test_monaco_launch_cancel_skips_load():
    from plugin.calc.python import editor as ed

    cell = MagicMock()
    cell.getCellAddress.return_value = SimpleNamespace(Column=0, Row=0)
    with patch.object(ed, "calc_cell_session_needs_flush", return_value=True), patch.object(
        ed, "confirm_unsaved_cell_edit", return_value="cancel"
    ), patch.object(ed, "launch_monaco_editor") as launch, patch.object(
        ed, "queue_save_then_load"
    ) as queued:
        ed._launch_editor_with_code(
            MagicMock(),
            MagicMock(),
            cell,
            initial_code="x",
            parsed_parts=None,
            exe="/bin/python",
        )
    launch.assert_not_called()
    queued.assert_not_called()


def test_format_cell_a1():
    from plugin.calc.python.editor import format_cell_a1

    cell = MagicMock()
    cell.getCellAddress.return_value = SimpleNamespace(Column=0, Row=0)
    assert format_cell_a1(cell) == "A1"
    cell.getCellAddress.return_value = SimpleNamespace(Column=26, Row=9)
    assert format_cell_a1(cell) == "AA10"


def test_native_follow_ref_keeps_data_enabled_and_saves_code_cell():
    formula_cell = MagicMock()
    formula_cell.getCellAddress.return_value = SimpleNamespace(Column=1, Row=0, Sheet=0)
    code_cell = MagicMock()
    code_cell.getCellAddress.return_value = SimpleNamespace(Column=0, Row=0, Sheet=0)
    parts = SimpleNamespace(data_suffix="; C1:C10)")
    with patch(
        "plugin.calc.python.formula_edit.format_data_binding_display",
        return_value="C1:C10",
    ):
        dlg, inst = _open_native(
            cell=formula_cell,
            code_cell=code_cell,
            code_ref="$A$1",
            initial_code="result = 42",
            parsed_parts=parts,
        )
    assert inst is not None
    assert inst._following_ref() is True
    assert dlg.controls["ChkPlainText"].getState() == 1
    assert dlg.controls["DataEdit"]._model.Enabled is True
    assert dlg.controls["DataEdit"].getText() == "C1:C10"
    assert dlg.controls["CellAddr"].getText() == "A1"
    dlg.controls["CodeEdit"].setText("result = 99")
    with patch(
        "plugin.calc.python.editor._apply_cell_save",
        return_value={
            "type": "saved",
            "ok": True,
            "save_as_plain": True,
            "status_ok_text": "Saved without =PY().",
        },
    ) as mock_save:
        inst._save()
    kwargs = mock_save.call_args.kwargs
    assert kwargs["new_code"] == "result = 99"
    assert kwargs["code_cell"] is code_cell
    assert kwargs["code_ref"] == "$A$1"
    assert kwargs["data_binding_text"] == "C1:C10"


def test_monaco_follow_ref_load_shows_code_cell_and_keeps_data():
    from plugin.calc.python import editor as ed
    from plugin.calc.python.formula_edit import parse_python_formula

    formula_cell = MagicMock()
    formula_cell.getCellAddress.return_value = SimpleNamespace(Column=1, Row=0, Sheet=0)
    code_cell = MagicMock()
    code_cell.getCellAddress.return_value = SimpleNamespace(Column=0, Row=0, Sheet=0)
    parts = parse_python_formula("=PY($A$1; C1:C10)")
    assert parts is not None
    captured: dict = {}

    def fake_launch(_ctx, *, exe, load_message, on_save, on_closed=None):
        captured["load_message"] = load_message
        captured["on_save"] = on_save
        return True

    with patch.object(ed, "calc_cell_session_needs_flush", return_value=False), patch.object(
        ed, "launch_monaco_editor", side_effect=fake_launch
    ):
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
    with patch.object(ed, "_apply_cell_save", return_value={"type": "saved", "ok": True}) as mock_save:
        captured["on_save"]("result = 99", False, "D1:D5")
    kwargs = mock_save.call_args.kwargs
    assert kwargs["save_as_plain"] is True
    assert kwargs["code_cell"] is code_cell
    assert kwargs["code_ref"] == "$A$1"
    assert kwargs["data_binding_text"] == "D1:D5"
    assert kwargs["new_code"] == "result = 99"


def test_open_unquoted_ref_loads_followed_cell_text():
    from plugin.calc.python import editor as ed
    from plugin.calc.python.formula_edit import parse_python_formula

    ctx = MagicMock()
    doc = MagicMock()
    formula_cell = MagicMock()
    formula_cell.getCellAddress.return_value = SimpleNamespace(Column=1, Row=0, Sheet=0)
    code_cell = MagicMock()
    code_cell.getCellAddress.return_value = SimpleNamespace(Column=0, Row=0, Sheet=0)
    code_cell.getString.return_value = "result = 42"
    parts = parse_python_formula("=PY($A$1; C1:C10)")
    assert parts is not None
    captured: dict = {}

    def fake_launch(_ctx, *, exe, load_message, on_save, on_closed=None):
        captured["load_message"] = load_message
        return True

    with patch.object(ed, "get_active_session", return_value=None), patch.object(
        ed, "_get_active_calc_cell", return_value=(doc, formula_cell, "=PY($A$1; C1:C10)")
    ), patch.object(
        ed, "_load_cell_editor_code", return_value=("$A$1", parts, "=PY($A$1; C1:C10)")
    ), patch.object(
        ed, "_resolve_code_ref_cell", return_value=code_cell
    ), patch.object(
        ed, "monaco_editor_available", return_value=("/venv/bin/python", True)
    ), patch.object(
        ed, "launch_monaco_editor", side_effect=fake_launch
    ), patch("plugin.calc.python.editor_context_menu.install_calc_cell_context_menu"):
        ed.open_python_cell_editor(ctx)

    load_msg = captured["load_message"]
    assert load_msg["code"] == "result = 42"
    assert load_msg["follow_code_ref"] is True
    assert load_msg["save_as_plain"] is True
    assert load_msg["cell_address"] == "A1"


def test_open_quoted_a1_does_not_follow_code_cell():
    from plugin.calc.python import editor as ed
    from plugin.calc.python.formula_edit import parse_python_formula

    ctx = MagicMock()
    doc = MagicMock()
    cell = MagicMock()
    cell.getCellAddress.return_value = SimpleNamespace(Column=1, Row=0, Sheet=0)
    parts = parse_python_formula('=PY("A1")')
    assert parts is not None
    captured: dict = {}

    def fake_launch(_ctx, *, exe, load_message, on_save, on_closed=None):
        captured["load_message"] = load_message
        return True

    with patch.object(ed, "get_active_session", return_value=None), patch.object(
        ed, "_get_active_calc_cell", return_value=(doc, cell, '=PY("A1")')
    ), patch.object(
        ed, "_load_cell_editor_code", return_value=("A1", parts, '=PY("A1")')
    ), patch.object(
        ed, "_resolve_code_ref_cell"
    ) as resolve, patch.object(
        ed, "monaco_editor_available", return_value=("/venv/bin/python", True)
    ), patch.object(
        ed, "launch_monaco_editor", side_effect=fake_launch
    ), patch("plugin.calc.python.editor_context_menu.install_calc_cell_context_menu"):
        ed.open_python_cell_editor(ctx)

    resolve.assert_not_called()
    load_msg = captured["load_message"]
    assert load_msg["code"] == "A1"
    assert load_msg["follow_code_ref"] is False
    assert load_msg["save_as_plain"] is False


def _open_with_formula(formula, *, resolve_cell=None, initial_code=None):
    from plugin.calc.python import editor as ed
    from plugin.calc.python.formula_edit import parse_python_formula

    ctx = MagicMock()
    doc = MagicMock()
    cell = MagicMock()
    cell.getCellAddress.return_value = SimpleNamespace(Column=1, Row=0, Sheet=0)
    parts = parse_python_formula(formula)
    assert parts is not None
    code = initial_code if initial_code is not None else parts.code
    captured: dict = {}

    def fake_launch(_ctx, *, exe, load_message, on_save, on_closed=None):
        captured["load_message"] = load_message
        return True

    resolve_target = resolve_cell if resolve_cell is not None else MagicMock()
    with patch.object(ed, "get_active_session", return_value=None), patch.object(
        ed, "_get_active_calc_cell", return_value=(doc, cell, formula)
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
    ) as boxed, patch("plugin.calc.python.editor_context_menu.install_calc_cell_context_menu"):
        ed.open_python_cell_editor(ctx)
    captured["resolve"] = resolve
    captured["msgbox"] = boxed
    captured["cell"] = cell
    captured["parts"] = parts
    return captured


def test_open_range_code_arg_does_not_follow():
    captured = _open_with_formula("=PY(A1:A10)")
    captured["resolve"].assert_not_called()
    assert captured["load_message"]["follow_code_ref"] is False
    assert captured["load_message"]["code"] == "A1:A10"


def test_open_a1_plus_b1_does_not_follow():
    captured = _open_with_formula("=PY(A1+B1)")
    captured["resolve"].assert_not_called()
    assert captured["load_message"]["follow_code_ref"] is False
    assert captured["load_message"]["code"] == "A1+B1"


def test_open_sp_prime_does_not_follow():
    captured = _open_with_formula("=PY(sp.prime(100))")
    captured["resolve"].assert_not_called()
    assert captured["load_message"]["follow_code_ref"] is False
    assert captured["load_message"]["code"] == "sp.prime(100)"


def test_open_relative_a1_follows_code_cell():
    code_cell = MagicMock()
    code_cell.getCellAddress.return_value = SimpleNamespace(Column=0, Row=0, Sheet=0)
    code_cell.getString.return_value = "result = 11"
    captured = _open_with_formula("=PY(A1)", resolve_cell=code_cell, initial_code="A1")
    captured["resolve"].assert_called_once()
    assert captured["load_message"]["code"] == "result = 11"
    assert captured["load_message"]["follow_code_ref"] is True


def test_open_sheet2_follows_code_cell():
    code_cell = MagicMock()
    code_cell.getCellAddress.return_value = SimpleNamespace(Column=0, Row=0, Sheet=1)
    code_cell.getString.return_value = "result = 7"
    captured = _open_with_formula(
        "=PY(Sheet2.A1; C1:C2)",
        resolve_cell=code_cell,
        initial_code="Sheet2.A1",
    )
    captured["resolve"].assert_called_once()
    assert captured["load_message"]["code"] == "result = 7"
    assert captured["load_message"]["follow_code_ref"] is True
    assert captured["load_message"]["data_binding"] == "C1:C2"


def test_open_python_alias_follows_code_cell():
    code_cell = MagicMock()
    code_cell.getCellAddress.return_value = SimpleNamespace(Column=0, Row=0, Sheet=0)
    code_cell.getString.return_value = "result = 5"
    captured = _open_with_formula(
        "=PYTHON($A$1; C1:C2)",
        resolve_cell=code_cell,
        initial_code="$A$1",
    )
    captured["resolve"].assert_called_once()
    assert captured["load_message"]["code"] == "result = 5"
    assert captured["load_message"]["follow_code_ref"] is True
    assert captured["load_message"]["data_binding"] == "C1:C2"


def test_open_missing_code_cell_shows_error():
    from plugin.calc.python import editor as ed
    from plugin.calc.python.formula_edit import parse_python_formula

    ctx = MagicMock()
    doc = MagicMock()
    cell = MagicMock()
    formula = "=PY(Missing.A1)"
    parts = parse_python_formula(formula)
    assert parts is not None
    with patch.object(ed, "get_active_session", return_value=None), patch.object(
        ed, "_get_active_calc_cell", return_value=(doc, cell, formula)
    ), patch.object(
        ed, "_load_cell_editor_code", return_value=("Missing.A1", parts, formula)
    ), patch.object(
        ed, "_resolve_code_ref_cell", return_value=None
    ), patch.object(
        ed, "launch_monaco_editor"
    ) as launch, patch.object(
        ed, "msgbox"
    ) as boxed, patch("plugin.calc.python.editor_context_menu.install_calc_cell_context_menu"):
        ed.open_python_cell_editor(ctx)
    launch.assert_not_called()
    boxed.assert_called_once()
    assert "Missing.A1" in boxed.call_args.args[2]


def test_native_follow_two_data_ranges_shown_and_saved():
    from plugin.calc.python.formula_edit import parse_python_formula

    formula_cell = MagicMock()
    formula_cell.getCellAddress.return_value = SimpleNamespace(Column=1, Row=0, Sheet=0)
    code_cell = MagicMock()
    code_cell.getCellAddress.return_value = SimpleNamespace(Column=0, Row=0, Sheet=0)
    parts = parse_python_formula("=PY($A$1; B1:B10; C1:C10)")
    dlg, inst = _open_native(
        cell=formula_cell,
        code_cell=code_cell,
        code_ref="$A$1",
        initial_code="result = data",
        parsed_parts=parts,
    )
    assert inst is not None
    assert inst._following_ref() is True
    assert dlg.controls["DataEdit"]._model.Enabled is True
    assert "B1:B10" in dlg.controls["DataEdit"].getText()
    assert "C1:C10" in dlg.controls["DataEdit"].getText()
    dlg.controls["DataEdit"].setText("D1:D5, E1:E5")
    dlg.controls["CodeEdit"].setText("result = 1")
    with patch(
        "plugin.calc.python.editor._apply_cell_save",
        return_value={"type": "saved", "ok": True, "save_as_plain": True},
    ) as mock_save:
        inst._save()
    assert mock_save.call_args.kwargs["data_binding_text"] == "D1:D5, E1:E5"
    assert mock_save.call_args.kwargs["code_ref"] == "$A$1"


def test_native_follow_python_alias_and_clear_data():
    from plugin.calc.python.formula_edit import parse_python_formula

    formula_cell = MagicMock()
    formula_cell.getCellAddress.return_value = SimpleNamespace(Column=1, Row=0, Sheet=0)
    code_cell = MagicMock()
    code_cell.getCellAddress.return_value = SimpleNamespace(Column=0, Row=0, Sheet=0)
    parts = parse_python_formula("=PYTHON($A$1; C1:C10)")
    dlg, inst = _open_native(
        cell=formula_cell,
        code_cell=code_cell,
        code_ref="$A$1",
        initial_code="result = data",
        parsed_parts=parts,
    )
    assert inst is not None
    assert inst._following_ref() is True
    assert dlg.controls["DataEdit"]._model.Enabled is True
    assert dlg.controls["DataEdit"].getText() == "C1:C10"
    dlg.controls["DataEdit"].setText("")
    dlg.controls["CodeEdit"].setText("result = 2")
    with patch(
        "plugin.calc.python.editor._apply_cell_save",
        return_value={"type": "saved", "ok": True, "save_as_plain": True},
    ) as mock_save:
        inst._save()
    assert mock_save.call_args.kwargs["data_binding_text"] == ""
    assert mock_save.call_args.kwargs["code_ref"] == "$A$1"
    assert mock_save.call_args.kwargs["new_code"] == "result = 2"
