# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""Writer get_document_content tool — export document content as HTML."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from plugin.framework.tool import ToolBase

if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext

from plugin.doc.text_helpers import collect_tracked_changes
from plugin.framework.errors import reraise_if_disposed


log = logging.getLogger("writeragent.writer")


def _note_unlisted_tracked_changes(result: dict[str, Any]) -> None:
    """The HTML hides deletions. An empty list must not look like a clean document."""
    note = (
        "could not list tracked changes. The document has pending tracked "
        "changes; do not accept or reject them."
    )
    prev = result.get("warning")
    if not isinstance(prev, str) or not prev:
        result["warning"] = note
    elif note not in prev:
        result["warning"] = "%s %s" % (prev, note)


# ------------------------------------------------------------------
# GetDocumentContent
# ------------------------------------------------------------------


class GetDocumentContent(ToolBase):
    """Export the document (or a portion) as formatted content."""

    name: str | None = "get_document_content"
    description: str = (
        "Get document (or selection/range) content. Result includes document_length. "
        "scope: full, selection, or range (requires start, end). "
        "CJK ruby (furigana) is exported as HTML <ruby>base<rt>reading</rt></ruby> "
        "and apply_document_content of that markup recreates live Ruby portions."
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "scope": {"type": "string", "enum": ["full", "selection", "range"], "description": ("Return full document (default), current selection/cursor region, or a character range (requires start and end).")},
            "max_chars": {"type": "integer", "description": "Maximum characters to return."},
            "start": {"type": "integer", "description": "Start character offset (0-based). Required for scope 'range'."},
            "end": {"type": "integer", "description": "End character offset (exclusive). Required for scope 'range'."},
            "include_images": {"type": "boolean", "description": "Include embedded image data (base64) in export. Default false: an image comes back as its wrapper with the picture's name (<div|span ... id=\"Name\"><img src=\"\"/></...>); keep that wrapper in apply_document_content content to keep the picture there, drop it to delete the picture."},
        },
        "required": [],
    }
    uno_services: list[str] | None = ["com.sun.star.text.TextDocument"]
    tier: str = "core"

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        from . import format as format_support
        t0 = time.perf_counter()
        scope = kwargs.get("scope", "full")
        max_chars = kwargs.get("max_chars")
        range_start = kwargs.get("start") if scope == "range" else None
        range_end = kwargs.get("end") if scope == "range" else None
        log.debug("get_document_content: start scope=%r max_chars=%r", scope, max_chars)

        if scope == "range" and (range_start is None or range_end is None):
            return self._tool_error("scope 'range' requires start and end.")

        include_images = bool(kwargs.get("include_images", False))
        walk_warnings: list[str] = []
        content = format_support.document_to_content(
            ctx.doc,
            ctx.ctx,
            ctx.services,
            max_chars=max_chars,
            scope=scope,
            range_start=range_start,
            range_end=range_end,
            include_images=include_images,
            walk_warnings=walk_warnings,
        )
        doc_len = ctx.services.document.get_document_length(ctx.doc)
        result = {"status": "ok", "content": content, "length": len(content), "document_length": doc_len}
        if walk_warnings:
            result["warning"] = walk_warnings[0]
        # Machine-readable truncation signal: without it the only clue was the in-band marker
        # string, which a model must know to look for. (length counts HTML chars; document_length
        # and scope='range' offsets are plain-text chars — use those for follow-up range reads.)
        if max_chars and isinstance(content, str) and content.endswith("[... truncated ...]"):
            result["truncated"] = True
        if scope == "range" and range_start is not None and range_end is not None:
            result["start"] = int(range_start)
            result["end"] = int(range_end)

        # The HTML content above hides tracked deletions and gives no sign that changes are pending.
        # When the document has tracked changes, surface them explicitly (insertion vs deletion, with
        # text) and say they await the user's review — so the model treats them as pending, not errors,
        # and never resolves them itself.
        n_tracked = 0
        try:
            t_tracked = time.perf_counter()
            # collect_tracked_changes swallows its own walk and returns [].
            # TextDocument always has getRedlines; getCount() > 0 signals
            # redlines exist, and a non-int count is the unit-test mock.
            redlines = ctx.doc.getRedlines()
            count = redlines.getCount() if redlines is not None else 0
            if isinstance(count, int) and not isinstance(count, bool) and count > 0:
                changes = collect_tracked_changes(ctx.doc.getText())
                if changes:
                    n_tracked = len(changes)
                    result["tracked_changes"] = changes
                    result["tracked_changes_note"] = (
                        "This document has %d change(s) recorded as tracked changes (listed in "
                        "tracked_changes as insertions/deletions). They are PENDING the user's review — "
                        "not errors and not yet final. Do NOT accept or reject them yourself; that is the "
                        "user's decision." % len(changes)
                    )
                else:
                    _note_unlisted_tracked_changes(result)
            log.debug(
                "get_document_content: phase=tracked_changes elapsed_ms=%.1f n_tracked=%d",
                (time.perf_counter() - t_tracked) * 1000.0,
                n_tracked,
            )
        except Exception as exc:
            # A closed document is not an ok read with the changes omitted.
            reraise_if_disposed(exc, object_type="Document")
            log.exception("get_document_content: could not collect tracked changes")
            _note_unlisted_tracked_changes(result)
        log.debug(
            "get_document_content: done scope=%r content_len=%d document_length=%d n_tracked=%d total_ms=%.1f",
            scope,
            len(content) if isinstance(content, str) else -1,
            doc_len,
            n_tracked,
            (time.perf_counter() - t0) * 1000.0,
        )
        return result
