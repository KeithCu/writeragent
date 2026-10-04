# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Unit tests for Calc form text insertion and per-field error reporting.

No LibreOffice process. Cell types are stubs with the same ``.value`` name
PyUNO's ``CellContentType`` enum exposes.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from plugin.writer.specialized.forms import (
    FormCreate,
    FormGenerate,
    _append_text_to_calc_active_area,
)


def _cell(kind, displayed=""):
    cell = MagicMock()
    if isinstance(kind, str):
        cell.getType.return_value = SimpleNamespace(value=kind)
    else:
        cell.getType.return_value = kind
    cell.getString.return_value = displayed
    return cell


def _doc_with_cell(cell, col=1, row=2):
    doc = MagicMock()
    controller = MagicMock()
    sheet = MagicMock()
    controller.ActiveSheet = sheet
    doc.getCurrentController.return_value = controller
    addr = SimpleNamespace(StartColumn=col, StartRow=row)
    selection = MagicMock()
    selection.getRangeAddress.return_value = addr
    controller.getSelection.return_value = selection
    sheet.getCellByPosition.return_value = cell
    return doc, sheet


def test_append_text_leaves_formula_and_value_cells():
    for kind in ("FORMULA", "VALUE"):
        cell = _cell(kind, "=A1+1" if kind == "FORMULA" else "42")
        doc, sheet = _doc_with_cell(cell, col=3, row=4)
        _append_text_to_calc_active_area(doc, " Name ")
        cell.setString.assert_not_called()
        sheet.getCellByPosition.assert_called_once_with(3, 4)


def test_append_text_leaves_enum_repr_formula():
    # Same shape as ``str(uno.Enum)`` when ``.value`` is not present.
    class EnumRepr:
        def __str__(self):
            return "<Enum instance com.sun.star.table.CellContentType ('FORMULA')>"

    cell = _cell(EnumRepr(), "=SUM(A1:A2)")
    doc, _sheet = _doc_with_cell(cell)
    _append_text_to_calc_active_area(doc, " ")
    cell.setString.assert_not_called()


def test_append_text_appends_to_text_and_writes_empty():
    text_cell = _cell("TEXT", "Hi")
    doc, _sheet = _doc_with_cell(text_cell)
    _append_text_to_calc_active_area(doc, " there")
    text_cell.setString.assert_called_once_with("Hi there")

    empty = _cell("EMPTY", "should-not-be-read")
    doc, _sheet = _doc_with_cell(empty)
    _append_text_to_calc_active_area(doc, "Label ")
    empty.setString.assert_called_once_with("Label ")
    empty.getString.assert_not_called()


def test_append_text_skips_unknown_cell_type():
    cell = MagicMock()
    cell.getString.return_value = "=A1"
    doc, _sheet = _doc_with_cell(cell)
    _append_text_to_calc_active_area(doc, " ")
    cell.setString.assert_not_called()


def test_append_text_without_selection_uses_a1():
    cell = _cell("EMPTY", "")
    doc = MagicMock()
    controller = MagicMock()
    sheet = MagicMock()
    controller.ActiveSheet = sheet
    controller.getSelection.return_value = SimpleNamespace()
    doc.getCurrentController.return_value = controller
    sheet.getCellByPosition.return_value = cell

    _append_text_to_calc_active_area(doc, "Label")

    sheet.getCellByPosition.assert_called_once_with(0, 0)
    cell.setString.assert_called_once_with("Label")


def test_insert_space_and_insert_text_skip_formula_cells(monkeypatch):
    monkeypatch.setattr("plugin.writer.specialized.forms._is_spreadsheet_doc", lambda doc: True)
    cell = _cell("FORMULA", "=B2")
    doc, _sheet = _doc_with_cell(cell)
    ctx = SimpleNamespace(doc=doc)

    FormCreate()._insert_space(ctx)
    FormGenerate()._insert_text(ctx, "<b>Name</b> extra")

    cell.setString.assert_not_called()


def test_insert_text_appends_plain_label_on_text_cell(monkeypatch):
    monkeypatch.setattr("plugin.writer.specialized.forms._is_spreadsheet_doc", lambda doc: True)
    cell = _cell("TEXT", "Hi")
    doc, _sheet = _doc_with_cell(cell)
    FormGenerate()._insert_text(SimpleNamespace(doc=doc), "<p>Name</p>")
    cell.setString.assert_called_once_with("HiName ")


def _patch_field_creator(monkeypatch, failing_name):
    def _execute(self, ctx, **kwargs):
        if kwargs.get("name") == failing_name:
            return {"status": "error", "code": "TOOL_EXECUTION_ERROR", "message": "no draw page"}
        return {"status": "ok", "message": "created", "control_name": kwargs.get("name")}

    monkeypatch.setattr("plugin.writer.specialized.forms.FormCreateControl._execute_main", _execute)


def test_form_create_reports_field_failures(monkeypatch):
    _patch_field_creator(monkeypatch, "bad")
    tool = FormCreate()
    monkeypatch.setattr(tool, "_insert_space", lambda ctx: None)

    res = tool.execute(MagicMock(), fields=[{"control": "text", "name": "ok1"}, {"control": "text", "name": "bad"}])

    assert res["status"] == "error"
    assert res["code"] == "TOOL_EXECUTION_ERROR"
    assert "bad: no draw page" in res["message"]
    assert "1 of 2" in res["message"]
    assert res["details"]["results"][0]["status"] == "ok"
    assert res["details"]["results"][1]["message"] == "no draw page"


def test_form_create_ok_when_every_field_succeeds(monkeypatch):
    _patch_field_creator(monkeypatch, "nobody")
    tool = FormCreate()
    monkeypatch.setattr(tool, "_insert_space", lambda ctx: None)

    res = tool.execute(MagicMock(), fields=[{"control": "text", "name": "ok1"}])

    assert res["status"] == "ok"
    assert res["results"][0]["control_name"] == "ok1"
    assert "Processed 1 form fields" in res["message"]


def test_form_generate_reports_field_failures(monkeypatch):
    _patch_field_creator(monkeypatch, "bad")
    tool = FormGenerate()
    monkeypatch.setattr(tool, "_insert_text", lambda ctx, text: None)

    res = tool._process_form_content(MagicMock(), "Hello {FIELD:control='text',name='bad',label='X'} tail")

    assert res["status"] == "error"
    assert "bad: no draw page" in res["message"]
    assert res["details"]["results"][0]["status"] == "error"


def test_form_generate_reports_unparsed_field_tag():
    tool = FormGenerate()
    tool._insert_text = lambda ctx, text: None

    res = tool._process_form_content(MagicMock(), "Intro {FIELD:broken} end")

    assert res["status"] == "error"
    assert "Could not parse form field tag" in res["message"]


def test_form_generate_ok_when_fields_succeed(monkeypatch):
    _patch_field_creator(monkeypatch, "nobody")
    tool = FormGenerate()
    inserted = []
    monkeypatch.setattr(tool, "_insert_text", lambda ctx, text: inserted.append(text))

    res = tool._process_form_content(MagicMock(), "Hello {FIELD:control='text',name='nm',label='Name'} tail")

    assert res == {"status": "ok", "message": "Form generation completed and inserted."}
    assert inserted == ["Hello ", " tail"]
