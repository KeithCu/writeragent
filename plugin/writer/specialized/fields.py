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

from plugin.doc.text_helpers import iter_elements
from plugin.doc.undo import commit_or_rollback_mutation
from plugin.framework.errors import is_disposal, reraise_if_disposed
from plugin.framework.tool import ToolBase

from ..edit_review import next_agent_edit_undo_title
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
        if is_disposal(e):
            raise
        return False


def _specific_field_service(field: Any) -> str:
    """Return the most specific com.sun.star.text.textfield.* service name."""
    try:
        services = tuple(field.getSupportedServiceNames())
    except Exception:
        services = ()
    narrower = sorted(
        s for s in services
        if isinstance(s, str)
        and s.startswith("com.sun.star.text.textfield.")
        and s != "com.sun.star.text.textfield.TextField"
    )
    if narrower:
        return narrower[0]
    if "com.sun.star.text.textfield.TextField" in services:
        return "com.sun.star.text.textfield.TextField"
    return ""


def _iter_body_field_identity(doc: Any, hasher: Any) -> Any:
    # Deferred: plugin.writer.search pulls the UNO document helpers in, and this module is
    # imported wherever the field tools are registered.
    from ..search import LocationLabeler

    fields = doc.getTextFields()
    enum = fields.createEnumeration()
    labeler = LocationLabeler(doc)
    count = 0
    for field in iter_elements(enum):
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
            location = labeler.describe(field.getAnchor())
        except Exception as e:
            reraise_if_disposed(e)
            location = "unknown"
        hasher.update(f"{service}|{location}\n".encode("utf-8"))
        yield count, field, service, presentation, location


class FieldsUpdateAll(ToolWriterFieldBase):
    name: str | None = "fields_update_all"
    description: str = "Refresh all text fields (dates, page numbers, cross-references). Call after changes that affect computed fields."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {}, "required": []}
    is_mutation: bool | None = True

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        doc = ctx.doc
        fields = doc.getTextFields()
        fields.refresh()

        enum = fields.createEnumeration()
        count = 0
        for _field in iter_elements(enum):
            count += 1

        return {"status": "ok", "fields_refreshed": count}


class FieldsList(ToolWriterFieldBase):
    name: str | None = "fields_list"
    description: str = "List all text fields in the document. Returns their types and text content, allowing you to identify and inspect fields like page numbers or dates."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {}, "required": []}

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        doc = ctx.doc
        results = []
        hasher = hashlib.sha256()
        service_props: dict[str, set[str]] = {}
        for field_id, field, service, presentation, location in _iter_body_field_identity(doc, hasher):
            try:
                content = field.getPresentation(True)
            except Exception as e:
                reraise_if_disposed(e)
                content = ""

            props = {}
            if service not in service_props:
                try:
                    info = field.getPropertySetInfo()
                    service_props[service] = {
                        getattr(p, "Name", "")
                        for p in (info.getProperties() or ())
                    }
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

        return {"status": "ok", "field_count": len(results), "snapshot": hasher.hexdigest(), "fields": results}


class FieldsDelete(ToolWriterFieldBase):
    name: str | None = "fields_delete"
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

        for item in raw_ids:
            if isinstance(item, bool) or not isinstance(item, int):
                return self._tool_error("Parameter 'ids' must contain only integers (no booleans).")

        cleaned_ids = list(dict.fromkeys(raw_ids))
        requested_count = len(cleaned_ids)
        if requested_count == 0:
            return self._tool_error("No field IDs provided.", requested_count=0, missing_ids=[])

        wanted = set(cleaned_ids)
        doc = ctx.doc
        hasher = hashlib.sha256()
        found: dict[int, tuple[Any, str]] = {}

        for field_id, field, _service, _presentation, location in _iter_body_field_identity(doc, hasher):
            if field_id in wanted:
                found[field_id] = (field, location)

        if snapshot is not None:
            current_snapshot = hasher.hexdigest()
            if current_snapshot != snapshot:
                return {
                    "status": "error",
                    "code": "FIELD_LIST_STALE",
                    "message": "Document fields have changed since fields_list. Call fields_list again.",
                    "deleted_count": 0,
                }

        missing_ids = [fid for fid in cleaned_ids if fid not in found]

        if not found:
            return {
                "status": "error",
                "code": "FIELD_NOT_FOUND",
                "message": f"No matching text fields found to delete (missing IDs: {missing_ids}). Use comment_delete to delete comments.",
                "deleted_count": 0,
                "requested_count": requested_count,
                "missing_ids": missing_ids,
            }

        anchors = []
        deleted: list[dict[str, Any]] = []
        for field, where in found.values():
            anchor = field.getAnchor()
            try:
                what = field.getPresentation(True)
            except Exception as e:
                reraise_if_disposed(e)
                what = ""
            anchors.append(anchor)
            deleted.append({"content": what, "location": where})

        undo_title = next_agent_edit_undo_title()

        def mutate() -> dict[str, Any] | None:
            for anchor in anchors:
                # setString("") on the anchor removes only the field (same as footnotes_delete); see test_fields_uno.py.
                anchor.setString("")
            return None

        err = commit_or_rollback_mutation(
            self,
            doc,
            undo_title,
            mutate,
            undo_unavailable_msg="Cannot delete fields atomically (no usable undo context).",
            rollback_msg="Failed to delete fields; the change was rolled back",
            deleted_count=0,
        )
        if err is not None:
            return err

        return {
            "status": "ok",
            "message": f"Successfully deleted {len(deleted)} field(s).",
            "deleted_count": len(deleted),
            "requested_count": requested_count,
            "missing_ids": missing_ids,
            "deleted": deleted,
        }


