# Copyright (c) David Berlioz
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Calc cell annotation (comment) tools."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from plugin.calc.base import ToolCalcCommentBase
from plugin.calc.address_utils import format_address, parse_address
from plugin.calc.bridge import CalcBridge
from plugin.calc.calc_utils import resolve_sheet
from plugin.framework.errors import is_real_disposal

if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext


def _annotation_date(dt: Any) -> str:
    """Return a cell-comment date as text.

    A string is the date LibreOffice already formatted.
    ``XSheetAnnotation.getDate()`` returns that string
    (``offapi/com/sun/star/sheet/XSheetAnnotation.idl``). Reading ``.Year``
    on it raised, and both format attempts failed closed to empty text. A
    struct is still formatted when an older bridge or a test double
    provides one.
    """
    if isinstance(dt, str):
        return dt
    if not dt:
        return ""
    return "%04d-%02d-%02d %02d:%02d" % (dt.Year, dt.Month, dt.Day, dt.Hours, dt.Minutes)


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


def _resolve_comment_cell(
    tool: Any,
    doc: Any,
    cell_ref: str,
    sheet_name_arg: str | None = None,
) -> tuple[Any, int, int, str] | dict[str, Any]:
    try:
        sheet, address = CalcBridge(doc).resolve(cell_ref, sheet_name_arg)
        col, row = parse_address(address)
    except Exception as e:
        if is_real_disposal(e):
            raise
        return tool._tool_error(str(e))

    return sheet, col, row, cell_ref


class ListCellComments(ToolCalcCommentBase):
    """List all cell comments/annotations in a sheet."""

    name: str | None = "list_cell_comments"
    intent: str | None = "review"
    description: str = "List all cell comments (annotations) in a Calc sheet. Returns cell address, author, date, and comment text."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"sheet": {"type": "string", "description": "Sheet name (active sheet if omitted)."}}, "required": []}

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        doc = ctx.doc
        sheet = resolve_sheet(doc, kwargs.get("sheet"))
        annotations = sheet.getAnnotations()
        ordered = sorted(
            (annotations.getByIndex(i) for i in range(annotations.getCount())),
            key=lambda ann: (ann.getPosition().Row, ann.getPosition().Column),
        )
        comments = []
        for ann in ordered:
            pos = ann.getPosition()
            # Read text/date through the cell's own CellAnnotation (as the
            # write path does), not the XSheetAnnotations collection item,
            # whose getString()/getDate() come back empty in current LO.
            # Fall back to getAnnotationShape() for lazy .xlsx captions.
            cell_ann, text = _annotation_text(sheet, pos.Column, pos.Row)
            date_str = _annotation_date(cell_ann.getDate())
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
        return {"status": "ok", "comments": comments, "count": len(comments), "sheet": sheet.getName()}


class AddCellComment(ToolCalcCommentBase):
    """Add a comment to a cell."""

    name: str | None = "add_cell_comment"
    intent: str | None = "review"
    description: str = "Add a comment (annotation) to a specific cell in a Calc sheet."
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {"cell": {"type": "string", "description": "Cell address (e.g. 'B3' or 'Sheet1.B3')."}, "text": {"type": "string", "description": "Comment text."}, "sheet": {"type": "string", "description": "Sheet name (active sheet if omitted)."}},
        "required": ["cell", "text"],
    }
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        cell_ref = kwargs["cell"]
        text = kwargs["text"]
        if not cell_ref or not text:
            return self._tool_error("cell and text are required.")

        target = _resolve_comment_cell(self, ctx.doc, cell_ref, kwargs.get("sheet"))
        if isinstance(target, dict):
            return target
        sheet, col, row, cell_ref = target

        cell = sheet.getCellByPosition(col, row)

        cell_ann, existing_text = _annotation_text(sheet, col, row)
        if existing_text:
            cell_ann.setString(text)
        else:
            sheet.getAnnotations().insertNew(cell.getCellAddress(), text)

        return {"status": "ok", "cell": cell_ref, "text": text, "sheet": sheet.getName()}


class DeleteCellComment(ToolCalcCommentBase):
    """Delete a comment from a cell."""

    name: str | None = "delete_cell_comment"
    intent: str | None = "review"
    description: str = "Delete the comment (annotation) from a specific cell."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"cell": {"type": "string", "description": "Cell address (e.g. 'B3' or 'Sheet1.B3')."}, "sheet": {"type": "string", "description": "Sheet name (active sheet if omitted)."}}, "required": ["cell"]}
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        cell_ref = kwargs["cell"]
        if not cell_ref:
            return self._tool_error("cell is required.")

        target = _resolve_comment_cell(self, ctx.doc, cell_ref, kwargs.get("sheet"))
        if isinstance(target, dict):
            return target
        sheet, col, row, cell_ref = target

        annotations = sheet.getAnnotations()
        # Find and remove the annotation at this position
        for i in range(annotations.getCount()):
            ann = annotations.getByIndex(i)
            pos = ann.getPosition()
            if pos.Column == col and pos.Row == row:
                annotations.removeByIndex(i)
                return {"status": "ok", "cell": cell_ref, "message": "Comment deleted."}

        return self._tool_error("No comment found at %s." % cell_ref)
