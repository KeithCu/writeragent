# Copyright (c) David Berlioz
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Impress speaker notes tools."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from plugin.draw.base import ToolDrawSpeakerNotesBase
from plugin.draw.bridge import DrawBridge, find_notes_shape

if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext


class GetSpeakerNotes(ToolDrawSpeakerNotesBase):
    """Read speaker notes from a slide."""

    name: str | None = "get_speaker_notes"
    intent: str | None = "navigate"
    description: str = "Read speaker notes from an Impress slide. Returns the notes text."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"page": {"type": "integer", "description": "0-based slide index (active slide if omitted)."}}, "required": []}
    uno_services: list[str] | None = ["com.sun.star.presentation.PresentationDocument"]

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        page_idx = kwargs.get("page")

        actual_idx = page_idx if page_idx is not None else ctx.active_page_index
        if actual_idx is None:
            bridge = DrawBridge(ctx.doc)
            actual_idx = bridge.get_active_page_index()

        page = DrawBridge.get_slide_for_tool(ctx.doc, actual_idx)
        notes_shape = find_notes_shape(page.getNotesPage())
        notes_text = ""
        if notes_shape is not None:
            notes_text = notes_shape.getString() or ""
        return {"status": "ok", "page": actual_idx, "notes": notes_text}


class SetSpeakerNotes(ToolDrawSpeakerNotesBase):
    """Set speaker notes on a slide."""

    name: str | None = "set_speaker_notes"
    intent: str | None = "edit"
    description: str = "Set or replace speaker notes on an Impress slide."
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {"text": {"type": "string", "description": "Speaker notes text."}, "page": {"type": "integer", "description": "0-based slide index (active slide if omitted)."}, "append": {"type": "boolean", "description": "Append to existing notes instead of replacing (default: false)."}},
        "required": ["text"],
    }
    uno_services: list[str] | None = ["com.sun.star.presentation.PresentationDocument"]
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        text = kwargs.get("text", "")
        append = kwargs.get("append", False)

        page_idx = kwargs.get("page")

        actual_idx = page_idx if page_idx is not None else ctx.active_page_index
        if actual_idx is None:
            bridge = DrawBridge(ctx.doc)
            actual_idx = bridge.get_active_page_index()

        page = DrawBridge.get_slide_for_tool(ctx.doc, actual_idx)
        notes_shape = find_notes_shape(page.getNotesPage())
        if notes_shape is None:
            return self._tool_error("No notes page available.")
        if append:
            existing = notes_shape.getString()
            if existing:
                text = existing + "\n" + text
        notes_shape.setString(text)

        return {"status": "ok", "page": actual_idx, "message": "Speaker notes updated."}
