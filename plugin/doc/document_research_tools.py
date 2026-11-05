# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Document research outer-agent tools: list nearby files in the same folder."""

from __future__ import annotations

from typing import Any, ClassVar

from plugin.chatbot.agent_manual import (
    get_section,
    list_topics,
    manual_index,
    normalize_topic,
    writer_topic_list_text,
)
from plugin.framework.doc_type import doc_type_of
from plugin.doc.document_research import get_open_documents, list_nearby_files
from plugin.doc.udprops import (
    get_document_property,
    list_user_defined_document_properties,
    read_builtin_document_properties,
    remove_document_property,
    set_document_property,
    write_builtin_document_properties,
)
from plugin.framework.errors import user_stopped_payload
from plugin.framework.queue_executor import main
from plugin.framework.send_cancellation import SendCancelled
from plugin.framework.tool import ToolBase, ToolContext


class ListNearbyFiles(ToolBase):
    """List office files in the active document's directory (or LO Work folder if untitled)."""

    name: str | None = "list_nearby_files"
    description: str = (
        "List files in the same folder as the active document (newest first). "
        "Default file_kind documents: LibreOffice formats (.odt, .ods, .odp, .odg, flat XML, templates). "
        "file_kind images: .png, .jpg, .jpeg, .gif, .webp, .bmp, .svg only (discovery; not readable via delegate_read_document). "
        "Excludes the active file. Optional filter is a case-insensitive substring on the basename."
    )
    tier: str = "specialized"
    specialized_domain: ClassVar[str | None] = "document_research"
    specialized_cross_cutting: ClassVar[bool] = True
    is_mutation: bool | None = False
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "filter": {"type": "string", "description": "Optional basename substring (e.g. 'budget')."},
            "file_kind": {
                "type": "string",
                "enum": ["documents", "images"],
                "description": "documents (default): office files. images: photos/diagrams in the folder.",
            },
        },
        "required": [],
    }

    def is_async(self) -> bool:
        return True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        # Execute off main thread because listing directory scans can take time.
        # list_nearby_files handles marshaling UNO requests to the main thread internally.
        scope = ctx.send_cancellation
        return list_nearby_files(
            ctx.ctx,
            ctx.doc,
            filter=kwargs.get("filter"),
            file_kind=kwargs.get("file_kind", "documents"),
            cancel=scope,
        )


class ListOpenDocuments(ToolBase):
    """List all currently open documents in LibreOffice, returning their URLs, names, and types."""

    name: str | None = "list_open_documents"
    description: str = (
        "List all currently open documents in LibreOffice. "
        "Returns the path, name, URL, a stable id (uid), document type (writer, calc, draw), whether it is the currently active document, and whether it has unsaved changes (modified). "
        "Pass a document's url OR uid as the document_url argument on any tool to target that document; the uid also works for unsaved/untitled documents that have no URL yet. "
        "You cannot save documents yourself; when modified is true and the work is done, tell the user to save. "
        "Also returns current_local_datetime with the host's wall clock."
    )
    tier: str = "mcp"
    is_mutation: bool | None = False
    # Listing open documents must work when NONE is open (it should return [] / no active doc),
    # otherwise the MCP no-document gate turns "what's open?" into a confusing NO_DOCUMENT_OPEN.
    requires_document: bool = False
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        def _run() -> dict[str, Any]:
            from plugin.mcp.mcp_protocol import _format_mcp_clock_context

            docs = get_open_documents(ctx.ctx, ctx.doc)
            # Piggyback clock for MCP hosts that ignore initialize.instructions (#374 Bug 1).
            return {"status": "ok", "documents": docs, "current_local_datetime": _format_mcp_clock_context()}

        try:
            return main.run(_run)
        except SendCancelled:
            return user_stopped_payload()


_EXCEL_PY_DAG_META = "ExcelPyDagMeta"


def _is_product_udprop(name: str) -> bool:
    return name.startswith("WriterAgent") or name == _EXCEL_PY_DAG_META


class DocumentPropertiesGet(ToolBase):
    """Read built-in and user-defined document metadata."""

    name: str | None = "document_properties_get"
    description: str = (
        "Read document metadata: title, subject, author, description, keywords, and custom "
        "user-defined properties. Internal WriterAgent product properties are listed by name only."
    )
    tier: str = "core"
    is_mutation: bool | None = False
    parameters: dict[str, Any] | None = {"type": "object", "properties": {}, "required": []}

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
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

    name: str | None = "document_properties_set"
    description: str = (
        "Update document metadata. Pass only fields to change. Empty string clears a built-in text "
        "field; an empty keywords array clears keywords. User-defined values must be strings."
    )
    tier: str = "core"
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

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
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
                    )
                updated.append("user_defined:%s" % key)
            for key in ud_removes:
                if _is_product_udprop(key):
                    return self._tool_error(
                        "Cannot remove internal property %r." % key,
                        code="INVALID_PARAMETER",
                        parameter="remove_user_defined",
                    )
                if remove_document_property(ctx.doc, key):
                    removed.append(key)
                else:
                    return self._tool_error(
                        "Could not remove user-defined property %r." % key,
                        code="REMOVE_PROPERTY_FAILED",
                    )
            return {"status": "ok", "updated": updated, "removed": removed}

        try:
            return main.run(_run)
        except SendCancelled:
            return user_stopped_payload()


class GetGuidance(ToolBase):
    """On-demand how-to-use manual for the WriterAgent tools (agent pulls one topic at a time)."""

    name: str | None = "get_guidance"
    description: str = (
        "Read WriterAgent's how-to-use manual on demand. Call with no topic to get the list of topics; "
        "call with a topic to read just that section (so you don't load everything). Topics follow the "
        f"open document's type (for Writer: {writer_topic_list_text()}). Use this when unsure how an edit, the review modes, search, or image ops work. "
        "The no-topic index also returns current_local_datetime."
    )
    # Core, not mcp-exclusive: the sidebar's HYBRID prompt keeps search/navigation/images out of
    # the ambient text and relies on pulling them from here (same single source, same topics).
    tier: str = "core"
    is_mutation: bool | None = False
    # Pure documentation — works with or without a document open (no doc -> neutral index).
    requires_document: bool = False
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "topic": {"type": "string", "description": "Topic to read (see the no-topic call for the list; topics follow the document type). Omit for the topic list."},
        },
        "required": [],
    }

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        # Guidance must match the document being worked on (a Calc session must never read Writer
        # advice). Resolve the target document the same way every other tool does; with no document
        # open, serve the neutral index / the always-available generic topics.
        from plugin.mcp.mcp_protocol import _format_mcp_clock_context

        doc_type = doc_type_of(ctx.doc)
        raw = (kwargs.get("topic") or "").strip()
        if not raw:
            # Index call is a natural early MCP step — stamp the clock for hosts that ignore
            # initialize.instructions (#374 Bug 1).
            return {
                "status": "ok",
                "doc_type": doc_type,
                "topics": list_topics(doc_type),
                "index": manual_index(doc_type),
                "current_local_datetime": _format_mcp_clock_context(),
            }
        section = get_section(raw, doc_type)
        if section is None:
            return {
                "status": "error",
                "code": "UNKNOWN_TOPIC",
                "message": "Unknown guidance topic '%s' for this document type. Available topics: %s." % (raw, ", ".join(list_topics(doc_type))),
                "topics": list_topics(doc_type),
            }
        return {"status": "ok", "doc_type": doc_type, "topic": normalize_topic(raw, doc_type), "guidance": section}
