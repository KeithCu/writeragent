# Copyright (c) David Berlioz
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Calc cell annotation (comment) tools."""

import logging

from plugin.framework.tool_base import ToolBase
from plugin.modules.calc.address_utils import (
    index_to_column,
    parse_range_string,
)

log = logging.getLogger("nelson.calc")


def _resolve_sheet(doc, sheet_name=None):
    """Return the target sheet (by name or active)."""
    if sheet_name:
        sheets = doc.getSheets()
        if not sheets.hasByName(sheet_name):
            raise ValueError("Sheet not found: %s" % sheet_name)
        return sheets.getByName(sheet_name)
    controller = doc.getCurrentController()
    if hasattr(controller, "getActiveSheet"):
        return controller.getActiveSheet()
    return doc.getSheets().getByIndex(0)


def _cell_label(col, row):
    return "%s%d" % (index_to_column(col), row + 1)


def _parse_cell_ref(cell_ref):
    """Parse 'B3' into (col, row) 0-based tuple."""
    (col, row), _ = parse_range_string(cell_ref)
    return col, row


class ListCellComments(ToolBase):
    """List all cell comments/annotations in a sheet."""

    name = "list_cell_comments"
    intent = "review"
    description = (
        "List all cell comments (annotations) in a Calc sheet. "
        "Returns cell address, author, date, and comment text."
    )
    parameters = {
        "type": "object",
        "properties": {
            "sheet_name": {
                "type": "string",
                "description": "Sheet name (active sheet if omitted).",
            },
        },
        "required": [],
    }
    doc_types = ["calc"]

    def execute(self, ctx, **kwargs):
        doc = ctx.doc
        try:
            sheet = _resolve_sheet(doc, kwargs.get("sheet_name"))
            annotations = sheet.getAnnotations()
            comments = []
            for i in range(annotations.getCount()):
                ann = annotations.getByIndex(i)
                pos = ann.getPosition()
                comments.append({
                    "cell": _cell_label(pos.Column, pos.Row),
                    "author": ann.getAuthor(),
                    "date": ann.getDate(),
                    "text": ann.getString(),
                    "is_visible": ann.getIsVisible(),
                })
            return {
                "status": "ok",
                "comments": comments,
                "count": len(comments),
                "sheet": sheet.getName(),
            }
        except Exception as e:
            return self._tool_error(str(e))


class AddCellComment(ToolBase):
    """Add a comment to a cell."""

    name = "add_cell_comment"
    intent = "review"
    description = (
        "Add a comment (annotation) to a specific cell in a Calc sheet."
    )
    parameters = {
        "type": "object",
        "properties": {
            "cell": {
                "type": "string",
                "description": "Cell address (e.g. 'B3').",
            },
            "text": {
                "type": "string",
                "description": "Comment text.",
            },
            "sheet_name": {
                "type": "string",
                "description": "Sheet name (active sheet if omitted).",
            },
        },
        "required": ["cell", "text"],
    }
    doc_types = ["calc"]
    is_mutation = True

    def execute(self, ctx, **kwargs):
        cell_ref = kwargs.get("cell", "")
        text = kwargs.get("text", "")
        if not cell_ref or not text:
            return self._tool_error("cell and text are required.")

        doc = ctx.doc
        try:
            sheet = _resolve_sheet(doc, kwargs.get("sheet_name"))
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

            return {
                "status": "ok",
                "cell": cell_ref,
                "text": text,
                "sheet": sheet.getName(),
            }
        except Exception as e:
            return self._tool_error(str(e))


class DeleteCellComment(ToolBase):
    """Delete a comment from a cell."""

    name = "delete_cell_comment"
    intent = "review"
    description = "Delete the comment (annotation) from a specific cell."
    parameters = {
        "type": "object",
        "properties": {
            "cell": {
                "type": "string",
                "description": "Cell address (e.g. 'B3').",
            },
            "sheet_name": {
                "type": "string",
                "description": "Sheet name (active sheet if omitted).",
            },
        },
        "required": ["cell"],
    }
    doc_types = ["calc"]
    is_mutation = True

    def execute(self, ctx, **kwargs):
        cell_ref = kwargs.get("cell", "")
        if not cell_ref:
            return self._tool_error("cell is required.")

        doc = ctx.doc
        try:
            sheet = _resolve_sheet(doc, kwargs.get("sheet_name"))
            col, row = _parse_cell_ref(cell_ref)

            annotations = sheet.getAnnotations()
            # Find and remove the annotation at this position
            for i in range(annotations.getCount()):
                ann = annotations.getByIndex(i)
                pos = ann.getPosition()
                if pos.Column == col and pos.Row == row:
                    annotations.removeByIndex(i)
                    return {
                        "status": "ok",
                        "cell": cell_ref,
                        "message": "Comment deleted.",
                    }

            return self._tool_error("No comment found at %s." % cell_ref)
        except Exception as e:
            return self._tool_error(str(e))
