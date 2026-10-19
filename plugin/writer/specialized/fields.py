# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Writer text fields — specialized fields domain."""

import hashlib
from typing import Any

from plugin.framework.errors import is_real_disposal

from ..specialized_base import ToolWriterFieldBase
from ..target_resolver import resolve_target_cursor


KNOWN_PROPERTIES = (
    "NumberingType", "IsDate", "SubType", "Format", "IsFixed", "DateTimeValue",
    "Content", "Author", "Level", "Offset", "Condition",
    "Placeholder", "Hint", "VariableName", "Value", "SequenceValue", "URL",
    "TargetFrame", "Name", "Representation", "Formula", "NumberFormat",
    "IsVisible", "Title", "Subject", "Initials", "SourceName",
    "ReferenceFieldPart", "ChapterFormat", "UserDataType",
    "DataBaseName", "DataTableName", "DataColumnName",
)


def _is_annotation(field: Any) -> bool:
    try:
        return field.supportsService("com.sun.star.text.textfield.Annotation") is True
    except Exception as e:
        if is_real_disposal(e):
            raise
        return False


def _specific_field_service(field: Any) -> str:
    """Return the most specific com.sun.star.text.textfield.* service name."""
    try:
        services = field.getSupportedServiceNames()
    except Exception:
        services = ()
    candidates = [
        s for s in services
        if isinstance(s, str) and s.startswith("com.sun.star.text.textfield.")
    ]
    narrower = [s for s in candidates if s != "com.sun.star.text.textfield.TextField"]
    if narrower:
        return sorted(narrower)[0]
    if candidates:
        return candidates[0]
    return ""


def _compute_document_fields_snapshot(doc: Any) -> str:
    from ..search import describe_match_location

    fields = doc.getTextFields()
    enum = fields.createEnumeration()
    hf_labels: dict[int, str] = {}
    hasher = hashlib.sha256()
    while enum.hasMoreElements():
        field = enum.nextElement()
        if _is_annotation(field):
            continue
        service = _specific_field_service(field)
        try:
            pres = field.getPresentation(False)
        except Exception:
            pres = ""
        try:
            loc = describe_match_location(field.getAnchor(), doc, hf_labels)
        except Exception:
            loc = "unknown"
        hasher.update(f"{service}|{pres}|{loc}\n".encode("utf-8"))
    return hasher.hexdigest()


class FieldsUpdateAll(ToolWriterFieldBase):
    name: str | None = "fields_update_all"
    intent: str | None = "navigate"
    description: str = "Refresh all text fields (dates, page numbers, cross-references). Call after changes that affect computed fields."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {}, "required": []}
    is_mutation: bool | None = True

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        doc = ctx.doc
        fields = doc.getTextFields()
        fields.refresh()

        enum = fields.createEnumeration()
        count = 0
        while enum.hasMoreElements():
            enum.nextElement()
            count += 1

        return {"status": "ok", "fields_refreshed": count}


class FieldsList(ToolWriterFieldBase):
    name: str | None = "fields_list"
    intent: str | None = "examine"
    description: str = "List all text fields in the document. Returns their types and text content, allowing you to identify and inspect fields like page numbers or dates."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {}, "required": []}

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        doc = ctx.doc

        # Deferred: plugin.writer.search pulls the UNO document helpers in, and this module is
        # imported wherever the field tools are registered.
        from ..search import describe_match_location

        fields = doc.getTextFields()
        enum = fields.createEnumeration()

        results = []
        hf_labels: dict[int, str] = {}
        hasher = hashlib.sha256()
        count = 0
        while enum.hasMoreElements():
            field = enum.nextElement()
            if _is_annotation(field):
                continue
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

            props = {}
            if hasattr(field, "getPropertySetInfo"):
                try:
                    info = field.getPropertySetInfo()
                    for prop_name in KNOWN_PROPERTIES:
                        try:
                            if info.hasPropertyByName(prop_name):
                                val = field.getPropertyValue(prop_name)
                                if isinstance(val, (int, float, str, bool)):
                                    props[prop_name] = val
                        except Exception:
                            pass
                except Exception:
                    pass

            # Where the field lives decides whether deleting it is safe: getTextFields() returns
            # header and footer fields mixed in with the body's, and a page number in a footer
            # looks exactly like one in the text. Same labels the search path reports.
            try:
                location = describe_match_location(field.getAnchor(), doc, hf_labels)
            except Exception:
                location = "unknown"

            service = _specific_field_service(field)
            hasher.update(f"{service}|{presentation}|{location}\n".encode("utf-8"))

            results.append({"id": count, "presentation": presentation, "content": content,
                            "location": location, "properties": props})

        return {"status": "ok", "field_count": count, "snapshot": hasher.hexdigest(), "fields": results}


