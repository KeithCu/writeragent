# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Unit tests for Calc cell-comment read helpers (xlsx caption fallback)."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from plugin.calc.comments import ListCellComments, _annotation_date, _annotation_text


def test_annotation_text_uses_get_string_when_present():
    sheet = MagicMock()
    cell = MagicMock()
    ann = MagicMock()
    ann.getString.return_value = "hello"
    cell.getAnnotation.return_value = ann
    sheet.getCellByPosition.return_value = cell

    got_ann, text = _annotation_text(sheet, 1, 2)

    assert got_ann is ann
    assert text == "hello"
    ann.getAnnotationShape.assert_not_called()


def test_annotation_text_falls_back_to_annotation_shape():
    # After reopening .xlsx, XSheetAnnotation.getString() is empty until the
    # caption is materialised via getAnnotationShape() (GetOrCreateCaption).
    sheet = MagicMock()
    cell = MagicMock()
    ann = MagicMock()
    ann.getString.return_value = ""
    shape = MagicMock()
    shape.getString.return_value = "from shape"
    ann.getAnnotationShape.return_value = shape
    cell.getAnnotation.return_value = ann
    sheet.getCellByPosition.return_value = cell

    got_ann, text = _annotation_text(sheet, 0, 0)

    assert got_ann is ann
    assert text == "from shape"
    ann.getAnnotationShape.assert_called_once()


def _list_one_comment(date_value):
    """Run list_cell_comments against one mocked annotation."""
    ctx = MagicMock()
    doc = MagicMock()
    ctx.doc = doc

    sheet = MagicMock()
    sheet.getName.return_value = "Sheet1"

    # list_cell_comments resolves sheet via resolve_sheet
    def get_sheet_by_name(name):
        return sheet

    sheets = MagicMock()
    sheets.hasByName.return_value = True
    sheets.getByName.side_effect = get_sheet_by_name
    doc.getSheets.return_value = sheets

    controller = MagicMock()
    controller.getActiveSheet.return_value = sheet
    doc.getCurrentController.return_value = controller

    annotations = MagicMock()
    annotations.getCount.return_value = 1

    ann = MagicMock()
    pos = MagicMock()
    pos.Column = 0
    pos.Row = 0
    ann.getPosition.return_value = pos
    ann.getAuthor.return_value = "Author"
    ann.getIsVisible.return_value = False

    annotations.getByIndex.return_value = ann
    sheet.getAnnotations.return_value = annotations

    cell_ann = MagicMock()
    cell_ann.getDate.return_value = date_value
    cell_ann.getString.return_value = "Test comment"

    cell = MagicMock()
    cell.getAnnotation.return_value = cell_ann
    sheet.getCellByPosition.return_value = cell

    res = ListCellComments().execute(ctx, sheet="Sheet1")
    json.dumps(res)
    return res


def test_list_cell_comments_keeps_formatted_date_string():
    # XSheetAnnotation.getDate() is a formatted string, not a DateTime struct.
    # Treating it as a struct dropped every date (attribute access failed).
    res = _list_one_comment("10/24/2023 12:30 PM")

    assert res["status"] == "ok"
    assert res["comments"][0]["date"] == "10/24/2023 12:30 PM"
    assert res["comments"][0]["text"] == "Test comment"


def test_list_cell_comments_formats_datetime_struct_fallback():
    class DummyDateTime:
        Year = 2023
        Month = 10
        Day = 24
        Hours = 12
        Minutes = 30

    res = _list_one_comment(DummyDateTime())
    assert res["comments"][0]["date"] == "2023-10-24 12:30"


def test_annotation_date_string_struct_and_empty():
    class DummyDateTime:
        Year = 2023
        Month = 1
        Day = 2
        Hours = 14
        Minutes = 5

    assert _annotation_date("2023-10-24 12:30") == "2023-10-24 12:30"
    assert _annotation_date("") == ""
    assert _annotation_date(DummyDateTime()) == "2023-01-02 14:05"
    assert _annotation_date(None) == ""


def test_add_cell_comment_updates_lazy_xlsx_note():
    from plugin.calc.comments import AddCellComment

    ctx = MagicMock()
    doc = MagicMock()
    ctx.doc = doc

    sheet = MagicMock()
    sheet.getName.return_value = "Sheet1"
    doc.getSheets.return_value.hasByName.return_value = True
    doc.getSheets.return_value.getByName.return_value = sheet
    doc.getCurrentController.return_value.getActiveSheet.return_value = sheet

    cell = MagicMock()
    cell_ann = MagicMock()
    cell_ann.getString.return_value = ""  # empty getString
    shape = MagicMock()
    shape.getString.return_value = "old note"  # lazy note in shape
    cell_ann.getAnnotationShape.return_value = shape
    cell.getAnnotation.return_value = cell_ann
    sheet.getCellByPosition.return_value = cell

    res = AddCellComment().execute(ctx, cell="A1", text="new text")
    assert res["status"] == "ok"
    cell_ann.setString.assert_called_once_with("new text")
    sheet.getAnnotations.return_value.insertNew.assert_not_called()


def test_add_cell_comment_inserts_new_when_no_note():
    from plugin.calc.comments import AddCellComment

    ctx = MagicMock()
    doc = MagicMock()
    ctx.doc = doc

    sheet = MagicMock()
    sheet.getName.return_value = "Sheet1"
    doc.getSheets.return_value.hasByName.return_value = True
    doc.getSheets.return_value.getByName.return_value = sheet
    doc.getCurrentController.return_value.getActiveSheet.return_value = sheet

    cell = MagicMock()
    cell_addr = MagicMock()
    cell.getCellAddress.return_value = cell_addr
    cell_ann = MagicMock()
    cell_ann.getString.return_value = ""
    cell_ann.getAnnotationShape.return_value = None  # no shape either
    cell.getAnnotation.return_value = cell_ann
    sheet.getCellByPosition.return_value = cell

    res = AddCellComment().execute(ctx, cell="A1", text="fresh text")
    assert res["status"] == "ok"
    sheet.getAnnotations.return_value.insertNew.assert_called_once_with(cell_addr, "fresh text")


