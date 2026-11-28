# Copyright (c) David Berlioz
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Undo/redo tools for all document types via XUndoManager.

With review mode off, these tools are the agent's only way to roll back its own bad edit;
otherwise only the user's Ctrl+Z can. The undo stack is SHARED with the user's manual edits,
so the descriptions steer the model to undo only its own last action and to tell the user."""

from __future__ import annotations

from typing import Any

from plugin.doc.compound_undo import UndoUnavailable, commit_or_rollback
from plugin.framework.errors import is_disposal, make_tool_error, reraise_if_disposed
from plugin.framework.i18n import N_, _
from plugin.framework.tool import ToolBase, ToolContext

# Undo title of WriterAgent's own review popup when it groups several Accept or Reject
# dispatches into one step (inline_review._grouped_resolve_undo).
RESOLVE_AGENT_CHANGES_UNDO_TITLE = N_("Resolve agent changes")
# Undo titles of a native Accept or Reject in an English UI (STR_ACCEPT_REDLINE and
# STR_REJECT_REDLINE, sw/inc/strings.hrc); other UI languages translate them.
_NATIVE_RESOLVE_TITLE_PREFIXES = ("Accept change: ", "Reject change: ")


def is_resolve_undo_title(title: str) -> bool:
    """True when the undo step titled *title* is an Accept or Reject of a tracked change.

    That is the user's review decision, or one ``tracking_manage`` dispatched for them; the title
    does not say which.
    """
    return title == _(RESOLVE_AGENT_CHANGES_UNDO_TITLE) or title.startswith(_NATIVE_RESOLVE_TITLE_PREFIXES)


def commit_or_rollback_mutation(
    doc: Any,
    undo_title: str,
    mutate: Any,
    undo_unavailable_msg: str,
    rollback_msg: str,
    *,
    code: str = "TOOL_EXECUTION_ERROR",
    session: Any | None = None,
    **extra_error: Any,
) -> dict[str, Any] | None:
    """Run mutate inside an undo transaction and turn a failure into a tool error.

    ``code`` is the error code used when mutate fails or the wrapper itself fails.
    ``extra_error`` keys (for example ``deleted=0``) sit at the top level of that error,
    except when ``outcome.nothing_landed`` is False (counts are not known to be true then).
    ``session`` is a review session whose records are dropped on rollback.
    Undo-manager problems use UNDO_UNAVAILABLE, and any ``UndoOutcome.error``
    (including UNDO_CONTEXT_LEAK and ROLLBACK_FAILED) is returned unchanged.
    """
    try:
        outcome = commit_or_rollback(doc, undo_title, mutate, session=session)
    except UndoUnavailable:
        return make_tool_error(undo_unavailable_msg, code="UNDO_UNAVAILABLE")
    except Exception as e:
        # Mutate failures come back on UndoOutcome; this arm catches rare faults in the
        # wrapper itself (such as the session snapshot), so the tool still gets a
        # structured error with its extra fields instead of a raw traceback.
        if is_disposal(e):
            raise
        cause: Exception = e
    else:
        if outcome.mutate_error is None:
            return outcome.error
        if not outcome.group_closed:
            return make_tool_error(
                f"The change failed and its undo step could not be closed, so part of it may still be in the document ({outcome.mutate_error}).",
                code=code,
            )
        if not outcome.nothing_landed:
            return make_tool_error(
                f"The change failed and could not be rolled back, so part of it may still be in the document ({outcome.mutate_error}).",
                code=code,
            )
        cause = outcome.mutate_error
    return make_tool_error(
        f"{rollback_msg} ({cause}).",
        code=code,
        top_level=extra_error,
    )


def _undo_steps(kwargs: dict[str, Any]) -> int:
    """Requested step count clamped to 1..10. The registry validator has already
    rejected a non-integer ``steps``; a missing, null or 0 value means one step."""
    return max(1, min(kwargs.get("steps") or 1, 10))


def _step_undo_manager(
    ctx: ToolContext,
    action_key: str,
    kwargs: dict[str, Any],
) -> dict[str, Any]:
    steps = _undo_steps(kwargs)
    try:
        if not hasattr(ctx.doc, "getUndoManager"):
            return make_tool_error("Document does not support undo.")
        um = ctx.doc.getUndoManager()
        count = 0
        possible_fn = um.isRedoPossible if action_key == "redone" else um.isUndoPossible
        step_fn = um.redo if action_key == "redone" else um.undo
        for _unused in range(steps):
            if not possible_fn():
                break
            if action_key == "undone" and is_resolve_undo_title(um.getCurrentUndoActionTitle()):
                # Undoing a Reject would put a rejected agent change back as pending. tracking_manage
                # dispatches the native command too, so its own resolve carries the same title.
                return make_tool_error(
                    "The next undo step is an Accept or Reject of a tracked change (the user's, or one "
                    "made through tracking_manage), so it was not undone. Ask the user before changing "
                    "that review decision.",
                    code="UNDO_USER_REVIEW",
                    top_level={action_key: count},
                )
            step_fn()
            count += 1
        return {"status": "ok", action_key: count, "can_undo": um.isUndoPossible(), "can_redo": um.isRedoPossible()}
    except Exception as e:
        reraise_if_disposed(e)
        return make_tool_error(str(e))


class Undo(ToolBase):
    """Undo the last action."""

    name: str | None = "document_undo"
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
        return _step_undo_manager(ctx, self._action_key, kwargs)


class Redo(Undo):
    """Redo the last undone action."""

    name: str | None = "document_redo"
    description: str = (
        "Redo the last undone change(s) in the document (all document types). Same caution as "
        "document_undo: the stack is shared with the user's edits — redo only what you yourself just "
        "undid, and tell the user."
    )
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"steps": {"type": "integer", "description": "Number of steps to redo (default: 1)."}}, "required": []}
    _action_key: str = "redone"
