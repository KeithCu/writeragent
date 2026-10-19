# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""set_selection — select a passage so the user sees it highlighted and target='selection' can
act on it. Headless MCP clients have no way to set a selection otherwise, which made
target='selection' a trap (see the target_resolver fix). Selection is view state and transient,
so for headless flows set_selection and the follow-up edit should be consecutive."""
from typing import Any

from plugin.framework.errors import reraise_if_disposed
from plugin.framework.tool import ToolBase
from plugin.writer.search import (
    describe_match_location,
    drawing_shape_containing,
    find_occurrence_range,
)


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
            "occurrence": {"type": "integer", "description": "0-based match to select when search_text repeats (default 0)."},
            "case_sensitive": {"type": "boolean", "description": "Case-sensitive search_text match (default true)."},
            "char_start": {"type": "integer", "description": "Alternative to search_text: 0-based start character offset (body text)."},
            "char_end": {"type": "integer", "description": "End character offset (exclusive), required with char_start."},
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
            return self._tool_error("Cannot combine search_text with char_start/char_end.")

        controller = doc.getCurrentController()
        if controller is None:
            return self._tool_error("No document view is available to hold a selection.")

        # --- resolve the range to select ---
        if has_search:
            occurrence_val = kwargs.get("occurrence", 0)
            if occurrence_val is None:
                occurrence = 0
            elif type(occurrence_val) is not int:
                return self._tool_error("occurrence must be an integer.")
            elif occurrence_val < 0:
                return self._tool_error("occurrence must be non-negative.")
            else:
                occurrence = occurrence_val

            case_sensitive = bool(kwargs.get("case_sensitive", True))
            search_str = str(search_text)

            from plugin.writer.format import content_has_markup, html_to_plain_text
            if content_has_markup(search_str):
                config_svc = ctx.services.get("config") if hasattr(ctx, "services") else None
                search_str = html_to_plain_text(search_str, ctx.ctx, config_svc)

            try:
                target_range = find_occurrence_range(
                    doc, search_str, occurrence=occurrence, case_sensitive=case_sensitive
                )
            except IndexError as e:
                return self._tool_error(str(e))

            if target_range is None:
                shape_name = drawing_shape_containing(doc, search_str)
                if shape_name:
                    return self._tool_error(
                        "search_text is only inside a drawing shape / floating text box ('%s'). "
                        "Edit it via the shapes toolset (delegate_to_specialized_writer_toolset domain='shapes')."
                        % shape_name
                    )
                return self._tool_error(
                    "No match for search_text '%s'%s."
                    % (search_text, (" at occurrence %d" % occurrence) if occurrence else "")
                )

            controller.select(target_range)

            selected = ""
            read_failed = False
            try:
                selected = target_range.getString()
            except Exception as exc:
                reraise_if_disposed(exc)
                read_failed = True

            result: dict[str, Any] = {"status": "ok"}
            if not read_failed:
                result["selected_text"] = selected
                result["length"] = len(selected)
            else:
                result["warning"] = "Could not read selected text."

            result["location"] = describe_match_location(target_range, doc)
            return result

        elif has_char:
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

            from plugin.doc.text_helpers import get_text_cursor_at_range_reached

            target_range, fully_reached = get_text_cursor_at_range_reached(
                doc, char_start, char_end
            )
            if target_range is None:
                return self._tool_error("Could not build a cursor for that character range.")

            selected = ""
            read_failed = False
            try:
                selected = target_range.getString()
            except Exception as exc:
                reraise_if_disposed(exc)
                read_failed = True

            if not fully_reached and not selected:
                return self._tool_error("Could not reach character range in document.")

            controller.select(target_range)

            result = {"status": "ok"}
            if not read_failed:
                result["selected_text"] = selected
                result["length"] = len(selected)
            else:
                result["warning"] = "Could not read selected text."

            if not fully_reached:
                result["char_start"] = char_start
                result["warning"] = "Character range was truncated at the document end."
            else:
                result["char_start"] = char_start
                result["char_end"] = char_end

            return result
        else:
            return self._tool_error("Provide search_text or char_start/char_end.")
