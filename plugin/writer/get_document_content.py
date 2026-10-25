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

from plugin.framework.errors import reraise_if_disposed
from plugin.writer.review_scan import scan_redlines
from plugin.writer.specialized_base import enum_param


log = logging.getLogger("writeragent.writer")

_MAX_TRACKED_CHANGES = 100
_TRACKED_NOTE = (
    "This document has %d change(s) recorded as tracked changes. They are PENDING the user's "
    "review — not errors and not yet final. Do NOT accept or reject them yourself; that is the "
    "user's decision."
)
_UNLISTED_NOTE = (
    "could not list tracked changes. The document has pending tracked changes; do not accept or "
    "reject them."
)


def _append_warning(result: dict[str, Any], note: str) -> None:
    """The HTML hides deletions. An empty list must not look like a clean document."""
    prev = result.get("warning")
    if not isinstance(prev, str) or not prev:
        result["warning"] = note
    elif note not in prev:
        result["warning"] = "%s %s" % (prev, note)


def _summarize_redlines(
    doc: Any, max_changes: int | None = None
) -> tuple[list[dict[str, str]], int, int, bool, bool]:
    """Insert/Delete redlines as insertion/deletion entries, from getRedlines().

    Returns (changes, insert_or_delete_count, total_redlines, reliable, truncated).
    Format/Table redlines count toward total but produce no entry. ``truncated`` is the cap
    only: a redline with no text (for example an inserted image) is not truncation.
    """
    from plugin.writer.tracking import _describe_redline

    limit = _MAX_TRACKED_CHANGES if max_changes is None else max_changes
    changes: list[dict[str, str]] = []
    found = 0
    truncated = False

    def on_item(redline: Any) -> bool:
        nonlocal found, truncated
        rtype = redline.getPropertyValue("RedlineType")
        if rtype not in ("Insert", "Delete"):
            return True
        found += 1
        if len(changes) >= limit:
            truncated = True
            return True
        text = _describe_redline(redline, found - 1, doc).get("text") or ""
        if text:
            changes.append(
                {"type": "insertion" if rtype == "Insert" else "deletion", "text": text}
            )
        return True

    reliable, _seen, total = scan_redlines(doc, on_item)
    return changes, found, total, reliable, truncated


# ------------------------------------------------------------------
# GetDocumentContent
# ------------------------------------------------------------------


