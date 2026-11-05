# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Embedded OLE objects in Writer — specialized embedded domain."""

from __future__ import annotations

import logging
from typing import Any

from plugin.doc.undo import commit_or_rollback_mutation
from plugin.framework.errors import is_disposal, reraise_if_disposed, uno_read
from plugin.framework.tool import ToolBaseDummy
from plugin.framework.uno_context import create_property_value
from ..edit_review import next_agent_edit_undo_title
from ..specialized_base import ToolWriterEmbeddedBase, enum_param, target_params
from ..target_resolver import resolve_target_cursor

log = logging.getLogger("writeragent.embedded")

# Integer constants (PyUNO enums are ints; pytest has no com.sun.star.embed types).
# math_mml_convert.py:26 AnchorType AS_CHARACTER; math_insert.py:36 MSOLE_CONTENT aspect.
_ANCHOR_AS_CHARACTER = 1
_EMBED_STATE_RUNNING = 1
_EMBED_STATE_INPLACE_ACTIVE = 3
_EMBED_STATE_UI_ACTIVE = 4

_EMBED_FACTORY = "com.sun.star.embed.OOoEmbeddedObjectFactory"
_SPREADSHEET_DOC_SERVICE = "com.sun.star.sheet.SpreadsheetDocument"
_TEXT_EMBEDDED_OBJECT = "com.sun.star.text.TextEmbeddedObject"


def _spreadsheet_clsid(uno_ctx: Any) -> str:
    """Resolve the Calc spreadsheet OLE ClassID from LibreOffice embedding registry."""
    smgr = uno_ctx.getServiceManager()
    config_provider = smgr.createInstanceWithContext("com.sun.star.configuration.ConfigurationProvider", uno_ctx)
    node_names = create_property_value("nodepath", "/org.openoffice.Office.Embedding/ObjectNames")
    names_access = config_provider.createInstanceWithArguments(
        "com.sun.star.configuration.ConfigurationAccess",
        (node_names,),
    )
    class_id = str(names_access.getByName("Calc").ClassID)
    if not class_id:
        raise ValueError("spreadsheet embedding is not registered")

    node_objects = create_property_value("nodepath", "/org.openoffice.Office.Embedding/Objects")
    objects_access = config_provider.createInstanceWithArguments(
        "com.sun.star.configuration.ConfigurationAccess",
        (node_objects,),
    )
    class_key = class_id.lower()
    match_name: str | None = None
    for element_name in objects_access.getElementNames():
        if str(element_name).lower() == class_key:
            match_name = str(element_name)
            break
    if match_name is None:
        raise ValueError("spreadsheet embedding is not registered")

    entry = objects_access.getByName(match_name)
    factory = str(getattr(entry, "ObjectFactory", "") or "")
    doc_service = str(getattr(entry, "ObjectDocumentServiceName", "") or "")
    if factory != _EMBED_FACTORY or doc_service != _SPREADSHEET_DOC_SERVICE:
        raise ValueError("spreadsheet embedding is not registered")
    return class_id


def _write_cell(cell: Any, value: Any) -> None:
    if isinstance(value, bool):
        raise ValueError("boolean cell values are not supported")
    if value is None or value == "":
        return
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        cell.setValue(float(value))
        return
    if isinstance(value, str):
        stripped = value.lstrip()
        if stripped.startswith("="):
            cell.setFormula(stripped)
        else:
            cell.setString(value)
        return
    raise ValueError(f"unsupported cell value type: {type(value).__name__}")


def _deselect_inplace_embed(doc: Any, tool: Any) -> dict[str, Any] | None:
    """Leave in-place OLE editing before insert (avoids native crash on second embed)."""
    controller = uno_read(doc.getCurrentController, None)
    if controller is None:
        return None
    try:
        selection = controller.getSelection()
    except Exception as exc:
        reraise_if_disposed(exc)
        return tool._tool_error(f"Could not read the selection before insert ({exc}).")

    if not selection.supportsService(_TEXT_EMBEDDED_OBJECT):
        return None

    try:
        xe = selection.getExtendedControlOverEmbeddedObject()
        state = xe.getCurrentState()
        if state in (_EMBED_STATE_INPLACE_ACTIVE, _EMBED_STATE_UI_ACTIVE):
            xe.changeState(_EMBED_STATE_RUNNING)
        anchor = selection.getAnchor()
        anchor_text = anchor.getText()
        cursor = anchor_text.createTextCursorByRange(anchor.getEnd())
        controller.select(cursor)
    except Exception as exc:
        reraise_if_disposed(exc)
        return tool._tool_error(f"Could not leave in-place embedded edit before insert ({exc}).")
    return None