def test_delete_cell_comment_found_and_not_found():
    from plugin.calc.comments import DeleteCellComment

    ctx = MagicMock()
    doc = MagicMock()
    ctx.doc = doc

    sheet = MagicMock()
    sheet.getName.return_value = "Sheet1"
    doc.getSheets.return_value.hasByName.return_value = True
    doc.getSheets.return_value.getByName.return_value = sheet
    doc.getCurrentController.return_value.getActiveSheet.return_value = sheet

    ann1 = MagicMock()
    ann1.getPosition.return_value = SimpleNamespace(Column=1, Row=2)
    ann2 = MagicMock()
    ann2.getPosition.return_value = SimpleNamespace(Column=0, Row=0)
    annotations = MagicMock()
    annotations.getCount.return_value = 2
    annotations.getByIndex.side_effect = [ann1, ann2]
    sheet.getAnnotations.return_value = annotations

    # Delete B3 (col=1, row=2)
    res = DeleteCellComment().execute(ctx, cell="B3")
    assert res["status"] == "ok"
    annotations.removeByIndex.assert_called_once_with(0)

    # Delete non-existent note
    annotations.removeByIndex.reset_mock()
    annotations.getByIndex.side_effect = [ann1, ann2]
    res_missing = DeleteCellComment().execute(ctx, cell="C5")
    assert res_missing["status"] == "error"
    assert "No comment found" in res_missing["message"]
    annotations.removeByIndex.assert_not_called()


def test_add_cell_comment_validation_and_resolution():
    from plugin.calc.comments import AddCellComment

    ctx = MagicMock()
    doc = MagicMock()
    ctx.doc = doc

    sheet1 = MagicMock()
    sheet1.getName.return_value = "Sheet1"
    data_sheet = MagicMock()
    data_sheet.getName.return_value = "Data Sheet"

    def get_sheet(name):
        if name == "Sheet1":
            return sheet1
        if name == "Data Sheet":
            return data_sheet
        return None

    sheets = MagicMock()
    sheets.hasByName.side_effect = lambda n: n in ("Sheet1", "Data Sheet")
    sheets.getByName.side_effect = get_sheet
    sheets.getElementNames.return_value = ("Sheet1", "Data Sheet")
    doc.getSheets.return_value = sheets
    doc.getCurrentController.return_value.getActiveSheet.return_value = sheet1

    # Empty cell / text
    assert AddCellComment().execute(ctx, cell="", text="hi")["status"] == "error"
    assert AddCellComment().execute(ctx, cell="A1", text="")["status"] == "error"

    # Sheet qualified Sheet1.A10
    cell = MagicMock()
    cell.getAnnotation.return_value.getString.return_value = "exists"
    sheet1.getCellByPosition.return_value = cell
    res1 = AddCellComment().execute(ctx, cell="Sheet1.A10", text="note")
    assert res1["status"] == "ok"
    assert res1["sheet"] == "Sheet1"

    # 'Data Sheet'!B2
    data_cell = MagicMock()
    data_cell.getAnnotation.return_value.getString.return_value = "exists"
    data_sheet.getCellByPosition.return_value = data_cell
    res2 = AddCellComment().execute(ctx, cell="'Data Sheet'!B2", text="note")
    assert res2["status"] == "ok"
    assert res2["sheet"] == "Data Sheet"

    # Prefix/sheet disagreement
    res_disagree = AddCellComment().execute(ctx, cell="Sheet1.A1", sheet="Data Sheet", text="note")
    assert res_disagree["status"] == "error"

    # Missing sheet
    res_missing = AddCellComment().execute(ctx, cell="NoSheet.A1", text="note")
    assert res_missing["status"] == "error"
    assert "Sheet1, Data Sheet" in res_missing["message"]


def test_list_cell_comments_sorted_by_row_col():
    ctx = MagicMock()
    doc = MagicMock()
    ctx.doc = doc

    sheet = MagicMock()
    sheet.getName.return_value = "Sheet1"
    doc.getSheets.return_value.hasByName.return_value = True
    doc.getSheets.return_value.getByName.return_value = sheet
    doc.getCurrentController.return_value.getActiveSheet.return_value = sheet

    ann_b2 = MagicMock()
    ann_b2.getPosition.return_value = SimpleNamespace(Column=1, Row=1)
    ann_a1 = MagicMock()
    ann_a1.getPosition.return_value = SimpleNamespace(Column=0, Row=0)
    ann_c1 = MagicMock()
    ann_c1.getPosition.return_value = SimpleNamespace(Column=2, Row=0)

    annotations = MagicMock()
    annotations.getCount.return_value = 3
    annotations.getByIndex.side_effect = [ann_b2, ann_a1, ann_c1]
    sheet.getAnnotations.return_value = annotations

    cell_mock = MagicMock()
    cell_mock.getAnnotation.return_value.getString.return_value = "txt"
    cell_mock.getAnnotation.return_value.getDate.return_value = "2024-01-01"
    sheet.getCellByPosition.return_value = cell_mock

    res = ListCellComments().execute(ctx, sheet="Sheet1")
    cells = [c["cell"] for c in res["comments"]]
    # A1 (0, 0), C1 (2, 0), B2 (1, 1)
    assert cells == ["A1", "C1", "B2"]
