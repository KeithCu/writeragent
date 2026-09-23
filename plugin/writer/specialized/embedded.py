# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Embedded OLE objects in Writer — specialized embedded domain."""

from typing import Any

from ..specialized_base import ToolWriterEmbeddedBase
from ..target_resolver import resolve_target_cursor


class EmbeddedInsert(ToolWriterEmbeddedBase):
    name: str | None = "embedded_insert"
    description: str = "Insert an embedded object (e.g. Calc spreadsheet) into the document. Use target='beginning', 'end', or 'selection' to insert at those positions. Use target='search' with old_content to find and replace text. Planned: CLSID-based insert + in-place activation."
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "object_type": {"type": "string", "description": "Target type, e.g. spreadsheet, chart."},
            "target": {"type": "string", "enum": ["beginning", "end", "selection", "full_document", "search"], "description": "Where to insert the embedded object."},
            "old_content": {"type": "string", "description": "Text to find and replace if target = 'search'."},
        },
        "required": [],
    }
    is_mutation: bool | None = True

    def execute(self, ctx: Any, **kwargs: Any):
        target = kwargs.get("target", "selection")
        old_content = kwargs.get("old_content")

        try:
            cursor = resolve_target_cursor(ctx, target, old_content)
        except ValueError as ve:
            return self._tool_error(str(ve))

        if not cursor:
            return self._tool_error("Failed to resolve target location.")

        return self._tool_error("embedded_insert is not implemented yet. Use Insert > Object > OLE Object in LibreOffice Writer.")


class EmbeddedEdit(ToolWriterEmbeddedBase):
    name: str | None = "embedded_edit"
    description: str = "Activate or edit an embedded OLE object (planned)."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"name": {"type": "string", "description": "Object name or anchor hint when available."}}, "required": []}
    is_mutation: bool | None = True

    def execute(self, ctx: Any, **kwargs: Any):
        return self._tool_error("embedded_edit is not implemented yet. Double-click the object in Writer.")
