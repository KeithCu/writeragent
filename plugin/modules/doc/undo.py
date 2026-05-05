# Copyright (c) David Berlioz
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Undo/redo tools for all document types via XUndoManager."""

from plugin.framework.tool_base import ToolBaseDummy


def _get_undo_manager(doc):
    """Return the UndoManager for any document type."""
    if hasattr(doc, "getUndoManager"):
        return doc.getUndoManager()
    raise RuntimeError("Document does not support undo.")


class Undo(ToolBaseDummy):
    """Undo the last action."""

    name = "undo"
    description = "Undo the last action in the document. Can undo multiple steps. Works on all document types."
    parameters = {"type": "object", "properties": {"steps": {"type": "integer", "description": "Number of steps to undo (default: 1)."}}, "required": []}
    uno_services = None
    is_mutation = True

    def execute(self, ctx, **kwargs):
        steps = kwargs.get("steps", 1)
        try:
            um = _get_undo_manager(ctx.doc)
            undone = 0
            for _ in range(steps):
                if not um.isUndoPossible():
                    break
                um.undo()
                undone += 1
            return {"status": "ok", "undone": undone, "can_undo": um.isUndoPossible(), "can_redo": um.isRedoPossible()}
        except Exception as e:
            return self._tool_error(str(e))


class Redo(ToolBaseDummy):
    """Redo the last undone action."""

    name = "redo"
    description = "Redo the last undone action in the document. Can redo multiple steps. Works on all document types."
    parameters = {"type": "object", "properties": {"steps": {"type": "integer", "description": "Number of steps to redo (default: 1)."}}, "required": []}
    uno_services = None
    is_mutation = True

    def execute(self, ctx, **kwargs):
        steps = kwargs.get("steps", 1)
        try:
            um = _get_undo_manager(ctx.doc)
            redone = 0
            for _ in range(steps):
                if not um.isRedoPossible():
                    break
                um.redo()
                redone += 1
            return {"status": "ok", "redone": redone, "can_undo": um.isUndoPossible(), "can_redo": um.isRedoPossible()}
        except Exception as e:
            return self._tool_error(str(e))
