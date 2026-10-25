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

from plugin.framework.tool import ToolBaseDummy
from ..specialized_base import ToolWriterEmbeddedBase, target_params


# B3-A3: the embedded domain is out of specialist routing until OLE insert is real.
# ToolBaseDummy keeps both stubs out of the registry but keeps the code and schema.
# Dropping the `embedded` domain from the delegate enum needs ToolWriterEmbeddedBase
# in plugin/writer/specialized_base.py (owned by K212); this job must not edit that file.
class EmbeddedInsert(ToolWriterEmbeddedBase, ToolBaseDummy):  # type: ignore[misc]  # pyright: ignore[reportIncompatibleVariableOverride]
    name: str | None = "embedded_insert"
    description: str = "Insert an embedded object (e.g. Calc spreadsheet) into the document. Use target='beginning', 'end', or 'selection' to insert at those positions. Use target='search' with old_content to find and replace text. Planned: CLSID-based insert + in-place activation."
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "object_type": {"type": "string", "description": "Target type, e.g. spreadsheet, chart."},
            **target_params("the embedded object"),
        },
        "required": [],
    }
    is_mutation: bool | None = True

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        return self._tool_error("embedded_insert is not implemented yet. Use Insert > Object > OLE Object in LibreOffice Writer.")


class EmbeddedEdit(ToolWriterEmbeddedBase, ToolBaseDummy):  # type: ignore[misc]  # pyright: ignore[reportIncompatibleVariableOverride]
    name: str | None = "embedded_edit"
    description: str = "Activate or edit an embedded OLE object (planned)."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"name": {"type": "string", "description": "Object name or anchor hint when available."}}, "required": []}
    is_mutation: bool | None = True

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        return self._tool_error("embedded_edit is not implemented yet. Double-click the object in Writer.")