class GetDocumentContent(ToolBase):
    """Export the document (or a portion) as formatted content."""

    name: str | None = "get_document_content"
    description: str = (
        "Get document (or selection/range) content. Result includes document_length (plain-text "
        "characters) and length (HTML characters). scope: full, selection, or range (requires start, "
        "end as plain-text offsets in the document_length space). max_chars counts returned HTML and "
        "is approximate. The result may carry warning, truncated, tracked_changes, "
        "tracked_changes_total, and tracked_changes_truncated. scope='selection' with nothing selected "
        "returns empty content plus a warning. "
        "CJK ruby (furigana) is exported as HTML <ruby>base<rt>reading</rt></ruby> "
        "and apply_document_content of that markup recreates live Ruby portions."
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "scope": enum_param(["full", "selection", "range"], ("Return full document (default), current selection/cursor region, or a character range (requires start and end).")),
            "max_chars": {"type": "integer", "description": "Maximum characters of exported HTML to return (approximate: the truncation marker adds about 20 characters)."},
            "start": {"type": "integer", "description": "Plain-text start offset (0-based), same coordinate space as document_length. Required for scope 'range'; must be less than end."},
            "end": {"type": "integer", "description": "Plain-text end offset (exclusive), same coordinate space as document_length. Required for scope 'range'."},
            "include_images": {"type": "boolean", "description": "Include embedded image data (base64) in export. Default false: an image comes back as its wrapper with the picture's name (<div|span ... id=\"Name\"><img src=\"\"/></...>); keep that wrapper in apply_document_content content to keep the picture there, drop it to delete the picture."},
        },
        "required": [],
    }
    uno_services: list[str] | None = ["com.sun.star.text.TextDocument"]
    tier: str = "core"

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        from . import format as format_support
        t0 = time.perf_counter()
        scope = kwargs.get("scope") or "full"
        max_chars = kwargs.get("max_chars")
        log.debug("get_document_content: start scope=%r max_chars=%r", scope, max_chars)

        if scope not in ("full", "selection", "range"):
            return self._tool_error("scope must be 'full', 'selection', or 'range'.")
        if max_chars is not None and (
            not isinstance(max_chars, int) or isinstance(max_chars, bool) or max_chars < 1
        ):
            return self._tool_error("max_chars must be a positive integer.")
        if scope == "range":
            try:
                range_start = int(kwargs["start"])
                range_end = int(kwargs["end"])
            except (KeyError, TypeError, ValueError):
                return self._tool_error("scope 'range' requires integer start and end.")
            if range_start < 0 or range_end <= range_start:
                return self._tool_error("scope 'range' requires 0 <= start < end.")
        else:
            range_start = range_end = None

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
            result["warning"] = " ".join(walk_warnings)
        if scope == "selection" and not content:
            _append_warning(
                result,
                "Nothing was selected, or the selection exported no content; an empty result "
                "does not mean the document is empty.",
            )
        if doc_len == 0 and isinstance(content, str) and content.strip():
            _append_warning(result, "document_length came back 0; do not use it as a range offset.")
        # Machine-readable truncation signal: without it the only clue was the in-band marker
        # string, which a model must know to look for. (length counts HTML chars; document_length
        # and scope='range' offsets are plain-text chars — use those for follow-up range reads.)
        if max_chars and isinstance(content, str) and content.endswith("[... truncated ...]"):
            result["truncated"] = True
        if scope == "range" and range_start is not None and range_end is not None:
            result["start"] = range_start
            result["end"] = range_end

        # The HTML content above hides tracked deletions and gives no sign that changes are pending.
        # When the document has tracked changes, surface them explicitly (insertion vs deletion, with
        # text) and say they await the user's review — so the model treats them as pending, not errors,
        # and never resolves them itself.
        n_tracked = 0
        try:
            t_tracked = time.perf_counter()
            # The old body walk could not see a header/footer change and reported "could not list"
            # for a format-only redline; getRedlines() sees the whole document and costs only the
            # change count. A non-int count is the unit-test mock.
            redlines = ctx.doc.getRedlines()
            count = redlines.getCount() if redlines is not None else 0
            if isinstance(count, int) and not isinstance(count, bool) and count > 0:
                changes, found, total, reliable, truncated = _summarize_redlines(ctx.doc)
                n_tracked = len(changes)
                if total:
                    result["tracked_changes_total"] = total
                if changes:
                    result["tracked_changes"] = changes
                if truncated:
                    result["tracked_changes_truncated"] = True
                if not reliable:
                    _append_warning(result, _UNLISTED_NOTE)
                else:
                    note = _TRACKED_NOTE % total
                    if not changes:
                        note += (
                            " None of them are text insertions/deletions (for example, formatting changes)."
                        )
                    if truncated:
                        note += " Only %d of %d insertion/deletion change(s) are listed." % (
                            len(changes), found)
                    result["tracked_changes_note"] = note
            log.debug(
                "get_document_content: phase=tracked_changes elapsed_ms=%.1f n_tracked=%d",
                (time.perf_counter() - t_tracked) * 1000.0,
                n_tracked,
            )
        except Exception as exc:
            # A closed document is not an ok read with the changes omitted.
            reraise_if_disposed(exc, object_type="Document")
            log.exception("get_document_content: could not collect tracked changes")
            _append_warning(result, _UNLISTED_NOTE)
        log.debug(
            "get_document_content: done scope=%r content_len=%d document_length=%d n_tracked=%d total_ms=%.1f",
            scope,
            len(content) if isinstance(content, str) else -1,
            doc_len,
            n_tracked,
            (time.perf_counter() - t0) * 1000.0,
        )
        return result
