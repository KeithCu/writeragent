# Copyright (c) David Berlioz
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Undo/redo tools for all document types via XUndoManager.

Re-enabled (was ToolBaseDummy): with review mode off an agent's bad edit was unrecoverable
except by the user's own Ctrl+Z — the agent had no rollback at all. The undo stack is SHARED
with the user's manual edits, so the descriptions steer the model to undo only its own last
action and to tell the user."""

from __future__ import annotations

from typing import Any

from plugin.framework.tool import ToolBase, ToolContext


def _get_undo_manager(doc: Any) -> Any:
    """Return the UndoManager for any document type."""
    if hasattr(doc, "getUndoManager"):
        return doc.getUndoManager()
    raise RuntimeError("Document does not support undo.")


class Undo(ToolBase):
    """Undo the last action."""

    name: str | None = "undo"
    description: str = (
        "Undo the last change(s) in the document (all document types). CAUTION: the undo stack "
        "interleaves YOUR edits with the user's own edits — call this only immediately after an "
        "edit of yours went wrong, undo exactly the steps you caused, and tell the user what you "
        "undid. Result reports undone plus can_undo/can_redo."
    )
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"steps": {"type": "integer", "description": "Number of steps to undo (default: 1)."}}, "required": []}
    uno_services: list[str] | None = None
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        steps = kwargs.get("steps", 1)
        try:
            um = _get_undo_manager(ctx.doc)
            undone = 0
            for _unused in range(steps):
                if not um.isUndoPossible():
                    break
                um.undo()
                undone += 1
            return {"status": "ok", "undone": undone, "can_undo": um.isUndoPossible(), "can_redo": um.isRedoPossible()}
        except Exception as e:
            return self._tool_error(str(e))


class Redo(ToolBase):
    """Redo the last undone action."""

    name: str | None = "redo"
    description: str = (
        "Redo the last undone change(s) in the document (all document types). Same caution as "
        "undo: the stack is shared with the user's edits — redo only what you yourself just "
        "undid, and tell the user."
    )
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"steps": {"type": "integer", "description": "Number of steps to redo (default: 1)."}}, "required": []}
    uno_services: list[str] | None = None
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        steps = kwargs.get("steps", 1)
        try:
            um = _get_undo_manager(ctx.doc)
            redone = 0
            for _unused in range(steps):
                if not um.isRedoPossible():
                    break
                um.redo()
                redone += 1
            return {"status": "ok", "redone": redone, "can_undo": um.isUndoPossible(), "can_redo": um.isRedoPossible()}
        except Exception as e:
            return self._tool_error(str(e))
