# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""set_selection — select a passage so the user sees it highlighted and target='selection' can
act on it. Headless MCP clients have no other way to set a selection, and target='selection'
needs one. Selection is view state and transient, so for headless flows set_selection and the
follow-up edit should be consecutive.

In a sidebar turn, target='selection' reads the Send-time SelectionPin rather than the live
view, so set_selection updates that pin when present; MCP contexts carry no pin.
"""
from typing import Any
import logging

from plugin.doc.text_helpers import TrackedDeletions, normalize_linebreaks, visible_body_range
from plugin.framework.errors import reraise_if_disposed
from plugin.framework.tool import ToolBase
from plugin.writer.html_import import plain_text_if_markup
from plugin.writer.search import (
    LADDER_CASE_SENSITIVE_DESCRIPTION,
    describe_match_location,
    explicit_case_sensitive,
    occurrence_range_or_error,
)
from plugin.writer.target_resolver import update_selection_pin

log = logging.getLogger("writeragent.writer")


def _read_selection_fields(doc: Any, target_range: Any) -> dict[str, Any]:
    """Read visible selected text and length, returning a warning on failure."""
    try:
        selected = normalize_linebreaks(TrackedDeletions(doc).text_of(target_range))
    except Exception as exc:
        reraise_if_disposed(exc)
        log.exception("set_selection: could not read selected text")
        return {"warning": "Could not read selected text."}
    return {"selected_text": selected, "length": len(selected)}


class SetSelection(ToolBase):
    name: str | None = "set_selection"
    tier: str = "core"
    is_mutation: bool | None = False
    uno_services: list[str] | None = ["com.sun.star.text.TextDocument"]
    description: str = (
        "Select a passage in the document (highlights it for the user and lets a following "
        "apply_content/apply_style with target='selection' act on it). Select by "
        "search_text (with occurrence and case_sensitive) or by character range (char_start/"
        "char_end). Selection is transient view state, so pair it with the edit that uses it. "
        "Character offsets are returned only for a fully applied char range. The search path "
        "returns selected_text and length."
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "search_text": {"type": "string", "description": "Text to select (first match unless occurrence is set)."},
            "occurrence": {"type": "integer", "minimum": 0, "default": 0, "description": "0-based match to select when search_text repeats."},
            "case_sensitive": {"type": "boolean", "description": LADDER_CASE_SENSITIVE_DESCRIPTION},
            "char_start": {
                "type": "integer",
                "minimum": 0,
                "description": (
                    "Alternative to search_text: 0-based start offset in get_content's document_length "
                    "space (same as search return_offsets). Must be in a paragraph, not in a table; "
                    "an offset inside a field or footnote number covers all of it."
                ),
            },
            "char_end": {"type": "integer", "minimum": 0, "description": "End offset (exclusive, same space), required with char_start."},
        },
        "required": [],
    }

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        doc = ctx.doc
        search_text = kwargs.get("search_text")
        char_start = kwargs.get("char_start")
        char_end = kwargs.get("char_end")

        has_search = search_text is not None and search_text != ""
        has_char = char_start is not None or char_end is not None

        if has_search and has_char:
            return self._tool_error("Cannot combine search_text with char_start/char_end.", code="INVALID_PARAM")
        if not has_search and not has_char:
            return self._tool_error("Provide search_text or char_start/char_end.", code="INVALID_PARAM")

        if not has_search:
            if char_start is None or char_end is None:
                return self._tool_error("char_start and char_end must both be provided.", code="INVALID_PARAM")
            if char_start < 0 or char_end < 0:
                return self._tool_error("char_start and char_end must be non-negative.", code="INVALID_PARAM")
            if char_start > char_end:
                return self._tool_error("char_start must not exceed char_end.", code="INVALID_PARAM")
            if char_start == char_end:
                return self._tool_error("char_start and char_end must define a non-empty range.", code="INVALID_PARAM")

        controller = doc.getCurrentController()
        if controller is None:
            return self._tool_error("No document view is available to hold a selection.", code="TOOL_EXECUTION_ERROR")

        if has_search:
            case_sensitive = explicit_case_sensitive(kwargs.get("case_sensitive"))
            search_str = plain_text_if_markup(str(search_text))
            if not search_str.strip():
                return self._tool_error("search_text is empty after normalization.", code="INVALID_PARAM")

            return self._select_by_search(
                ctx, controller, search_str, kwargs.get("occurrence", 0), case_sensitive
            )

        found = visible_body_range(doc, char_start, char_end)
        if found is None:
            return self._tool_error("Could not reach character range in document.", code="TOOL_EXECUTION_ERROR")
        controller.select(found.cursor)
        update_selection_pin(ctx, found.cursor)
        result: dict[str, Any] = {
            "status": "ok",
            "selected_text": found.text,
            "length": len(found.text),
            "char_start": found.start,
        }
        if found.end < char_end:
            result["warning"] = "The range ends past the readable text; it was cut at offset %d." % found.end
        else:
            result["char_end"] = found.end
        return result

    def _select_by_search(
        self,
        ctx: Any,
        controller: Any,
        search_str: str,
        raw_occurrence: Any,
        case_sensitive: bool | None,
    ) -> dict[str, Any]:
        doc = ctx.doc
        found, err, mode = occurrence_range_or_error(
            doc, search_str, raw_occurrence, case_sensitive=case_sensitive, subject="search_text"
        )
        if found is None:
            assert err is not None
            res = err
        else:
            controller.select(found)
            update_selection_pin(ctx, found)
            fields = _read_selection_fields(doc, found)
            res = {
                "status": "ok",
                **fields,
                "location": describe_match_location(found, doc),
            }
        res.update(mode.result_fields())
        return res
