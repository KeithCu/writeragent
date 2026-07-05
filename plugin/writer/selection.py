# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""set_selection — select a passage so the user sees it highlighted and target='selection' can
act on it. Headless MCP clients have no way to set a selection otherwise, which made
target='selection' a trap (see the target_resolver fix). Selection is view state and transient,
so for headless flows set_selection and the follow-up edit should be consecutive."""
from typing import Any

from plugin.framework.tool import ToolBase


class SetSelection(ToolBase):
    name = "set_selection"
    tier = "core"
    is_mutation = False
    uno_services = ["com.sun.star.text.TextDocument"]
    description = (
        "Select a passage in the document (highlights it for the user and lets a following "
        "apply_document_content/apply_style with target='selection' act on it). Select by "
        "search_text (with occurrence and case_sensitive) or by character range (char_start/"
        "char_end). Selection is transient view state, so pair it with the edit that uses it. "
        "Returns the selected text and its character range."
    )
    parameters = {
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

        try:
            controller = doc.getCurrentController()
            if controller is None:
                return self._tool_error("No document view is available to hold a selection.")
        except Exception as e:
            return self._tool_error("No document view available: %s" % e)

        # --- resolve the range to select ---
        if search_text:
            try:
                occurrence = int(kwargs.get("occurrence", 0) or 0)
            except (TypeError, ValueError):
                return self._tool_error("occurrence must be an integer.")
            if occurrence < 0:
                return self._tool_error("occurrence must be non-negative.")
            case_sensitive = bool(kwargs.get("case_sensitive", True))
            try:
                sd = doc.createSearchDescriptor()
                sd.SearchString = str(search_text)
                sd.SearchRegularExpression = False
                sd.SearchCaseSensitive = case_sensitive
                found = doc.findFirst(sd)
                for _ in range(occurrence):
                    if found is None:
                        break
                    found = doc.findNext(found.getEnd(), sd)
            except Exception as e:
                return self._tool_error("Search failed: %s" % e)
            if found is None:
                return self._tool_error(
                    "No match for search_text '%s'%s." % (search_text, (" at occurrence %d" % occurrence) if occurrence else ""))
            target_range = found
        elif char_start is not None or char_end is not None:
            if char_start is None or char_end is None:
                return self._tool_error("char_start and char_end must both be provided.")
            from plugin.doc.document_helpers import get_text_cursor_at_range

            target_range = get_text_cursor_at_range(doc, int(char_start), int(char_end))
            if target_range is None:
                return self._tool_error("Could not build a cursor for that character range.")
        else:
            return self._tool_error("Provide search_text or char_start/char_end.")

        # --- apply the selection in the view ---
        try:
            controller.select(target_range)
        except Exception as e:
            return self._tool_error("Could not set the selection: %s" % e)

        selected = ""
        try:
            selected = target_range.getString()
        except Exception:
            pass
        result = {"status": "ok", "selected_text": selected, "length": len(selected)}
        # Report the character range when we can measure it (offset path already knows it).
        if search_text is None and char_start is not None and char_end is not None:
            result["char_start"], result["char_end"] = int(char_start), int(char_end)
        return result
