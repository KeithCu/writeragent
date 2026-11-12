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

import logging
from typing import Any

from plugin.doc.compound_undo import UndoUnavailable, commit_or_rollback
from plugin.framework.tool import ToolBase, ToolContext

log = logging.getLogger("writeragent.undo")


def _get_undo_manager(doc: Any) -> Any:
    """Return the UndoManager for any document type."""
    if hasattr(doc, "getUndoManager"):
        return doc.getUndoManager()
    raise RuntimeError("Document does not support undo.")


def commit_or_rollback_mutation(
    tool: Any,
    doc: Any,
    undo_title: str,
    mutate: Any,
    undo_unavailable_msg: str,
    rollback_msg: str,
    *,
    code: str = "TOOL_EXECUTION_ERROR",
    **extra_error: Any,
) -> dict[str, Any] | None:
    """Execute mutate inside an undo transaction, reporting tool errors on failure."""
    from plugin.framework.errors import is_disposal

    try:
        outcome = commit_or_rollback(doc, undo_title, mutate)
    except UndoUnavailable:
        return tool._tool_error(
            undo_unavailable_msg,
            code="UNDO_UNAVAILABLE",
        )
    except Exception as e:
        # Mutate failures are returned on UndoOutcome; this path is for rare wrapper faults
        # (e.g. session snapshot) so tools still get extra_error instead of a raw traceback.
        if is_disposal(e):
            raise
        return tool._tool_error(
            f"{rollback_msg} ({e}).",
            code=code,
            top_level=extra_error,
        )
    if outcome.mutate_error is not None:
        mut_exc = outcome.mutate_error
        return tool._tool_error(
            f"{rollback_msg} ({mut_exc}).",
            code=code,
            top_level=extra_error,
        )
    return outcome.error


def _undo_steps(kwargs: dict[str, Any]) -> int:
    try:
        steps = int(kwargs.get("steps") or 1)
    except (TypeError, ValueError):
        steps = 1
    return max(1, min(steps, 10))


def _step_undo_manager(
    tool: ToolBase,
    ctx: ToolContext,
    action_key: str,
    kwargs: dict[str, Any],
) -> dict[str, Any]:
    from plugin.framework.errors import reraise_if_disposed

    steps = _undo_steps(kwargs)
    try:
        um = _get_undo_manager(ctx.doc)
        count = 0
        possible_fn = um.isRedoPossible if action_key == "redone" else um.isUndoPossible
        step_fn = um.redo if action_key == "redone" else um.undo
        for _unused in range(steps):
            if not possible_fn():
                break
            step_fn()
            count += 1
        return {"status": "ok", action_key: count, "can_undo": um.isUndoPossible(), "can_redo": um.isRedoPossible()}
    except Exception as e:
        reraise_if_disposed(e)
        return tool._tool_error(str(e))


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
    _action_key: str = "undone"

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        return _step_undo_manager(self, ctx, self._action_key, kwargs)


class Redo(Undo):
    """Redo the last undone action."""

    name: str | None = "redo"
    description: str = (
        "Redo the last undone change(s) in the document (all document types). Same caution as "
        "undo: the stack is shared with the user's edits — redo only what you yourself just "
        "undid, and tell the user."
    )
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"steps": {"type": "integer", "description": "Number of steps to redo (default: 1)."}}, "required": []}
    _action_key: str = "redone"
