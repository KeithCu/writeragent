# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Document research outer-agent tools: list nearby files in the same folder."""

from __future__ import annotations

import logging
from typing import Any, ClassVar, Literal

from plugin.framework.tool import ToolBase, ToolContext
from plugin.doc.document_research import list_nearby_files
from plugin.doc.document_research_grep import grep_nearby_files

log = logging.getLogger(__name__)


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


class GrepNearbyFiles(ToolBase):
    """Search text across nearby LibreOffice files without opening each via delegate_read_document."""

    name = "grep_nearby_files"
    description = (
        "Search nearby LibreOffice files for text and return snippet previews per matching file. "
        "Use file_subset='budget' to scan only files whose basenames contain 'budget' "
        "(e.g. Budget_2026.ods, my-budget.odt). Prefer this before delegate_read_document when "
        "locating which file contains a keyword."
    )
    tier = "specialized"
    specialized_domain: ClassVar[str | None] = "document_research"
    specialized_cross_cutting: ClassVar[bool] = True
    is_mutation = False
    long_running = True
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Text or regex to search for."},
            "file_subset": {
                "type": "string",
                "description": "Optional basename token (e.g. 'budget' matches *budget*.od*) or absolute path to one file.",
            },
            "regex": {"type": "boolean", "description": "Treat pattern as regular expression (default: false)."},
            "case_sensitive": {"type": "boolean", "description": "Case-sensitive search (default: false)."},
        },
        "required": ["pattern"],
    }

    def is_async(self) -> bool:
        return True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        from plugin.framework.queue_executor import execute_on_main_thread

        pattern = kwargs.get("pattern")
        if not pattern:
            return self._tool_error("pattern is required")

        file_subset = kwargs.get("file_subset")
        regex = bool(kwargs.get("regex", False))
        case_sensitive = bool(kwargs.get("case_sensitive", False))

        def _run() -> dict[str, Any]:
            return grep_nearby_files(
                ctx.ctx,
                ctx.doc,
                ctx.services,
                str(pattern),
                file_subset=str(file_subset) if file_subset else None,
                regex=regex,
                case_sensitive=case_sensitive,
                stop_checker=ctx.stop_checker,
                status_callback=ctx.status_callback,
            )

        return execute_on_main_thread(_run)


class ListOpenDocuments(ToolBase):
    """List all currently open documents in LibreOffice, returning their URLs, names, and types."""

    name = "list_open_documents"
    description = (
        "List all currently open documents in LibreOffice. "
        "Returns the path, name, URL, document type (writer, calc, draw), and if it is the currently active document."
    )
    tier = "mcp"
    is_mutation = False
    parameters = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        from plugin.framework.queue_executor import execute_on_main_thread
        from plugin.framework.uno_context import get_desktop
        from plugin.doc.document_research import guess_doc_type_from_path, _system_path_from_url
        import os

        def _run() -> dict[str, Any]:
            desktop = get_desktop(ctx.ctx)
            comps = desktop.getComponents()
            if not comps:
                return {"status": "ok", "documents": []}
            enum = comps.createEnumeration()
            docs = []
            while enum and enum.hasMoreElements():
                elem = enum.nextElement()
                model = elem
                if hasattr(elem, "getController") and elem.getController():
                    model = elem.getController().getModel()
                if model is None or not hasattr(model, "getURL"):
                    continue
                url = model.getURL()
                if not url:
                    try:
                        from plugin.doc.document_helpers import get_document_type, DocumentType
                        doc_type_enum = get_document_type(model)
                        doc_type = "writer"
                        if doc_type_enum == DocumentType.CALC:
                            doc_type = "calc"
                        elif doc_type_enum in (DocumentType.DRAW, DocumentType.IMPRESS):
                            doc_type = "draw"
                        docs.append({
                            "name": "Untitled",
                            "url": "",
                            "path": "",
                            "doc_type": doc_type,
                            "is_active": (model == ctx.doc)
                        })
                    except Exception:
                        pass
                    continue
                
                path = _system_path_from_url(str(url)) or ""
                doc_type_guess = guess_doc_type_from_path(path) if path else "unknown"
                if doc_type_guess == "unknown":
                    try:
                        from plugin.doc.document_helpers import get_document_type, DocumentType
                        doc_type_enum = get_document_type(model)
                        if doc_type_enum == DocumentType.WRITER:
                            doc_type_guess = "writer"
                        elif doc_type_enum == DocumentType.CALC:
                            doc_type_guess = "calc"
                        elif doc_type_enum in (DocumentType.DRAW, DocumentType.IMPRESS):
                            doc_type_guess = "draw"
                    except Exception:
                        pass
                
                name = os.path.basename(path) if path else "Untitled"
                docs.append({
                    "name": name,
                    "url": str(url),
                    "path": path,
                    "doc_type": doc_type_guess,
                    "is_active": (model == ctx.doc)
                })
            return {"status": "ok", "documents": docs}

        return execute_on_main_thread(_run)

