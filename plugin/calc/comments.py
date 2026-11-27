# Copyright (c) David Berlioz
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Calc cell annotation (comment) tools."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from plugin.calc.base import CALC_SHEET_PREFIX_NOTE, ToolCalcCommentBase
from plugin.calc.address_utils import format_address
from plugin.calc.bridge import CalcBridge
from plugin.framework.errors import make_tool_ok

if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext


def _annotation_text(sheet: Any, col: int, row: int) -> tuple[Any, str]:
    """Read a cell note's text, working around lazy captions on .xlsx.

    A workbook loaded from .xlsx has no caption object for its notes until
    something asks for one, and until then every XSheetAnnotation read path
    returns an empty string — while the text is plainly there in
    xl/comments*.xml.

    ``getAnnotationShape()`` resolves it: LibreOffice implements it as
    ``GetOrCreateCaption()`` (sc/source/ui/unoobj/notesuno.cxx), so the
    caption is materialised on demand and the shape's text is readable.
    Unlike forcing ``setIsVisible(True)``, this does not route through
    ShowNote, so it neither changes what the user sees nor pushes an undo
    action.
    """
    cell_ann = sheet.getCellByPosition(col, row).getAnnotation()
    text = cell_ann.getString()
    if text:
        return cell_ann, text
    shape = cell_ann.getAnnotationShape()
    if shape is not None:
        text = shape.getString() or ""
    return cell_ann, text


class ListCellComments(ToolCalcCommentBase):
    """List all cell comments/annotations in a sheet."""

    name: str | None = "cell_comment_list"
    description: str = "List all cell comments (annotations) in a Calc sheet. Returns cell address, author, date, and comment text."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"sheet": {"type": "string", "description": "Sheet name (active sheet if omitted)."}}, "required": []}

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        doc = ctx.doc
        sheet = CalcBridge(doc).sheet_or_active(kwargs.get("sheet"))
        annotations = sheet.getAnnotations()
        positioned = [(ann.getPosition(), ann) for ann in (annotations.getByIndex(i) for i in range(annotations.getCount()))]
        positioned.sort(key=lambda item: (item[0].Row, item[0].Column))
        comments = []
        for pos, ann in positioned:
            # Read text/date through the cell's own CellAnnotation (as the
            # write path does), not the XSheetAnnotations collection item,
            # whose getString()/getDate() come back empty (see _annotation_text).
            cell_ann, text = _annotation_text(sheet, pos.Column, pos.Row)
            date_str = cell_ann.getDate()
            comments.append(
                {
                    "cell": format_address(pos.Column, pos.Row),
                    "author": ann.getAuthor(),
                    # .xlsx has no date field on a comment element, so an
                    # empty date there is the format, not a failure.
                    "date": date_str,
                    "text": text,
                    "is_visible": ann.getIsVisible(),
                }
            )
        return make_tool_ok(comments=comments, count=len(comments), sheet=sheet.getName())


class AddCellComment(ToolCalcCommentBase):
    """Add a comment to a cell."""

    name: str | None = "cell_comment_add"
    description: str = "Add a comment (annotation) to a specific cell in a Calc sheet."
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {"cell": {"type": "string", "description": f"One cell (B3) or a named range that refers to one cell. {CALC_SHEET_PREFIX_NOTE}"}, "text": {"type": "string", "description": "Comment text."}, "sheet": {"type": "string", "description": "Sheet name (active sheet if omitted)."}},
        "required": ["cell", "text"],
    }
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        cell_ref = kwargs["cell"]
        text = kwargs["text"]
        if not cell_ref or not text:
            return self._tool_error("cell and text are required.", code="INVALID_PARAM")

        try:
            sheet, col, row = CalcBridge(ctx.doc).resolve_cell(cell_ref, kwargs.get("sheet"))
        except ValueError as e:
            # parse_address and a prefix/sheet disagreement raise ValueError; the bridge's
            # UnoObjectError (missing sheet) and CalcError (multi-cell name) reach execute_safe.
            return self._tool_error(str(e), code="INVALID_PARAM")

        cell_ann, existing_text = _annotation_text(sheet, col, row)
        if existing_text:
            cell_ann.setString(text)
        else:
            sheet.getAnnotations().insertNew(sheet.getCellByPosition(col, row).getCellAddress(), text)

        return make_tool_ok(cell=cell_ref, text=text, sheet=sheet.getName())


class DeleteCellComment(ToolCalcCommentBase):
    """Delete a comment from a cell."""

    name: str | None = "cell_comment_delete"
    description: str = "Delete the comment (annotation) from a specific cell."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"cell": {"type": "string", "description": f"One cell (B3) or a named range that refers to one cell. {CALC_SHEET_PREFIX_NOTE}"}, "sheet": {"type": "string", "description": "Sheet name (active sheet if omitted)."}}, "required": ["cell"]}
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        cell_ref = kwargs["cell"]
        if not cell_ref:
            return self._tool_error("cell is required.", code="INVALID_PARAM")

        try:
            sheet, col, row = CalcBridge(ctx.doc).resolve_cell(cell_ref, kwargs.get("sheet"))
        except ValueError as e:
            # parse_address and a prefix/sheet disagreement raise ValueError; the bridge's
            # UnoObjectError (missing sheet) and CalcError (multi-cell name) reach execute_safe.
            return self._tool_error(str(e), code="INVALID_PARAM")

        annotations = sheet.getAnnotations()
        for i in range(annotations.getCount()):
            ann = annotations.getByIndex(i)
            pos = ann.getPosition()
            if pos.Column == col and pos.Row == row:
                annotations.removeByIndex(i)
                return make_tool_ok(cell=cell_ref, message="Comment deleted.")

        return self._tool_error("No comment found at %s." % cell_ref, code="COMMENT_NOT_FOUND")
