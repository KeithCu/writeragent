# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Embedded OLE objects in Writer — specialized embedded domain."""

from plugin.modules.writer.base import ToolWriterEmbeddedBase


class EmbeddedInsert(ToolWriterEmbeddedBase):
    name = "embedded_insert"
    intent = "edit"
    description = (
        "Insert an embedded object (e.g. Calc spreadsheet) into the document. "
        "Planned: CLSID-based insert + in-place activation."
    )
    parameters = {
        "type": "object",
        "properties": {
            "object_type": {
                "type": "string",
                "description": "Target type, e.g. spreadsheet, chart.",
            },
        },
        "required": [],
    }
    uno_services = ["com.sun.star.text.TextDocument"]
    is_mutation = True

    def execute(self, ctx, **kwargs):
        return self._tool_error(
            "embedded_insert is not implemented yet. Use Insert > Object > OLE Object "
            "in LibreOffice Writer."
        )


class EmbeddedEdit(ToolWriterEmbeddedBase):
    name = "embedded_edit"
    intent = "edit"
    description = "Activate or edit an embedded OLE object (planned)."
    parameters = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Object name or anchor hint when available.",
            },
        },
        "required": [],
    }
    uno_services = ["com.sun.star.text.TextDocument"]
    is_mutation = True

    def execute(self, ctx, **kwargs):
        return self._tool_error(
            "embedded_edit is not implemented yet. Double-click the object in Writer."
        )
