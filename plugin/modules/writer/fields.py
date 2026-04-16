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
from plugin.modules.writer.target_resolver import resolve_target_cursor



class FieldsUpdateAll(ToolWriterFieldBase):
    name = "fields_update_all"
    intent = "navigate"
    description = (
        "Refresh all text fields (dates, page numbers, cross-references). "
        "Call after changes that affect computed fields."
    )
    parameters = {"type": "object", "properties": {}, "required": []}
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


class FieldsList(ToolWriterFieldBase):
    name = "fields_list"
    intent = "examine"
    description = (
        "List all text fields in the document. Returns their types and text content, "
        "allowing you to identify and inspect fields like page numbers or dates."
    )
    parameters = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    def execute(self, ctx, **kwargs):
        doc = ctx.doc
        if not hasattr(doc, "getTextFields"):
            return self._tool_error("Document does not support text fields.")

        fields = doc.getTextFields()
        enum = fields.createEnumeration()

        results = []
        count = 0
        while enum.hasMoreElements():
            field = enum.nextElement()
            count += 1

            # Use getPresentation to get human-readable info
            try:
                presentation = field.getPresentation(False)
            except Exception:
                presentation = str(field)

            try:
                content = field.getPresentation(True)
            except Exception:
                content = ""

            # Try to get the specific field type by checking supported services
            # since we know fields start with com.sun.star.text.textfield.
            # However, LibreOffice API does not have an easy introspection for this.
            # Getting the PropertySetInfo is the best way to extract properties.
            props = {}
            if hasattr(field, "getPropertySetInfo"):
                try:
                    for prop in field.getPropertySetInfo().getProperties():
                        try:
                            # Avoid extracting complex types
                            val = field.getPropertyValue(prop.Name)
                            if isinstance(val, (int, float, str, bool)):
                                props[prop.Name] = val
                        except Exception:
                            pass
                except Exception:
                    pass

            results.append({
                "id": count,
                "presentation": presentation,
                "content": content,
                "properties": props
            })

        return {
            "status": "ok",
            "field_count": count,
            "fields": results
        }


class FieldsDelete(ToolWriterFieldBase):
    name = "fields_delete"
    intent = "edit"
    description = (
        "Deletes one or more text fields from the document by their 1-based ID. "
        "Use fields_list first to obtain the IDs of the fields you wish to remove."
    )
    parameters = {
        "type": "object",
        "properties": {
            "ids": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "A list of 1-based IDs representing the text fields to delete.",
            },
        },
        "required": ["ids"],
    }
    is_mutation = True

    def execute(self, ctx, **kwargs):
        ids = kwargs.get("ids")
        doc = ctx.doc
        if not hasattr(doc, "getTextFields"):
            return self._tool_error("Document does not support text fields.")

        fields = doc.getTextFields()
        enum = fields.createEnumeration()

        fields_to_delete = []
        count = 0
        while enum.hasMoreElements():
            field = enum.nextElement()
            count += 1
            if ids and count in ids:
                fields_to_delete.append(field)

        if not fields_to_delete:
            return {"status": "ok", "deleted_count": 0, "message": "No matching fields found to delete."}

        deleted_count = 0
        for field in fields_to_delete:
            try:
                # Text fields can be deleted by removing their anchor content
                anchor = field.getAnchor()
                anchor.setString("")
                deleted_count += 1
            except Exception as e:
                return self._tool_error(f"Failed to delete field: {str(e)}")

        return {
            "status": "ok",
            "message": f"Successfully deleted {deleted_count} field(s).",
            "deleted_count": deleted_count
        }


class FieldsInsert(ToolWriterFieldBase):
    name = "fields_insert"
    intent = "edit"
    description = (
        "Insert a text field at the specified target position. "
        "Use target='beginning', 'end', or 'selection' to insert at those positions. "
        "Use target='search' with old_content to find and replace text. "
        "Supports various field types natively provided by LibreOffice. Common field types include: "
        "'PageNumber', 'PageCount', 'DateTime', 'Author', 'FileName', 'WordCount', "
        "'CharacterCount', 'ParagraphCount', 'TableCount', 'GraphicObjectCount', "
        "'EmbeddedObjectCount', and 'Annotation'. Specify optional properties "
        "to configure the field."
    )
    parameters = {
        "type": "object",
        "properties": {
            "field_type": {
                "type": "string",
                "description": (
                    "The exact name of the text field service to create, excluding the "
                    "'com.sun.star.text.textfield.' prefix. Examples: 'PageNumber', "
                    "'PageCount', 'DateTime', 'Author', 'FileName', 'WordCount'."
                ),
            },
            "properties": {
                "type": "object",
                "description": (
                    "Optional dictionary of UNO properties to apply to the field. "
                    "Example: {'NumberingType': 4} for Arabic numbering, or "
                    "{'IsDate': true} to force a date display on a DateTime field."
                ),
                "default": {},
            },
            "target": {
                "type": "string",
                "enum": ["beginning", "end", "selection", "full_document", "search"],
                "description": "Where to insert the field.",
            },
            "old_content": {
                "type": "string",
                "description": "Text to find and replace if target = 'search'.",
            },
        },
        "required": ["field_type"],
    }
    is_mutation = True

    def execute(self, ctx, **kwargs):
        field_type = kwargs.get("field_type")
        properties = kwargs.get("properties")
        doc = ctx.doc
        if not hasattr(doc, "createInstance"):
            return self._tool_error("Document does not support creating instances.")

        target = kwargs.get("target", "selection")
        old_content = kwargs.get("old_content")

        try:
            cursor = resolve_target_cursor(ctx, target, old_content)
        except ValueError as ve:
            return self._tool_error(str(ve))

        if not cursor:
            return self._tool_error("Failed to resolve target location.")

        properties = properties or {}
        full_service_name = f"com.sun.star.text.textfield.{field_type}"

        try:
            field = doc.createInstance(full_service_name)
            if not field:
                return self._tool_error(f"Failed to create field of type '{field_type}'.")
        except Exception as e:
            return self._tool_error(f"Error creating field '{field_type}': {str(e)}")

        # Apply properties
        for key, value in properties.items():
            try:
                field.setPropertyValue(key, value)
            except Exception as e:
                return self._tool_error(f"Failed to set property '{key}' to '{value}': {str(e)}")

        if target == "search" and old_content:
            cursor.setString("")

        # Insert at resolved cursor
        try:
            text = cursor.getText()
            text.insertTextContent(cursor, field, False)
        except Exception as e:
            return self._tool_error(f"Failed to insert field into document: {str(e)}")

        return {
            "status": "ok",
            "message": f"Successfully inserted {field_type} field.",
            "applied_properties": properties
        }
