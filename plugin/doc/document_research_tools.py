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
        from plugin.framework.queue_executor import execute_on_main_thread

        filt = kwargs.get("filter")
        file_kind_raw = kwargs.get("file_kind")
        file_kind: Literal["documents", "images"] = "images" if file_kind_raw == "images" else "documents"

        def _run() -> dict[str, Any]:
            return list_nearby_files(ctx.ctx, ctx.doc, filter=filt, file_kind=file_kind)

        return execute_on_main_thread(_run)


class ListOpenDocuments(ToolBase):
    """List all currently open documents in LibreOffice, returning their URLs, names, and types."""

    name = "list_open_documents"
    description = (
        "List all currently open documents in LibreOffice. "
        "Returns the path, name, URL, a stable id (uid), document type (writer, calc, draw), whether it is the currently active document, and whether it has unsaved changes (modified). "
        "Pass a document's url OR uid as the document_url argument on any tool to target that document; the uid also works for unsaved/untitled documents that have no URL yet. "
        "You cannot save documents yourself; when modified is true and the work is done, tell the user to save."
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
        from plugin.framework.queue_executor import execute_on_main_thread
        from plugin.doc.document_research import get_open_documents

        def _run() -> dict[str, Any]:
            docs = get_open_documents(ctx.ctx, ctx.doc)
            return {"status": "ok", "documents": docs}

        return execute_on_main_thread(_run)


_FEEDBACK_LOG_FILENAME = "agent_feedback.jsonl"
_FEEDBACK_CATEGORIES = ("bug", "ux", "feature")


def _append_feedback_log(category: str, summary: str, details: str, ts: str | None = None) -> str | None:
    """Append one agent feedback entry to agent_feedback.jsonl in the LO user config dir.

    Best-effort: returns the file path on success, None on any failure (never breaks report_bug).
    ts is injectable for tests; otherwise stamped from the local clock."""
    import json
    import os

    try:
        from plugin.framework.config import user_config_dir

        if ts is None:
            import datetime

            ts = datetime.datetime.now().isoformat(timespec="seconds")
        config_dir = user_config_dir()
        if not config_dir:
            return None
        path = os.path.join(config_dir, _FEEDBACK_LOG_FILENAME)
        record = {"ts": ts, "category": category, "summary": summary, "details": details}
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return path
    except Exception:
        return None


class ReportBug(ToolBase):
    """Report a WriterAgent bug or bad user experience (agent-callable)."""

    name = "report_bug"
    description = (
        "Report a WriterAgent bug or bad experience — the agent itself may call this when a tool "
        "misbehaves, returns a confusing result, or the workflow felt wrong. It records the feedback "
        "locally for the user AND returns a pre-filled GitHub issue URL the user can review and submit. "
        "Nothing is published automatically. Describe what happened and what you expected."
    )
    tier = "mcp"
    is_mutation = False
    # Reporting a bug must work even with NO document open — e.g. "the extension won't open my
    # file". The no-document gate would otherwise block the very tool meant to report that.
    requires_document = False
    parameters = {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "One-line title of the bug / bad experience."},
            "details": {"type": "string", "description": "What happened, what you expected, and any steps to reproduce. Be specific."},
            "category": {"type": "string", "enum": list(_FEEDBACK_CATEGORIES), "description": "bug (something broke), ux (confusing/clunky), or feature (missing capability). Default 'bug'."},
        },
        "required": ["summary"],
    }

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        summary = (kwargs.get("summary") or "").strip()
        if not summary:
            return self._tool_error("summary is required.", code="MISSING_PARAMETER", parameter="summary")
        details = (kwargs.get("details") or "").strip()
        category = (kwargs.get("category") or "bug").strip().lower()
        if category not in _FEEDBACK_CATEGORIES:
            category = "bug"

        # 1) Pre-filled GitHub issue URL (reuse bug_report's builder; env block + truncation included).
        body = details or "(no details provided)"
        body += "\n\n_Filed via the report_bug tool (agent-assisted). Review before submitting._"
        url = ""
        try:
            from plugin.framework.bug_report import build_github_issue_url

            url = build_github_issue_url(title="[%s] %s" % (category, summary), extra_body=body, ctx=getattr(ctx, "ctx", None))
        except Exception:
            url = ""  # URL is best-effort; the local feedback log below is the durable record.

        # 2) Log locally for the user (best-effort).
        logged_to = _append_feedback_log(category, summary, details)

        return {
            "status": "ok",
            "message": ("Feedback recorded locally. Share the github_issue_url with the user to file it — "
                        "nothing was auto-submitted (auto-filing a GitHub issue would require a configured "
                        "token and the user's consent)."),
            "category": category,
            "github_issue_url": url,
            "logged_to": logged_to,
        }


class GetGuidance(ToolBase):
    """On-demand how-to-use manual for the WriterAgent tools (agent pulls one topic at a time)."""

    name = "get_guidance"
    description = (
        "Read WriterAgent's how-to-use manual on demand. Call with no topic to get the list of topics; "
        "call with a topic to read just that section (so you don't load everything). Topics follow the "
        "open document's type (for Writer: editing, editing-html, review-modes, search, navigation, "
        "images, concurrency). Use this when unsure how an edit, the review modes, search, or image ops work."
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
        from plugin.framework.agent_manual import doc_type_of, get_section, list_topics, manual_index, normalize_topic

        # Guidance must match the document being worked on (a Calc session must never read Writer
        # advice). Resolve the target document the same way every other tool does; with no document
        # open, serve the neutral index / the always-available generic topics.
        doc_type = doc_type_of(getattr(ctx, "doc", None))
        raw = (kwargs.get("topic") or "").strip()
        if not raw:
            return {"status": "ok", "doc_type": doc_type, "topics": list_topics(doc_type), "index": manual_index(doc_type)}
        section = get_section(raw, doc_type)
        if section is None:
            return {
                "status": "error",
                "code": "UNKNOWN_TOPIC",
                "message": "Unknown guidance topic '%s' for this document type. Available topics: %s." % (raw, ", ".join(list_topics(doc_type))),
                "topics": list_topics(doc_type),
            }
        return {"status": "ok", "doc_type": doc_type, "topic": normalize_topic(raw, doc_type), "guidance": section}
