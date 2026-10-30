# Copyright (c) David Berlioz
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Impress speaker notes tools."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from plugin.draw.base import ToolDrawSpeakerNotesBase
from plugin.draw.bridge import DrawBridge, find_notes_shape, resolve_page_and_bridge
from plugin.doc.text_helpers import normalize_linebreaks
from plugin.framework.errors import uno_probe

if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext


def _write_notes(shape: Any, text: str, append: bool) -> None:
    """Replace or append the notes body; one paragraph break between old and new text."""
    text = normalize_linebreaks(text)
    if append:
        existing = str(shape.getString() or "")
        if existing.strip():
            text = existing + ("" if existing.endswith("\n") else "\n") + text
    shape.setString(text)


class GetSpeakerNotes(ToolDrawSpeakerNotesBase):
    """Read speaker notes from a slide."""

    name: str | None = "get_speaker_notes"
    intent: str | None = "navigate"
    description: str = "Read speaker notes from an Impress slide. Returns the notes text."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"page": {"type": "integer", "description": "0-based slide index (active slide if omitted)."}}, "required": []}

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        _bridge, actual_idx = resolve_page_and_bridge(ctx, kwargs)
        page = DrawBridge.get_slide_for_tool(ctx.doc, actual_idx)
        notes_shape = find_notes_shape(uno_probe(lambda: page.getNotesPage(), None))
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
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        text = kwargs["text"]
        append = kwargs.get("append", False)

        _bridge, actual_idx = resolve_page_and_bridge(ctx, kwargs)
        page = DrawBridge.get_slide_for_tool(ctx.doc, actual_idx)
        notes_page = uno_probe(lambda: page.getNotesPage(), None)
        if notes_page is None:
            return self._tool_error(
                "Slide %d has no notes page." % actual_idx,
                code="NOTES_UNAVAILABLE",
                page=actual_idx,
            )
        notes_shape = find_notes_shape(notes_page)
        if notes_shape is None:
            return self._tool_error(
                "Slide %d has a notes page but no notes body (no NotesShape)." % actual_idx,
                code="NOTES_UNAVAILABLE",
                page=actual_idx,
            )
        _write_notes(notes_shape, text, append)

        return {"status": "ok", "page": actual_idx, "message": "Speaker notes updated."}
