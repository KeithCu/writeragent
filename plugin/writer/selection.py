# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""set_selection — select a passage so the user sees it highlighted and target='selection' can
act on it. Headless MCP clients have no way to set a selection otherwise, which made
target='selection' a trap (see the target_resolver fix). Selection is view state and transient,
so for headless flows set_selection and the follow-up edit should be consecutive."""
from typing import Any
import logging

from plugin.doc.text_helpers import get_text_cursor_at_range_reached
from plugin.framework.errors import reraise_if_disposed
from plugin.framework.tool import ToolBase
from plugin.writer.format import plain_text_if_markup
from plugin.writer.search import (
    describe_match_location,
    drawing_shape_containing,
    find_occurrence_range,
    parse_non_negative_int,
    shape_only_search_message,
)

log = logging.getLogger("writeragent.writer")


def _read_selection_fields(target_range: Any) -> dict[str, Any]:
    """Read selected text and length, returning a warning on failure."""
    try:
        selected = target_range.getString()
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
        "apply_document_content/apply_style with target='selection' act on it). Select by "
        "search_text (with occurrence and case_sensitive) or by character range (char_start/"
        "char_end). Selection is transient view state, so pair it with the edit that uses it. "
        "Character offsets are returned only for a fully applied char range. The search path "
        "returns selected_text and length."
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "search_text": {"type": "string", "description": "Text to select (first match unless occurrence is set)."},
            "occurrence": {"type": "integer", "minimum": 0, "description": "0-based match to select when search_text repeats (default 0)."},
            "case_sensitive": {"type": "boolean", "description": "Case-sensitive search_text match (default true)."},
            "char_start": {"type": "integer", "minimum": 0, "description": "Alternative to search_text: 0-based start character offset (body text)."},
            "char_end": {"type": "integer", "minimum": 0, "description": "End character offset (exclusive), required with char_start."},
        },
        "required": [],
    }

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        doc = ctx.doc
        search_text = kwargs.get("search_text")
        char_start = kwargs.get("char_start")
        char_end = kwargs.get("char_end")
        occurrence: int = 0

        has_search = search_text is not None and search_text != ""
        has_char = char_start is not None or char_end is not None

        if has_search and has_char:
            return self._tool_error("Cannot combine search_text with char_start/char_end.")
        if not has_search and not has_char:
            return self._tool_error("Provide search_text or char_start/char_end.")

        if has_search:
            occ_val, occ_err = parse_non_negative_int(kwargs.get("occurrence", 0))
            if occ_err:
                return self._tool_error(occ_err)
            occurrence = 0 if occ_val is None else occ_val
        else:
            if char_start is None or char_end is None:
                return self._tool_error("char_start and char_end must both be provided.")
            if type(char_start) is not int or type(char_end) is not int:
                return self._tool_error("char_start and char_end must be integers.")
            if char_start < 0 or char_end < 0:
                return self._tool_error("char_start and char_end must be non-negative.")
            if char_start > char_end:
                return self._tool_error("char_start must not exceed char_end.")
            if char_start == char_end:
                return self._tool_error("char_start and char_end must define a non-empty range.")

        controller = doc.getCurrentController()
        if controller is None:
            return self._tool_error("No document view is available to hold a selection.")

        # --- resolve the range to select ---
        if has_search:
            case_sensitive = bool(kwargs.get("case_sensitive", True))
            search_str = plain_text_if_markup(str(search_text), ctx.ctx, ctx.services.get("config"))
            if not search_str.strip():
                return self._tool_error("search_text is empty after normalization.")

            try:
                target_range = find_occurrence_range(
                    doc, search_str, occurrence=occurrence, case_sensitive=case_sensitive
                )
            except IndexError as e:
                return self._tool_error(str(e))

            if target_range is None:
                shape_name = drawing_shape_containing(doc, search_str, case_sensitive=case_sensitive)
                if shape_name:
                    return self._tool_error(
                        shape_only_search_message("search_text", shape_name)
                    )
                return self._tool_error(
                    "No match for search_text '%s'%s."
                    % (search_str, (" at occurrence %d" % occurrence) if occurrence else "")
                )

            controller.select(target_range)
            fields = _read_selection_fields(target_range)
            return {
                "status": "ok",
                **fields,
                "location": describe_match_location(target_range, doc),
            }

        target_range, fully_reached = get_text_cursor_at_range_reached(
            doc, char_start, char_end
        )
        if target_range is None:
            return self._tool_error("Could not build a cursor for that character range.")

        fields = _read_selection_fields(target_range)
        if not fully_reached and not fields.get("selected_text") and "warning" not in fields:
            return self._tool_error("Could not reach character range in document.")

        controller.select(target_range)

        result: dict[str, Any] = {"status": "ok", **fields}
        if not fully_reached:
            result["char_start"] = char_start
            truncation = "Character range was truncated at the document end."
            result["warning"] = (truncation + " " + result["warning"]) if "warning" in result else truncation
        else:
            result["char_start"] = char_start
            result["char_end"] = char_end

        return result
