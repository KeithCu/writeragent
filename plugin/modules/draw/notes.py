# Copyright (c) David Berlioz
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Impress speaker notes tools."""

from plugin.framework.errors import ToolExecutionError, UnoObjectError
from plugin.framework.tool_base import ToolBase


def _get_slide(doc, page_index=None):
    """Resolve a slide by index or active."""
    pages = doc.getDrawPages()
    if page_index is not None:
        if page_index < 0 or page_index >= pages.getCount():
            raise ToolExecutionError("Page index %d out of range." % page_index)
        return pages.getByIndex(page_index)
    controller = doc.getCurrentController()
    if hasattr(controller, "getCurrentPage"):
        return controller.getCurrentPage()
    return pages.getByIndex(0)


class GetSpeakerNotes(ToolBase):
    """Read speaker notes from a slide."""

    name = "get_speaker_notes"
    intent = "navigate"
    description = (
        "Read speaker notes from an Impress slide. "
        "Returns the notes text."
    )
    parameters = {
        "type": "object",
        "properties": {
            "page_index": {
                "type": "integer",
                "description": "0-based slide index (active slide if omitted).",
            },
        },
        "required": [],
    }
    uno_services = ["com.sun.star.presentation.PresentationDocument"]

    def execute(self, ctx, **kwargs):
        page = _get_slide(ctx.doc, kwargs.get("page_index"))
        notes_page = page.getNotesPage()
        notes_text = ""
        if notes_page and notes_page.getCount() > 1:
            notes_shape = notes_page.getByIndex(1)
            notes_text = notes_shape.getString()
        return {
            "status": "ok",
            "page_index": kwargs.get("page_index"),
            "notes": notes_text,
        }
class SetSpeakerNotes(ToolBase):
    """Set speaker notes on a slide."""

    name = "set_speaker_notes"
    intent = "edit"
    description = (
        "Set or replace speaker notes on an Impress slide."
    )
    parameters = {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "Speaker notes text.",
            },
            "page_index": {
                "type": "integer",
                "description": "0-based slide index (active slide if omitted).",
            },
            "append": {
                "type": "boolean",
                "description": "Append to existing notes instead of replacing (default: false).",
            },
        },
        "required": ["text"],
    }
    uno_services = ["com.sun.star.presentation.PresentationDocument"]
    is_mutation = True

    def execute(self, ctx, **kwargs):
        text = kwargs.get("text", "")
        append = kwargs.get("append", False)

        page = _get_slide(ctx.doc, kwargs.get("page_index"))
        notes_page = page.getNotesPage()
        if notes_page is None or notes_page.getCount() < 2:
            return self._tool_error("No notes page available.")

        notes_shape = notes_page.getByIndex(1)
        if append:
            existing = notes_shape.getString()
            if existing:
                text = existing + "\n" + text
        notes_shape.setString(text)

        return {
            "status": "ok",
            "page_index": kwargs.get("page_index"),
            "message": "Speaker notes updated.",
        }
