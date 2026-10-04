# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Unit tests for Calc cell-comment read helpers (xlsx caption fallback)."""

from unittest.mock import MagicMock

from plugin.calc.comments import _annotation_text, _split_cell_sheet


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


def test_split_cell_sheet_prefix_wins():
    address, sheet = _split_cell_sheet("Summary.B3", None)
    assert address == "B3"
    assert sheet == "Summary"


def test_split_cell_sheet_conflict_raises():
    try:
        _split_cell_sheet("Summary.B3", "Other")
        assert False, "expected ValueError"
    except ValueError as e:
        assert "Summary" in str(e)
        assert "Other" in str(e)

def test_list_cell_comments_serializes_uno_date():
    import json
    from plugin.calc.comments import ListCellComments
    from unittest.mock import MagicMock


    ctx = MagicMock()
    doc = MagicMock()
    ctx.doc = doc

    sheet = MagicMock()
    sheet.getName.return_value = "Sheet1"

    # Setup doc to return our sheet
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
    class DummyDateTime:
        Year = 2023
        Month = 10
        Day = 24
        Hours = 12
        Minutes = 30

    cell_ann.getDate.return_value = DummyDateTime()
    cell_ann.getString.return_value = "Test comment"

    cell = MagicMock()
    cell.getAnnotation.return_value = cell_ann
    sheet.getCellByPosition.return_value = cell

    tool = ListCellComments()
    res = tool.execute(ctx, sheet="Sheet1")

    assert res["status"] == "ok"
    assert res["comments"][0]["date"] == "2023-10-24 12:30"

    # Verify json.dumps works on the result without errors
    try:
        json.dumps(res)
    except TypeError:
        assert False, "ListCellComments result is not JSON serializable"
