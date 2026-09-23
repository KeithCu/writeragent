# Copyright (c) David Berlioz
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Calc cell annotation (comment) tools."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from plugin.calc.base import ToolCalcCommentBase
from plugin.calc.address_utils import format_address, parse_address, split_sheet_prefix
from plugin.calc.calc_utils import resolve_sheet

if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext

log = logging.getLogger("writeragent.calc")


def _cell_label(col: int, row: int) -> str:
    return format_address(col, row)


def _parse_cell_ref(cell_ref: str) -> tuple[int, int]:
    """Parse 'B3' into (col, row) 0-based tuple."""
    return parse_address(cell_ref)



def _split_cell_sheet(cell_ref: str, sheet_name: str | None):
    """Let a sheet-qualified cell reference pick the sheet."""
    prefix, address = split_sheet_prefix(cell_ref)
    if prefix is not None and sheet_name and prefix != sheet_name:
        raise ValueError(
            "Reference names sheet '%s' but sheet_name says '%s' — "
            "pass one or the other." % (prefix, sheet_name)
        )
    return address, (prefix or sheet_name)


def _annotation_text(sheet: Any, col: int, row: int):
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
    text = ""
    try:
        text = cell_ann.getString()
    except Exception:
        pass
    if text:
        return cell_ann, text
    try:
        shape = cell_ann.getAnnotationShape()
        if shape is not None:
            text = shape.getString() or ""
    except Exception:
        pass  # optional interface — older builds may not offer it
    return cell_ann, text


class ListCellComments(ToolCalcCommentBase):
    """List all cell comments/annotations in a sheet."""

    name: str | None = "list_cell_comments"
    intent: str | None = "review"
    description: str = "List all cell comments (annotations) in a Calc sheet. Returns cell address, author, date, and comment text."
    parameters: dict | None = {
        "type": "object",
        "properties": {
            "sheet": {
                "type": "string",
                "description": "Sheet name (active sheet if omitted).",
            }
        },
        "required": [],
    }

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        doc = ctx.doc
        sheet = resolve_sheet(doc, kwargs.get("sheet"))
        annotations = sheet.getAnnotations()
        comments = []
        for i in range(annotations.getCount()):
            ann = annotations.getByIndex(i)
            pos = ann.getPosition()
            # Read text/date through the cell's own CellAnnotation (as the
            # write path does), not the XSheetAnnotations collection item,
            # whose getString()/getDate() come back empty in current LO.
            # Fall back to getAnnotationShape() for lazy .xlsx captions.
            cell_ann, text = _annotation_text(sheet, pos.Column, pos.Row)
            comments.append(
                {
                    "cell": _cell_label(pos.Column, pos.Row),
                    "author": ann.getAuthor(),
                    # .xlsx has no date field on a comment element, so an
                    # empty date there is the format, not a failure.
                    "date": cell_ann.getDate(),
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
    parameters: dict | None = {
        "type": "object",
        "properties": {
            "cell": {
                "type": "string",
                "description": "Cell address (e.g. 'B3' or 'Sheet1.B3').",
            },
            "text": {"type": "string", "description": "Comment text."},
            "sheet": {
                "type": "string",
                "description": "Sheet name (active sheet if omitted).",
            },
        },
        "required": ["cell", "text"],
    }
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        cell_ref = kwargs.get("cell", "")
        text = kwargs.get("text", "")
        if not cell_ref or not text:
            return self._tool_error("cell and text are required.")

        try:
            cell_ref, sheet_name = _split_cell_sheet(cell_ref, kwargs.get("sheet"))
        except ValueError as e:
            return self._tool_error(str(e))

        doc = ctx.doc
        sheet = resolve_sheet(doc, sheet_name)
        col, row = _parse_cell_ref(cell_ref)
        cell = sheet.getCellByPosition(col, row)

        # Insert or update annotation
        from com.sun.star.table import CellAddress

        addr = CellAddress()
        addr.Sheet = sheet.getRangeAddress().Sheet
        addr.Column = col
        addr.Row = row

        annotations = sheet.getAnnotations()
        # Check if annotation already exists
        ann = cell.getAnnotation()
        if ann and ann.getString():
            ann.setString(text)
        else:
            annotations.insertNew(addr, text)

        return {"status": "ok", "cell": cell_ref, "text": text, "sheet": sheet.getName()}


class DeleteCellComment(ToolCalcCommentBase):
    """Delete a comment from a cell."""

    name: str | None = "delete_cell_comment"
    intent: str | None = "review"
    description: str = "Delete the comment (annotation) from a specific cell."
    parameters: dict | None = {
        "type": "object",
        "properties": {
            "cell": {
                "type": "string",
                "description": "Cell address (e.g. 'B3' or 'Sheet1.B3').",
            },
            "sheet": {
                "type": "string",
                "description": "Sheet name (active sheet if omitted).",
            },
        },
        "required": ["cell"],
    }
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        cell_ref = kwargs.get("cell", "")
        if not cell_ref:
            return self._tool_error("cell is required.")

        try:
            cell_ref, sheet_name = _split_cell_sheet(cell_ref, kwargs.get("sheet"))
        except ValueError as e:
            return self._tool_error(str(e))

        doc = ctx.doc
        sheet = resolve_sheet(doc, sheet_name)
        col, row = _parse_cell_ref(cell_ref)

        annotations = sheet.getAnnotations()
        # Find and remove the annotation at this position
        for i in range(annotations.getCount()):
            ann = annotations.getByIndex(i)
            pos = ann.getPosition()
            if pos.Column == col and pos.Row == row:
                annotations.removeByIndex(i)
                return {"status": "ok", "cell": cell_ref, "message": "Comment deleted."}

        return self._tool_error("No comment found at %s." % cell_ref)
