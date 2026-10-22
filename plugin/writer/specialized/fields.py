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

from plugin.framework.errors import is_real_disposal, reraise_if_disposed

from ..specialized_base import ToolWriterFieldBase, target_params
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


def _iter_body_field_identity(doc: Any) -> Any:
    # Deferred: plugin.writer.search pulls the UNO document helpers in, and this module is
    # imported wherever the field tools are registered.
    from ..search import describe_match_location

    fields = doc.getTextFields()
    enum = fields.createEnumeration()
    hf_labels: dict[int, str] = {}
    count = 0
    while enum.hasMoreElements():
        field = enum.nextElement()
        if _is_annotation(field):
            continue
        count += 1
        service = _specific_field_service(field)
        try:
            presentation = field.getPresentation(False)
        except Exception as e:
            reraise_if_disposed(e)
            presentation = ""
        try:
            location = describe_match_location(field.getAnchor(), doc, hf_labels)
        except Exception as e:
            reraise_if_disposed(e)
            location = "unknown"
        yield count, field, service, presentation, location


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
        results = []
        hasher = hashlib.sha256()
        count = 0
        service_props: dict[str, set[str]] = {}
        for field_id, field, service, presentation, location in _iter_body_field_identity(doc):
            count = field_id
            hasher.update(f"{service}|{presentation}|{location}\n".encode("utf-8"))
            try:
                content = field.getPresentation(True)
            except Exception as e:
                reraise_if_disposed(e)
                content = ""

            props = {}
            if hasattr(field, "getPropertySetInfo"):
                if service not in service_props:
                    try:
                        info = field.getPropertySetInfo()
                        if info is not None and hasattr(info, "getProperties"):
                            service_props[service] = {
                                getattr(p, "Name", "")
                                for p in (info.getProperties() or ())
                            }
                        else:
                            service_props[service] = set()
                    except Exception as e:
                        reraise_if_disposed(e)
                        service_props[service] = set()

                valid_props = service_props.get(service, set())
                for prop_name in KNOWN_PROPERTIES:
                    if prop_name in valid_props:
                        try:
                            val = field.getPropertyValue(prop_name)
                            if isinstance(val, (int, float, str, bool)):
                                props[prop_name] = val
                        except Exception as e:
                            reraise_if_disposed(e)

            results.append({"id": field_id, "presentation": presentation, "content": content,
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
        raw_ids: list[Any] = kwargs["ids"]
        assert isinstance(raw_ids, list)

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
        hasher = hashlib.sha256()
        fields_to_delete: list[tuple[int, Any, str]] = []

        for field_id, field, service, presentation, location in _iter_body_field_identity(doc):
            hasher.update(f"{service}|{presentation}|{location}\n".encode("utf-8"))
            if field_id in seen:
                fields_to_delete.append((field_id, field, location))

        if snapshot is not None:
            current_snapshot = hasher.hexdigest()
            if current_snapshot != snapshot:
                return {
                    "status": "error",
                    "code": "FIELD_LIST_STALE",
                    "message": "Document fields have changed since fields_list. Call fields_list again.",
                    "deleted_count": 0,
                }

        found_ids = {c for c, _, _ in fields_to_delete}
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

        targets = []
        for _cid, field, where in fields_to_delete:
            anchor = field.getAnchor()
            try:
                what = field.getPresentation(True)
            except Exception as e:
                reraise_if_disposed(e)
                what = ""
            targets.append((anchor, what, where))

        from ..edit_review import next_agent_edit_undo_title
        from plugin.doc.undo import UndoUnavailable, commit_or_rollback

        undo_title = next_agent_edit_undo_title()
        deleted: list[dict[str, Any]] = []

        def mutate() -> dict[str, Any] | None:
            for anchor, what, where in targets:
                # setString("") on the anchor removes only the field (same as footnotes_delete); see test_fields_uno.py.
                anchor.setString("")
                deleted.append({"content": what, "location": where})
            return None

        try:
            error = commit_or_rollback(doc, undo_title, mutate)
            if error is not None:
                return error
        except UndoUnavailable:
            return self._tool_error(
                "Cannot delete fields atomically (no usable undo context).",
                code="UNDO_UNAVAILABLE",
            )
        except Exception as e:
            if is_real_disposal(e):
                raise
            return self._tool_error(
                f"Failed to delete fields; the change was rolled back ({e}).",
                deleted_count=0,
            )

        deleted_count = len(deleted)
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
            **target_params("the field"),
        },
        "required": ["field"],
    }
    is_mutation: bool | None = True

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        field_type = kwargs["field"]
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

        properties = dict(properties) if properties else {}
        if field_type == "PageNumber":
            # Bare UNO PageNumber is NumberingType=0/SubType=PREV: blank on page 1.
            properties.setdefault("NumberingType", 4)
            if "SubType" not in properties:
                import uno
                properties["SubType"] = uno.Enum("com.sun.star.text.PageNumberType", "CURRENT")

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