class FieldsInsert(ToolWriterFieldBase):
    name: str | None = "fields_insert"
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
        doc = ctx.doc
        if not hasattr(doc, "createInstance"):
            return self._tool_error("Document does not support creating instances.")

        target = kwargs.get("target", "selection")
        old_content = kwargs.get("old_content")

        try:
            cursor = resolve_target_cursor(ctx, target, old_content, case_sensitive=kwargs.get("case_sensitive"))
        except ValueError as ve:
            return self._tool_error(str(ve))

        properties = dict(kwargs.get("properties") or {})
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
                if is_disposal(e):
                    raise
                return self._tool_error(f"Failed to set property '{key}' to '{value}': {e}")

        # Insert at resolved cursor
        if target == "search" and old_content:
            undo_title = next_agent_edit_undo_title()

            def mutate() -> dict[str, Any] | None:
                cursor.setString("")
                text = cursor.getText()
                text.insertTextContent(cursor, field, False)
                return None

            err = commit_or_rollback_mutation(
                self,
                doc,
                undo_title,
                mutate,
                undo_unavailable_msg="Cannot replace the matched text atomically (no usable undo context).",
                rollback_msg="Failed to insert field; the change was rolled back",
            )
            if err is not None:
                return err
        else:
            try:
                text = cursor.getText()
                text.insertTextContent(cursor, field, False)
            except Exception as e:
                if is_disposal(e):
                    raise
                return self._tool_error(f"Failed to insert field: {e}")

        return {"status": "ok", "message": f"Successfully inserted {field_type} field.", "applied_properties": properties}


_EXCEL_PY_DAG_META = "ExcelPyDagMeta"


def _is_product_udprop(name: str) -> bool:
    return name.startswith("WriterAgent") or name == _EXCEL_PY_DAG_META


class DocumentPropertiesGet(ToolBase):
    """Read built-in and user-defined document metadata."""
    # Subclasses ToolBase (not ToolWriterFieldBase) because document properties work across all apps (Writer/Calc/Draw).

    name: str | None = "document_properties_get"
    description: str = (
        "Read document metadata: title, subject, author, description, keywords, and custom "
        "user-defined properties. Internal WriterAgent product properties are listed by name only."
    )
    tier: str = "specialized"
    specialized_domain: str | None = "fields"
    is_mutation: bool | None = False
    parameters: dict[str, Any] | None = {"type": "object", "properties": {}, "required": []}

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        from plugin.doc.udprops import (
            get_document_property,
            list_user_defined_document_properties,
            read_builtin_document_properties,
        )
        from plugin.framework.queue_executor import main
        from plugin.framework.send_cancellation import SendCancelled
        from plugin.framework.errors import user_stopped_payload

        def _run() -> dict[str, Any]:
            builtins = read_builtin_document_properties(ctx.doc)
            user_defined: dict[str, str] = {}
            internal_omitted: list[str] = []
            for prop_name in list_user_defined_document_properties(ctx.doc):
                if _is_product_udprop(prop_name):
                    internal_omitted.append(prop_name)
                    continue
                val = get_document_property(ctx.doc, prop_name, default="")
                user_defined[prop_name] = str(val) if val is not None else ""
            return {
                "status": "ok",
                **builtins,
                "user_defined": user_defined,
                "internal_omitted": sorted(internal_omitted),
            }

        try:
            return main.run(_run)
        except SendCancelled:
            return user_stopped_payload()


