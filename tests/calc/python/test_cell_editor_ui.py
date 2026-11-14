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
        self.events: list[str] = []

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
        self.events.append("focus")

    def addActionListener(self, listener: object) -> None:
        self.action_listeners.append(listener)

    def addItemListener(self, listener: object) -> None:
        self.item_listeners.append(listener)

    def addTextListener(self, listener: object) -> None:
        self.action_listeners.append(listener)


class _FakeDialog:
    def __init__(self) -> None:
        self.events: list[str] = []
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
        for ctrl in self.controls.values():
            ctrl.events = self.events
        self.visible = False
        self.disposed = False
        self.top_listeners: list = []

    def getControl(self, name: str) -> _FakeControl:
        return self.controls[name]

    def setVisible(self, visible: bool) -> None:
        self.visible = bool(visible)
        self.events.append("visible")

    def addTopWindowListener(self, listener: object) -> None:
        self.top_listeners.append(listener)

    def dispose(self) -> None:
        self.disposed = True


def _reset_native_editor() -> None:
    if ui._active is not None:
        ui._active.close()
    ui._active = None


def setup_function() -> None:
    _reset_native_editor()


def teardown_function() -> None:
    _reset_native_editor()


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
        "plugin.calc.python.editor.apply_cell_save",
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
        "plugin.calc.python.editor.apply_cell_save",
        return_value={
            "type": "saved",
            "ok": True,
            "save_as_plain": True,
            "status_ok_text": "Saved without =PY().",
        },
    ) as mock_save:
        inst._save()
    assert mock_save.call_args.kwargs["save_as_plain"] is True
    assert mock_save.call_args.kwargs["data_binding_text"] == ""
    assert "Saved without =PY()." in dlg.controls["StatusEdit"].getText()


def test_native_save_error_status():
    dlg, inst = _open_native(initial_code="x", parsed_parts=None)
    with patch(
        "plugin.calc.python.editor.apply_cell_save",
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


def test_native_dirty_retarget_names_followed_code_cell():
    formula_cell = MagicMock()
    formula_cell.getCellAddress.return_value = SimpleNamespace(Column=1, Row=0, Sheet=0)
    code_cell = MagicMock()
    code_cell.getCellAddress.return_value = SimpleNamespace(Column=0, Row=0, Sheet=0)
    dlg, inst = _open_native(
        cell=formula_cell,
        code_cell=code_cell,
        code_ref="$A$1",
        initial_code="result = 1",
        parsed_parts=None,
    )
    assert dlg.controls["CellAddr"].getText() == "A1"
    inst._dirty = True
    with patch("plugin.calc.python.editor.confirm_unsaved_cell_edit", return_value="cancel") as ask:
        opened, unused = ui.show_native_python_cell_editor(
            MagicMock(),
            doc=MagicMock(),
            cell=MagicMock(),
            initial_code="next",
            parsed_parts=None,
        )
    assert opened is True
    assert ask.call_args.args[1] == "A1"
    assert dlg.controls["CodeEdit"].getText() == "result = 1"


def test_native_dirty_retarget_names_selection_when_not_following():
    cell = MagicMock()
    cell.getCellAddress.return_value = SimpleNamespace(Column=2, Row=4, Sheet=0)
    dlg, inst = _open_native(cell=cell, initial_code="print(1)", parsed_parts=None)
    assert dlg.controls["CellAddr"].getText() == "C5"
    inst._dirty = True
    with patch("plugin.calc.python.editor.confirm_unsaved_cell_edit", return_value="cancel") as ask:
        ui.show_native_python_cell_editor(
            MagicMock(),
            doc=MagicMock(),
            cell=MagicMock(),
            initial_code="next",
            parsed_parts=None,
        )
    assert ask.call_args.args[1] == "C5"


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
        "plugin.calc.python.editor.apply_cell_save",
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


def test_monaco_calc_cell_save_keeps_data_binding_in_follow_mode():
    """Follow-mode Monaco save must still send the Data field (#1474); the JS has no CI runner, so the guard is the binding expression in editor_shell.js."""
    from pathlib import Path

    js_file = Path(__file__).resolve().parents[3] / "plugin" / "contrib" / "scripting" / "assets" / "editor" / "editor_shell.js"
    assert js_file.is_file()
    assert "var dataBinding = (followCodeRef || !saveAsPlain)" in js_file.read_text(encoding="utf-8")


def test_native_follow_two_data_ranges_shown_and_saved():
    from plugin.calc.python.formula_edit import PyFormula

    formula_cell = MagicMock()
    formula_cell.getCellAddress.return_value = SimpleNamespace(Column=1, Row=0, Sheet=0)
    code_cell = MagicMock()
    code_cell.getCellAddress.return_value = SimpleNamespace(Column=0, Row=0, Sheet=0)
    parts = PyFormula.parse("=PY($A$1; B1:B10; C1:C10)")
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
        "plugin.calc.python.editor.apply_cell_save",
        return_value={"type": "saved", "ok": True, "save_as_plain": True},
    ) as mock_save:
        inst._save()
    assert mock_save.call_args.kwargs["data_binding_text"] == "D1:D5, E1:E5"
    assert mock_save.call_args.kwargs["code_ref"] == "$A$1"


