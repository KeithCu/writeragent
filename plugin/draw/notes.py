# Copyright (c) David Berlioz
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Impress speaker notes tools."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from plugin.draw.base import ToolDrawSpeakerNotesBase
from plugin.draw.bridge import DrawBridge

if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext


class GetSpeakerNotes(ToolDrawSpeakerNotesBase):
    """Read speaker notes from a slide."""

    name: str | None = "get_speaker_notes"
    intent: str | None = "navigate"
    description: str = "Read speaker notes from an Impress slide. Returns the notes text."
    parameters: dict | None = {"type": "object", "properties": {"page": {"type": "integer", "description": "0-based slide index (active slide if omitted)."}}, "required": []}
    uno_services: list | None = ["com.sun.star.presentation.PresentationDocument"]

    def execute(self, ctx: ToolContext, **kwargs: Any):
        page_idx = kwargs.get("page")
        page = DrawBridge.get_slide_for_tool(ctx.doc, page_idx)
        notes_page = page.getNotesPage()
        notes_text = ""
        if notes_page and notes_page.getCount() > 1:
            notes_shape = notes_page.getByIndex(1)
            notes_text = notes_shape.getString()
        return {"status": "ok", "page": page_idx, "notes": notes_text}


class SetSpeakerNotes(ToolDrawSpeakerNotesBase):
    """Set speaker notes on a slide."""

    name: str | None = "set_speaker_notes"
    intent: str | None = "edit"
    description: str = "Set or replace speaker notes on an Impress slide."
    parameters: dict | None = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Speaker notes text."},
            "page": {"type": "integer", "description": "0-based slide index (active slide if omitted)."},
            "append": {"type": "boolean", "description": "Append to existing notes instead of replacing (default: false)."},
        },
        "required": ["text"],
    }
    uno_services: list | None = ["com.sun.star.presentation.PresentationDocument"]
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any):
        text = kwargs.get("text", "")
        append = kwargs.get("append", False)

        page_idx = kwargs.get("page")
        page = DrawBridge.get_slide_for_tool(ctx.doc, page_idx)
        notes_page = page.getNotesPage()
        if notes_page is None or notes_page.getCount() < 2:
            return self._tool_error("No notes page available.")

        notes_shape = notes_page.getByIndex(1)
        if append:
            existing = notes_shape.getString()
            if existing:
                text = existing + "\n" + text
        notes_shape.setString(text)

        return {"status": "ok", "page": page_idx, "message": "Speaker notes updated."}
