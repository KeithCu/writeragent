# Copyright (c) David Berlioz
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Impress speaker notes tools."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from plugin.draw.base import ToolDrawSpeakerNotesBase
from plugin.draw.bridge import DrawBridge, notes_text, resolve_page_and_bridge, set_notes_text
from plugin.doc.text_helpers import normalize_linebreaks
from plugin.framework.errors import uno_read

if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext


class GetSpeakerNotes(ToolDrawSpeakerNotesBase):
    """Read speaker notes from a slide."""

    name: str | None = "get_speaker_notes"
    description: str = "Read speaker notes from an Impress slide. Returns the notes text."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"page": {"type": "integer", "description": "0-based slide index (active slide if omitted)."}}, "required": []}

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        _bridge, actual_idx = resolve_page_and_bridge(ctx, kwargs)
        page = DrawBridge.get_slide_for_tool(ctx.doc, actual_idx)
        notes = notes_text(page)
        return {"status": "ok", "page": actual_idx, "notes": "" if notes is None else notes}


class SetSpeakerNotes(ToolDrawSpeakerNotesBase):
    """Set speaker notes on a slide."""

    name: str | None = "set_speaker_notes"
    description: str = "Set or replace speaker notes on an Impress slide."
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {"text": {"type": "string", "description": "Speaker notes text."}, "page": {"type": "integer", "description": "0-based slide index (active slide if omitted)."}, "append": {"type": "boolean", "description": "Append to existing notes instead of replacing (default: false)."}},
        "required": ["text"],
    }
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        text = kwargs["text"]
        append = kwargs.get("append", False)

        _bridge, actual_idx = resolve_page_and_bridge(ctx, kwargs)
        page = DrawBridge.get_slide_for_tool(ctx.doc, actual_idx)
        text = normalize_linebreaks(text)
        if append:
            existing = notes_text(page)
            if existing is None:
                notes_page = uno_read(lambda: page.getNotesPage(), None)
                detail = "no notes page" if notes_page is None else "a notes page but no notes body (no NotesShape)"
                return self._tool_error(
                    "Slide %d has %s." % (actual_idx, detail),
                    code="NOTES_UNAVAILABLE",
                    page=actual_idx,
                )
            if existing.strip():
                text = existing + ("" if existing.endswith("\n") else "\n") + text
            set_notes_text(page, text)
        else:
            if not set_notes_text(page, text):
                notes_page = uno_read(lambda: page.getNotesPage(), None)
                detail = "no notes page" if notes_page is None else "a notes page but no notes body (no NotesShape)"
                return self._tool_error(
                    "Slide %d has %s." % (actual_idx, detail),
                    code="NOTES_UNAVAILABLE",
                    page=actual_idx,
                )

        return {"status": "ok", "page": actual_idx, "message": "Speaker notes updated."}
