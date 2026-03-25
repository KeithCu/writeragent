# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Writer text fields — specialized fields domain."""

from plugin.modules.writer.base import ToolWriterFieldBase


class FieldsUpdateAll(ToolWriterFieldBase):
    name = "fields_update_all"
    intent = "navigate"
    description = (
        "Refresh all text fields (dates, page numbers, cross-references). "
        "Call after changes that affect computed fields."
    )
    parameters = {"type": "object", "properties": {}, "required": []}
    uno_services = ["com.sun.star.text.TextDocument"]
    is_mutation = True

    def execute(self, ctx, **kwargs):
        doc = ctx.doc
        if not hasattr(doc, "getTextFields"):
            return self._tool_error("Document does not support text fields.")
        fields = doc.getTextFields()
        fields.refresh()

        enum = fields.createEnumeration()
        count = 0
        while enum.hasMoreElements():
            enum.nextElement()
            count += 1

        return {"status": "ok", "fields_refreshed": count}


class UpdateFieldsAlias(ToolWriterFieldBase):
    """Legacy name for fields_update_all."""

    name = "update_fields"
    intent = "navigate"
    description = "Refresh all text fields. Alias of fields_update_all."
    parameters = {"type": "object", "properties": {}, "required": []}
    uno_services = ["com.sun.star.text.TextDocument"]
    is_mutation = True

    def execute(self, ctx, **kwargs):
        doc = ctx.doc
        if not hasattr(doc, "getTextFields"):
            return self._tool_error("Document does not support text fields.")
        fields = doc.getTextFields()
        fields.refresh()
        enum = fields.createEnumeration()
        count = 0
        while enum.hasMoreElements():
            enum.nextElement()
            count += 1
        return {"status": "ok", "fields_refreshed": count}


class FieldsInsert(ToolWriterFieldBase):
    name = "fields_insert"
    intent = "edit"
    description = (
        "Insert a text field (page number, date, cross-ref). "
        "Reserved for future UNO implementation (masters + dependent fields)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "field_type": {
                "type": "string",
                "description": "Intended field kind (e.g. page, date, cross_ref).",
            },
        },
        "required": [],
    }
    uno_services = ["com.sun.star.text.TextDocument"]
    is_mutation = True

    def execute(self, ctx, **kwargs):
        return self._tool_error(
            "fields_insert is not implemented yet. Insert fields via LibreOffice "
            "Insert > Field, then call fields_update_all."
        )