class FieldsDelete(ToolWriterFieldBase):
    name: str | None = "fields_delete"
    intent: str | None = "edit"
    description: str = (
        "Deletes one or more text fields from the document by their 1-based ID in the fields_list snapshot. "
        "Pass the snapshot string from fields_list to ensure IDs have not shifted."
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "ids": {"type": "array", "items": {"type": "integer"}, "description": "A list of 1-based IDs representing the text fields to delete."},
            "snapshot": {"type": "string", "description": "Optional snapshot hash from fields_list to prevent deleting shifted fields."},
        },
        "required": ["ids"],
    }
    is_mutation: bool | None = True

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        snapshot = kwargs.get("snapshot")
        raw_ids = kwargs.get("ids")
        if not isinstance(raw_ids, list):
            return self._tool_error("Parameter 'ids' must be a list of integers.")

        cleaned_ids: list[int] = []
        seen = set()
        for item in raw_ids:
            if isinstance(item, bool) or not isinstance(item, int):
                return self._tool_error("Parameter 'ids' must contain only integers (no booleans).")
            if item not in seen:
                seen.add(item)
                cleaned_ids.append(item)

        requested_count = len(cleaned_ids)
        if requested_count == 0:
            return self._tool_error("No field IDs provided.", requested_count=0, missing_ids=[])

        doc = ctx.doc
        if snapshot is not None:
            current_snapshot = _compute_document_fields_snapshot(doc)
            if current_snapshot != snapshot:
                return {
                    "status": "error",
                    "code": "FIELD_LIST_STALE",
                    "message": "Document fields have changed since fields_list. Call fields_list again.",
                    "deleted_count": 0,
                }
        from ..search import describe_match_location  # deferred, see FieldsList.execute

        fields = doc.getTextFields()
        enum = fields.createEnumeration()

        fields_to_delete: list[tuple[int, Any]] = []
        hf_labels: dict[int, str] = {}
        count = 0
        while enum.hasMoreElements():
            field = enum.nextElement()
            if _is_annotation(field):
                continue
            count += 1
            if count in seen:
                fields_to_delete.append((count, field))

        found_ids = {c for c, _ in fields_to_delete}
        missing_ids = [fid for fid in cleaned_ids if fid not in found_ids]

        if not fields_to_delete:
            return {
                "status": "error",
                "code": "FIELD_NOT_FOUND",
                "message": f"No matching text fields found to delete (missing IDs: {missing_ids}). Use comment_delete to delete comments.",
                "deleted_count": 0,
                "requested_count": requested_count,
                "missing_ids": missing_ids,
            }

        deleted_count = 0
        deleted = []
        for _cid, field in fields_to_delete:
            anchor = field.getAnchor()
            try:
                # Report the region before the anchor is emptied: a page number removed from a
                # footer and one removed from the body read identically afterwards.
                where = describe_match_location(anchor, doc, hf_labels)
            except Exception:
                where = "unknown"
            try:
                what = field.getPresentation(True)
            except Exception:
                what = ""
            # setString("") on the anchor removes only the field (same as footnotes_delete); see test_fields_uno.py.
            anchor.setString("")
            deleted_count += 1
            deleted.append({"content": what, "location": where})

        return {
            "status": "ok",
            "message": f"Successfully deleted {deleted_count} field(s).",
            "deleted_count": deleted_count,
            "requested_count": requested_count,
            "missing_ids": missing_ids,
            "deleted": deleted,
        }


class FieldsInsert(ToolWriterFieldBase):
    name: str | None = "fields_insert"
    intent: str | None = "edit"
    description: str = (
        "Insert a text field at the specified target position. "
        "Use target='beginning', 'end', or 'selection' to insert at those positions. "
        "Use target='search' with old_content to find and replace text. "
        "Supports various field types natively provided by LibreOffice. Common field types include: "
        "'PageNumber', 'PageCount', 'DateTime', 'Author', 'FileName', 'WordCount', "
        "'CharacterCount', 'ParagraphCount', 'TableCount', 'GraphicObjectCount', "
        "'EmbeddedObjectCount', and 'Annotation'. Specify optional properties "
        "to configure the field."
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "field": {"type": "string", "description": ("The exact name of the text field service to create, excluding the 'com.sun.star.text.textfield.' prefix. Examples: 'PageNumber', 'PageCount', 'DateTime', 'Author', 'FileName', 'WordCount'.")},
            "properties": {"type": "object", "description": ("Optional dictionary of UNO properties to apply to the field. Example: {'NumberingType': 4} for Arabic numbering, or {'IsDate': true} to force a date display on a DateTime field."), "default": {}},
            "target": {"type": "string", "enum": ["beginning", "end", "selection", "full_document", "search"], "description": "Where to insert the field."},
            "old_content": {"type": "string", "description": "Text to find and replace if target = 'search'."},
        },
        "required": ["field"],
    }
    is_mutation: bool | None = True

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        field_type = kwargs.get("field")
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

        field = doc.createInstance(full_service_name)
        if not field:
            return self._tool_error(f"Failed to create field of type '{field_type}'.")

        # Apply properties
        for key, value in properties.items():
            try:
                field.setPropertyValue(key, value)
            except Exception as e:
                # A closed document still reaches execute_safe as DOCUMENT_DISPOSED.
                if is_real_disposal(e):
                    raise
                return self._tool_error(f"Failed to set property '{key}' to '{value}': {e}")

        # Insert at resolved cursor
        if target == "search" and old_content:
            from ..edit_review import next_agent_edit_undo_title
            from plugin.doc.undo import UndoUnavailable, commit_or_rollback

            undo_title = next_agent_edit_undo_title()

            def mutate() -> dict[str, Any] | None:
                cursor.setString("")
                text = cursor.getText()
                text.insertTextContent(cursor, field, False)
                return None

            try:
                error = commit_or_rollback(doc, undo_title, mutate)
                if error is not None:
                    return error
            except UndoUnavailable:
                return self._tool_error(
                    "Cannot replace the matched text atomically (no usable undo context).",
                    code="UNDO_UNAVAILABLE",
                )
            except Exception as e:
                if is_real_disposal(e):
                    raise
                return self._tool_error(f"Failed to insert field; the change was rolled back ({e}).")
        else:
            try:
                text = cursor.getText()
                text.insertTextContent(cursor, field, False)
            except Exception as e:
                if is_real_disposal(e):
                    raise
                return self._tool_error(f"Failed to insert field: {e}")

        return {"status": "ok", "message": f"Successfully inserted {field_type} field.", "applied_properties": properties}