def test_native_follow_python_alias_and_clear_data():
    from plugin.calc.python.formula_edit import PyFormula

    formula_cell = MagicMock()
    formula_cell.getCellAddress.return_value = SimpleNamespace(Column=1, Row=0, Sheet=0)
    code_cell = MagicMock()
    code_cell.getCellAddress.return_value = SimpleNamespace(Column=0, Row=0, Sheet=0)
    parts = PyFormula.parse("=PYTHON($A$1; C1:C10)")
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
        "plugin.calc.python.editor.apply_cell_save",
        return_value={"type": "saved", "ok": True, "save_as_plain": True},
    ) as mock_save:
        inst._save()
    assert mock_save.call_args.kwargs["data_binding_text"] == ""
    assert mock_save.call_args.kwargs["code_ref"] == "$A$1"
    assert mock_save.call_args.kwargs["new_code"] == "result = 2"


def test_native_focuses_code_after_visible():
    dlg, inst = _open_native(initial_code="x", parsed_parts=None)
    assert dlg.visible is True
    assert dlg.events.index("visible") < dlg.events.index("focus")


def test_native_retarget_refocuses_code():
    dlg, inst = _open_native(initial_code="first", parsed_parts=None)
    dlg.events.clear()
    inst.retarget(doc=MagicMock(), cell=MagicMock(), initial_code="second", parsed_parts=None)
    assert dlg.events.count("focus") == 1
    assert "visible" not in dlg.events


def test_native_external_dispose_clears_active():
    dlg, inst = _open_native(initial_code="x", parsed_parts=None)
    assert dlg.top_listeners
    dlg.top_listeners[0].disposing(None)
    assert inst.is_open is False
    assert ui._active is None
    assert dlg.disposed is False


def test_native_save_exception_shows_reason_and_stays_dirty():
    dlg, inst = _open_native(initial_code="x", parsed_parts=None)
    inst._dirty = True
    with patch("plugin.calc.python.editor.apply_cell_save", side_effect=RuntimeError("boom")):
        inst._save()
    assert inst._dirty is True
    assert "Could not save the cell." in dlg.controls["StatusEdit"].getText()
    assert "boom" in dlg.controls["StatusEdit"].getText()


def test_xdl_controls_match_controller():
    import re
    import xml.etree.ElementTree as ET
    from pathlib import Path

    root_dir = Path(__file__).resolve().parents[3]
    root = ET.parse(root_dir / "extension" / "Dialogs" / "PythonCellEditorDialog.xdl").getroot()
    ns = "{http://openoffice.org/2000/dialog}"
    ids = {el.get(ns + "id") for el in root.iter() if el.get(ns + "id")}
    required = {
        "CellAddr", "BtnSave", "BtnCancel", "ChkPlainText",
        "DataLbl", "DataEdit", "StatusEdit", "CodeEdit",
    }
    assert required <= ids
    src = (root_dir / "plugin" / "calc" / "python" / "cell_editor_ui.py").read_text(encoding="utf-8")
    used = set(re.findall(r'(?:_ctrl|getControl)\("([^"]+)"\)', src))
    assert used <= ids


def test_status_text_prefixes_once():
    dlg, inst = _open_native(initial_code="x", parsed_parts=None)
    inst._set_status("Saved.")
    assert dlg.controls["StatusEdit"].getText() == "Status: Saved."
    inst._set_status("Status: Saved.")
    assert dlg.controls["StatusEdit"].getText() == "Status: Saved."
    inst._set_status("")
    assert dlg.controls["StatusEdit"].getText() == "Status: Ready"


def test_open_failure_returns_detail():
    with patch.object(ui, "load_writeragent_dialog_detail", return_value=(None, "no xdl")):
        opened, detail = ui.show_native_python_cell_editor(
            MagicMock(), doc=MagicMock(), cell=MagicMock(), initial_code="x", parsed_parts=None
        )
    assert opened is False
    assert detail == "no xdl"


def test_native_data_help_text_follows_enablement():
    dlg, inst = _open_native(initial_code="print(1)", parsed_parts=None)
    disabled_help = dlg.controls["DataEdit"]._model.HelpText
    assert disabled_help
    dlg.controls["ChkPlainText"].setState(0)
    inst._sync_data_enabled()
    enabled_help = dlg.controls["DataEdit"]._model.HelpText
    assert enabled_help
    assert enabled_help != disabled_help