# Default Writer Calc embed visual area for A1:D5 (9033×2260 in 1/100 mm on typical builds).
_DEFAULT_EMBED_COLS = 4
_DEFAULT_EMBED_ROWS = 5
_DEFAULT_EMBED_WIDTH = 9033
_DEFAULT_EMBED_HEIGHT = 2260


def _embed_size_for_used_range(rows: int, cols: int) -> Any:
    import uno

    width = max(int(_DEFAULT_EMBED_WIDTH * cols / _DEFAULT_EMBED_COLS), 1000)
    height = max(int(_DEFAULT_EMBED_HEIGHT * rows / _DEFAULT_EMBED_ROWS), 500)
    return uno.createUnoStruct("com.sun.star.awt.Size", Width=width, Height=height)


def _embed_size_from_args(width: int, height: int) -> Any:
    import uno

    return uno.createUnoStruct("com.sun.star.awt.Size", Width=width, Height=height)


class EmbeddedInsert(ToolWriterEmbeddedBase):
    name: str | None = "embedded_insert"
    description: str = (
        "Insert a Calc spreadsheet OLE into the active Writer document (object_type='spreadsheet' only). "
        "Use target='beginning', 'end', 'selection', 'full_document', or 'search' with old_content. "
        "Optional data fills cells from A1; optional width and height are 1/100 mm (both or neither). "
        "Saving as .docx keeps cell values and formulas as an embedded xlsx but drops the object name "
        "and the exact visible width."
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "object_type": enum_param(["spreadsheet"], "Embedded object type (Calc spreadsheet only)."),
            "data": {
                "type": "array",
                "description": "Optional rows of cell values (string, number, or null). Default empty sheet.",
                "items": {
                    "type": "array",
                    "items": {"type": ["string", "number", "null"]},
                },
            },
            "name": {"type": "string", "description": "Optional container name in getEmbeddedObjects()."},
            "width": {"type": "integer", "description": "Frame width in 1/100 mm (requires height)."},
            "height": {"type": "integer", "description": "Frame height in 1/100 mm (requires width)."},
            **target_params("the embedded spreadsheet"),
        },
        "required": ["object_type"],
    }
    is_mutation: bool | None = True

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        object_type = kwargs.get("object_type")
        if object_type != "spreadsheet":
            return self._tool_error(
                "object_type must be 'spreadsheet'.",
                code="INVALID_PARAMETER",
                parameter="object_type",
            )

        data = kwargs.get("data")
        if data is None:
            data = []
        elif isinstance(data, bool) or not isinstance(data, list):
            return self._tool_error("data must be an array of rows.", code="INVALID_PARAMETER", parameter="data")
        for row_idx, row in enumerate(data):
            if isinstance(row, bool) or not isinstance(row, list):
                return self._tool_error(
                    f"data row {row_idx} must be an array.",
                    code="INVALID_PARAMETER",
                    parameter="data",
                )
            for col_idx, cell in enumerate(row):
                if isinstance(cell, bool):
                    return self._tool_error(
                        f"data[{row_idx}][{col_idx}] cannot be a boolean.",
                        code="INVALID_PARAMETER",
                        parameter="data",
                    )

        width = kwargs.get("width")
        height = kwargs.get("height")
        if (width is None) ^ (height is None):
            return self._tool_error(
                "width and height must both be set or both omitted.",
                code="INVALID_PARAMETER",
            )
        if width is not None and (not isinstance(width, int) or isinstance(width, bool) or width <= 0):
            return self._tool_error("width must be a positive integer.", code="INVALID_PARAMETER", parameter="width")
        if height is not None and (not isinstance(height, int) or isinstance(height, bool) or height <= 0):
            return self._tool_error("height must be a positive integer.", code="INVALID_PARAMETER", parameter="height")

        embed_name = kwargs.get("name")
        if embed_name is not None and embed_name == "":
            return self._tool_error("name cannot be empty.", code="INVALID_PARAMETER", parameter="name")

        target = kwargs.get("target", "selection")
        old_content = kwargs.get("old_content")

        try:
            cursor = resolve_target_cursor(ctx, target, old_content)
        except ValueError as ve:
            return self._tool_error(str(ve))

        deselect_err = _deselect_inplace_embed(ctx.doc, self)
        if deselect_err is not None:
            return deselect_err

        try:
            clsid = _spreadsheet_clsid(ctx.ctx)
        except ValueError as ve:
            return self._tool_error(str(ve), code="INVALID_PARAMETER")

        doc = ctx.doc
        replace_match = target == "search" and bool(old_content)
        result_holder: list[dict[str, Any]] = []

        def mutate() -> dict[str, Any] | None:
            if replace_match:
                cursor.setString("")
            text = cursor.getText()
            container = doc.getEmbeddedObjects()
            before_names = set(container.getElementNames())
            if embed_name and container.hasByName(embed_name):
                return {
                    "status": "error",
                    "code": "INVALID_PARAMETER",
                    "message": f"Embedded object {embed_name!r} already exists.",
                }

            emb = doc.createInstance("com.sun.star.text.TextEmbeddedObject")
            if not emb:
                return {"status": "error", "message": "Failed to create TextEmbeddedObject."}

            emb.CLSID = clsid
            emb.AnchorType = _ANCHOR_AS_CHARACTER
            if embed_name:
                emb.Name = embed_name

            text.insertTextContent(cursor, emb, False)

            after_names = set(container.getElementNames())
            new_names = after_names - before_names
            if len(new_names) != 1:
                return {
                    "status": "error",
                    "message": "Spreadsheet OLE was not installed in the document.",
                }

            container_name = next(iter(new_names))

            try:
                inner = emb.getEmbeddedObject()
            except Exception as exc:
                if is_disposal(exc):
                    raise
                return {
                    "status": "error",
                    "message": f"Could not open embedded spreadsheet ({exc}).",
                }

            if inner is None or not inner.supportsService(_SPREADSHEET_DOC_SERVICE):
                return {"status": "error", "message": "Embedded object is not a Calc spreadsheet."}

            sheet = inner.getSheets().getByIndex(0)
            rows = len(data)
            cols = max((len(row) for row in data), default=0)

            size = None
            if width is not None and height is not None:
                size = _embed_size_from_args(width, height)
            elif rows > 0 and cols > 0:
                size = _embed_size_for_used_range(rows, cols)

            read_w = read_h = None
            if size is not None:
                # Direct host size: XEmbeddedObject setVisualAreaSize/getVisualAreaSize
                # blocked for 30s+ on headless CI after Calc OLE insert.
                emb.Width = size.Width
                emb.Height = size.Height
                read_w = size.Width
                read_h = size.Height

            for r_idx, row in enumerate(data):
                for c_idx, cell_val in enumerate(row):
                    try:
                        _write_cell(sheet.getCellByPosition(c_idx, r_idx), cell_val)
                    except ValueError as ve:
                        return {"status": "error", "message": str(ve)}

            result_holder.append(
                {
                    "status": "ok",
                    "name": container_name,
                    "object_type": "spreadsheet",
                    "rows": rows,
                    "cols": cols,
                    "width": read_w,
                    "height": read_h,
                    "message": f"Inserted spreadsheet OLE {container_name!r}.",
                }
            )
            return None

        error = commit_or_rollback_mutation(
            self,
            doc,
            next_agent_edit_undo_title(),
            mutate,
            "Cannot insert the spreadsheet OLE atomically (no usable undo context).",
            "Failed to insert spreadsheet OLE; the change was rolled back",
        )
        if error is not None:
            return error
        return result_holder[0]


class EmbeddedEdit(ToolWriterEmbeddedBase, ToolBaseDummy):  # type: ignore[misc]  # pyright: ignore[reportIncompatibleVariableOverride]
    name: str | None = "embedded_edit"
    description: str = "Activate or edit an embedded OLE object (planned)."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"name": {"type": "string", "description": "Object name or anchor hint when available."}}, "required": []}
    is_mutation: bool | None = True

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        return self._tool_error("embedded_edit is not implemented yet. Double-click the object in Writer.")
