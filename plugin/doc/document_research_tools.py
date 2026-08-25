# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Document research outer-agent tools: list nearby files in the same folder."""

from __future__ import annotations

from typing import Any, ClassVar, Literal

from plugin.doc.document_research import list_nearby_files
from plugin.framework.tool import ToolBase, ToolContext


class ListNearbyFiles(ToolBase):
    """List office files in the active document's directory (or LO Work folder if untitled)."""

    name = "list_nearby_files"
    description = (
        "List files in the same folder as the active document (newest first). "
        "Default file_kind documents: LibreOffice formats (.odt, .ods, .odp, .odg, flat XML, templates). "
        "file_kind images: .png, .jpg, .jpeg, .gif, .webp, .bmp, .svg only (discovery; not readable via delegate_read_document). "
        "Excludes the active file. Optional filter is a case-insensitive substring on the basename."
    )
    tier = "specialized"
    specialized_domain: ClassVar[str | None] = "document_research"
    specialized_cross_cutting: ClassVar[bool] = True
    is_mutation = False
    parameters = {
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
        from plugin.framework.thread_guard import on_main_thread
        from plugin.framework.queue_executor import execute_on_main_thread

        filt = kwargs.get("filter")
        file_kind_raw = kwargs.get("file_kind")
        file_kind: Literal["documents", "images"] = "images" if file_kind_raw == "images" else "documents"

        def _run() -> dict[str, Any]:
            return list_nearby_files(ctx.ctx, ctx.doc, filter=filt, file_kind=file_kind)

        if on_main_thread():
            return _run()
        return execute_on_main_thread(_run)


class ListOpenDocuments(ToolBase):
    """List all currently open documents in LibreOffice, returning their URLs, names, and types."""

    name = "list_open_documents"
    description = (
        "List all currently open documents in LibreOffice. "
        "Returns the path, name, URL, a stable id (uid), document type (writer, calc, draw), whether it is the currently active document, and whether it has unsaved changes (modified). "
        "Pass a document's url OR uid as the document_url argument on any tool to target that document; the uid also works for unsaved/untitled documents that have no URL yet. "
        "You cannot save documents yourself; when modified is true and the work is done, tell the user to save. "
        "Also returns current_local_datetime with the host's wall clock."
    )
    tier = "mcp"
    is_mutation = False
    # Listing open documents must work when NONE is open (it should return [] / no active doc),
    # otherwise the MCP no-document gate turns "what's open?" into a confusing NO_DOCUMENT_OPEN.
    requires_document = False
    parameters = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        from plugin.framework.thread_guard import on_main_thread
        from plugin.framework.queue_executor import execute_on_main_thread
        from plugin.doc.document_research import get_open_documents

        def _run() -> dict[str, Any]:
            from plugin.mcp.mcp_protocol import _format_mcp_clock_context

            docs = get_open_documents(ctx.ctx, ctx.doc)
            # Piggyback clock for MCP hosts that ignore initialize.instructions (#374 Bug 1).
            return {"status": "ok", "documents": docs, "current_local_datetime": _format_mcp_clock_context()}

        if on_main_thread():
            return _run()
        return execute_on_main_thread(_run)


class GetGuidance(ToolBase):
    """On-demand how-to-use manual for the WriterAgent tools (agent pulls one topic at a time)."""

    name = "get_guidance"
    description = (
        "Read WriterAgent's how-to-use manual on demand. Call with no topic to get the list of topics; "
        "call with a topic to read just that section (so you don't load everything). Topics follow the "
        "open document's type (for Writer: editing, editing-html, review-modes, search, navigation, "
        "images, concurrency). Use this when unsure how an edit, the review modes, search, or image ops work. "
        "The no-topic index also returns current_local_datetime."
    )
    # Core, not mcp-exclusive: the sidebar's HYBRID prompt keeps search/navigation/images out of
    # the ambient text and relies on pulling them from here (same single source, same topics).
    tier = "core"
    is_mutation = False
    # Pure documentation — works with or without a document open (no doc -> neutral index).
    requires_document = False
    parameters = {
        "type": "object",
        "properties": {
            "topic": {"type": "string", "description": "Topic to read (see the no-topic call for the list; topics follow the document type). Omit for the topic list."},
        },
        "required": [],
    }

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        from plugin.chatbot.agent_manual import doc_type_of, get_section, list_topics, manual_index, normalize_topic

        # Guidance must match the document being worked on (a Calc session must never read Writer
        # advice). Resolve the target document the same way every other tool does; with no document
        # open, serve the neutral index / the always-available generic topics.
        from plugin.mcp.mcp_protocol import _format_mcp_clock_context

        doc_type = doc_type_of(getattr(ctx, "doc", None))
        raw = (kwargs.get("topic") or "").strip()
        clock = _format_mcp_clock_context()
        if not raw:
            # Index call is a natural early MCP step — stamp the clock for hosts that ignore
            # initialize.instructions (#374 Bug 1).
            return {
                "status": "ok",
                "doc_type": doc_type,
                "topics": list_topics(doc_type),
                "index": manual_index(doc_type),
                "current_local_datetime": clock,
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