class DocumentPropertiesSet(ToolBase):
    """Update built-in and user-defined document metadata."""
    # Subclasses ToolBase (not ToolWriterFieldBase) because document properties work across all apps (Writer/Calc/Draw).

    name: str | None = "document_properties_set"
    description: str = (
        "Update document metadata. Pass only fields to change. Empty string clears a built-in text "
        "field; an empty keywords array clears keywords. User-defined values must be strings."
    )
    tier: str = "specialized"
    specialized_domain: str | None = "fields"
    is_mutation: bool | None = True
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Document title (empty clears)."},
            "subject": {"type": "string", "description": "Subject (empty clears)."},
            "author": {"type": "string", "description": "Author (empty clears)."},
            "description": {"type": "string", "description": "Description (empty clears)."},
            "keywords": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Keyword list (empty array clears).",
            },
            "user_defined": {
                "type": "object",
                "additionalProperties": {"type": "string"},
                "description": "Custom property names and string values to set.",
            },
            "remove_user_defined": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Custom property names to remove.",
            },
        },
        "required": [],
    }

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        from plugin.doc.udprops import (
            remove_document_property,
            set_document_property,
            write_builtin_document_properties,
        )
        from plugin.framework.queue_executor import main
        from plugin.framework.send_cancellation import SendCancelled
        from plugin.framework.errors import user_stopped_payload

        title = kwargs.get("title")
        subject = kwargs.get("subject")
        author = kwargs.get("author")
        description = kwargs.get("description")
        keywords = kwargs.get("keywords")
        user_defined = kwargs.get("user_defined")
        remove_user_defined = kwargs.get("remove_user_defined")

        if keywords is not None and not isinstance(keywords, list):
            return self._tool_error(
                "keywords must be an array of strings.",
                code="INVALID_PARAMETER",
                parameter="keywords",
            )
        if keywords is not None:
            for item in keywords:
                if not isinstance(item, str):
                    return self._tool_error(
                        "keywords must be an array of strings.",
                        code="INVALID_PARAMETER",
                        parameter="keywords",
                    )

        ud_writes: dict[str, str] = {}
        if user_defined is not None:
            if not isinstance(user_defined, dict):
                return self._tool_error(
                    "user_defined must be an object of string values.",
                    code="INVALID_PARAMETER",
                    parameter="user_defined",
                )
            for key, val in user_defined.items():
                if not isinstance(key, str) or not isinstance(val, str):
                    return self._tool_error(
                        "user_defined keys and values must be strings.",
                        code="INVALID_PARAMETER",
                        parameter="user_defined",
                    )
                if _is_product_udprop(key):
                    return self._tool_error(
                        "Cannot set internal property %r." % key,
                        code="INVALID_PARAMETER",
                        parameter="user_defined",
                    )
                ud_writes[key] = val

        ud_removes: list[str] = []
        if remove_user_defined is not None:
            if not isinstance(remove_user_defined, list):
                return self._tool_error(
                    "remove_user_defined must be an array of property names.",
                    code="INVALID_PARAMETER",
                    parameter="remove_user_defined",
                )
            for key in remove_user_defined:
                if not isinstance(key, str):
                    return self._tool_error(
                        "remove_user_defined entries must be strings.",
                        code="INVALID_PARAMETER",
                        parameter="remove_user_defined",
                    )
                if key in ud_writes:
                    return self._tool_error(
                        "Cannot set and remove the same user-defined property %r." % key,
                        code="INVALID_PARAMETER",
                        parameter="user_defined",
                    )
                if _is_product_udprop(key):
                    return self._tool_error(
                        "Cannot remove internal property %r." % key,
                        code="INVALID_PARAMETER",
                        parameter="remove_user_defined",
                    )
                ud_removes.append(key)

        builtin_fields: dict[str, Any] = {}
        if title is not None:
            builtin_fields["title"] = title
        if subject is not None:
            builtin_fields["subject"] = subject
        if author is not None:
            builtin_fields["author"] = author
        if description is not None:
            builtin_fields["description"] = description
        if keywords is not None:
            builtin_fields["keywords"] = keywords

        if not builtin_fields and not ud_writes and not ud_removes:
            return self._tool_error("No properties to set.", code="NO_CHANGES")

        def _run() -> dict[str, Any]:
            updated: list[str] = list(write_builtin_document_properties(ctx.doc, **builtin_fields))
            removed: list[str] = []
            for key, val in ud_writes.items():
                if not set_document_property(ctx.doc, key, val):
                    return self._tool_error(
                        "Could not write user-defined property %r." % key,
                        code="SET_PROPERTY_FAILED",
                        updated=updated,
                        removed=removed,
                    )
                updated.append("user_defined:%s" % key)
            for key in ud_removes:
                if remove_document_property(ctx.doc, key):
                    removed.append(key)
                else:
                    return self._tool_error(
                        "Could not remove user-defined property %r." % key,
                        code="REMOVE_PROPERTY_FAILED",
                        updated=updated,
                        removed=removed,
                    )
            return {"status": "ok", "updated": updated, "removed": removed}

        try:
            return main.run(_run)
        except SendCancelled:
            return user_stopped_payload()

