# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""document_research grep_nearby_files tool."""

from __future__ import annotations

from typing import Any, ClassVar

from plugin.doc.document_research_grep import grep_nearby_files
from plugin.framework.tool import ToolBase, ToolContext


class GrepNearbyFiles(ToolBase):
    """Search text across nearby LibreOffice files without opening each via delegate_read_document."""

    name: str | None = "grep_nearby_files"
    description: str = (
        "Search nearby LibreOffice files for text and return snippet previews per matching file. "
        "Use file_subset='budget' to scan only files whose basenames contain 'budget' "
        "(e.g. Budget_2026.ods, my-budget.odt). Prefer this before delegate_read_document when "
        "locating which file contains a keyword."
    )
    tier: str = "specialized"
    specialized_domain: ClassVar[str | None] = "document_research"
    specialized_cross_cutting: ClassVar[bool] = True
    is_mutation: bool | None = False
    long_running: bool = True
    parameters: dict | None = {
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
        from plugin.framework.thread_guard import on_main_thread
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

        if on_main_thread():
            return _run()
        return execute_on_main_thread(_run)
