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
