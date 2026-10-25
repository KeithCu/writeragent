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


class UndoUnavailable(RuntimeError):
    """Raised when an undo manager cannot be entered or is locked."""


def _undo_titles(mgr: Any) -> tuple[str, ...] | None:
    """Undo-stack titles, or None when they cannot be read (rollback is then refused)."""
    from plugin.framework.errors import reraise_if_disposed

    try:
        titles = mgr.getAllUndoActionTitles()
    except Exception as exc:
        reraise_if_disposed(exc)
        return None
    return tuple(titles or ())


def commit_or_rollback(doc: Any, title: str, mutate: Any) -> dict[str, Any] | None:
    """Run mutate inside an undo context with title.

    Missing or locked undo manager raises UndoUnavailable.
    Rolls back on error dict or exception from mutate, and re-raises real disposal.
    `mutate` returns an error dict or None; any dict is treated as an error.
    """
    from plugin.framework.errors import is_real_disposal, reraise_if_disposed

    try:
        mgr = _get_undo_manager(doc)
    except Exception as exc:
        reraise_if_disposed(exc)
        raise UndoUnavailable("Document does not support undo.") from exc

    if mgr is None:
        raise UndoUnavailable("Undo manager is missing.")

    try:
        if mgr.isLocked():
            raise UndoUnavailable("Undo manager is locked.")
        before = _undo_titles(mgr)
        mgr.enterUndoContext(title)
    except UndoUnavailable:
        raise
    except Exception as exc:
        reraise_if_disposed(exc)
        raise UndoUnavailable(f"Failed to enter undo context: {exc}") from exc

    error: dict[str, Any] | None = None
    exc_cause: Exception | None = None
    try:
        res = mutate()
        if isinstance(res, dict):
            error = res
    except Exception as exc:
        exc_cause = exc
    finally:
        left = False
        try:
            mgr.leaveUndoContext()
            left = True
        except Exception as exc:
            if is_real_disposal(exc):
                exc_cause = exc
            elif error is None and exc_cause is None:
                error = {
                    "status": "error",
                    "code": "UNDO_CONTEXT_LEAK",
                    "message": f"Could not close the undo context; the document may stay in one undo group ({exc}).",
                }
        failed = error is not None or exc_cause is not None
        if failed and left and before is not None and not (
            exc_cause is not None and is_real_disposal(exc_cause)
        ):
            try:
                titles = tuple(mgr.getAllUndoActionTitles() or ())
                if titles != before and titles and titles[0] == title:
                    mgr.undo()
            except Exception as rollback_exc:
                reraise_if_disposed(rollback_exc)
                # Keep the original cause and use a code distinct from a clean rollback.
                cause = str(error.get("message")) if error and error.get("message") else str(exc_cause)
                error = {
                    "status": "error",
                    "code": "ROLLBACK_FAILED",
                    "message": f"Rollback failed; the document may be only partly edited ({rollback_exc}). Cause: {cause}",
                }
                exc_cause = None

    if exc_cause is not None:
        reraise_if_disposed(exc_cause)
        raise exc_cause
    return error


def commit_or_rollback_mutation(
    tool: Any,
    doc: Any,
    undo_title: str,
    mutate: Any,
    undo_unavailable_msg: str,
    rollback_msg: str,
    **extra_error: Any,
) -> dict[str, Any] | None:
    """Execute mutate inside an undo transaction, reporting tool errors on failure."""
    from plugin.framework.errors import is_real_disposal

    try:
        error = commit_or_rollback(doc, undo_title, mutate)
        if error is not None:
            return error
    except UndoUnavailable:
        return tool._tool_error(
            undo_unavailable_msg,
            code="UNDO_UNAVAILABLE",
        )
    except Exception as e:
        if is_real_disposal(e):
            raise
        return tool._tool_error(
            f"{rollback_msg} ({e}).",
            **extra_error,
        )
    return None


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

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        return _step_undo_manager(self, ctx, "undone", kwargs)


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
        return _step_undo_manager(self, ctx, "redone", kwargs)
