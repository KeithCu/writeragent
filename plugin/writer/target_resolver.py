from __future__ import annotations

from typing import TYPE_CHECKING, Any

from plugin.doc.text_helpers import clone_text_range, live_insert_range, live_selection_range
from plugin.doc.undo import commit_or_rollback_mutation
from plugin.framework.errors import ToolExecutionError, reraise_if_disposed, uno_read
from plugin.writer import search as search_mod
from plugin.writer.edit_review import next_agent_edit_undo_title, run_in_review_session

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from plugin.framework.tool import ToolBase, ToolContext
    from plugin.writer.edit_review import EditReviewSession


def cursor_spanning_range(rng: Any) -> Any:
    cursor = rng.getText().createTextCursorByRange(rng.getStart())
    cursor.gotoRange(rng.getEnd(), True)
    return cursor


def _span_cursor(rng: Any, fail: str) -> Any:
    try:
        return cursor_spanning_range(rng)
    except Exception as e:
        reraise_if_disposed(e)
        raise ValueError(fail % e)


def _pinned_range(ctx: ToolContext) -> Any | None:
    pin = ctx.selection_pin
    if pin is not None and pin.range is not None and pin.applies_to(ctx.doc):
        return pin.range
    return None


def request_selection_range(ctx: ToolContext) -> Any | None:
    """Range a Writer tool acts on for target='selection' or insert-at-caret: ctx.selection_pin.range when set for ctx.doc, else live_selection_range(doc controller)."""
    pinned = _pinned_range(ctx)
    if pinned is not None:
        return pinned
    controller = uno_read(lambda: ctx.doc.getCurrentController(), None)
    return live_selection_range(controller)


def request_insert_range(ctx: ToolContext) -> tuple[Any | None, bool]:
    """(range, after_object) to insert at: the pinned selection, else live_insert_range."""
    pinned = _pinned_range(ctx)
    if pinned is not None:
        return pinned, False
    return live_insert_range(uno_read(lambda: ctx.doc.getCurrentController(), None))


def require_selection_range(ctx: ToolContext) -> Any:
    """request_selection_range(ctx), or ToolExecutionError when the view has no caret."""
    rng = request_selection_range(ctx)
    if rng is None:
        raise ToolExecutionError("No text cursor is available in the document view; nothing was changed.")
    return rng


def update_selection_pin(ctx: ToolContext, text_range: Any) -> None:
    """Move the turn's pinned Writer selection to *text_range* when a pin exists for ctx.doc."""
    pin = ctx.selection_pin
    if pin is not None and text_range is not None and pin.applies_to(ctx.doc):
        pin.range = clone_text_range(text_range)


def resolve_target_cursor(
    ctx: ToolContext,
    target: str,
    old_content: Any,
    *,
    case_sensitive: bool | None = None,
) -> Any:
    """
    Resolves the `target` ("beginning", "end", "selection", "search")
    and returns a valid TextCursor pointing to the desired location.

    If `target` == "search", it finds the text matching `old_content`
    and returns a TextCursor spanning that match. If not found, raises ValueError.

    Returns:
        com.sun.star.text.XTextCursor positioned or spanning the target area.
    """
    doc = ctx.doc

    if target in ("end", "beginning", "full_document"):
        cursor = doc.getText().createTextCursor()
        if target == "end":
            cursor.gotoEnd(False)
        elif target == "beginning":
            cursor.gotoStart(False)
        elif target == "full_document":
            cursor.gotoStart(False)
            cursor.gotoEnd(True)
        return cursor
    elif target == "selection":
        # Resolve the pinned Send-time range (sidebar) or the live selection/caret (MCP);
        # NEVER fall back to document end (a gotoEnd fallback would append edits at the very
        # end and still report ok). Only when unavailable do we raise a clear error, so a
        # headless MCP client that never set a selection is told to use set_selection / target='search'.
        rng = request_selection_range(ctx)
        if rng is None:
            raise ValueError(
                "Could not resolve the current selection. Select text first, or use "
                "target='search' with old_content, or call set_selection.")
        # Build the cursor in the SELECTION's own text object: a selection inside a table cell or
        # text frame is a different XText, and gotoRange on a body cursor raises a raw UNO
        # RuntimeException that escapes callers expecting ValueError. Same pattern as the search
        # branch below.
        return _span_cursor(
            rng,
            "Could not span the current selection (%s). Use target='search' "
            "with old_content, or call set_selection.",
        )

    if target != "search":
        raise ValueError(f"Unknown target {target!r}; use end, selection, beginning, full_document or search.")

    search_string = search_mod.prepare_old_content(old_content)
    if not search_string:
        raise ValueError("old_content is empty after normalization.")

    found = search_mod.find_first_range(doc, search_string, case_sensitive=case_sensitive)

    if found is None:
        raise ValueError(
            search_mod.build_search_not_found_response(
                doc, search_string, refuse_oversized=True, case_sensitive=case_sensitive
            )["message"]
        )

    # Build the cursor in the MATCH's own text object, not the body: a match inside a table cell
    # or text frame is a different XText, and gotoRange across text objects raises (see
    # `_parked_cursor` in html_import.py). createTextCursorByRange on found.getText() keeps the
    # cursor in-scope.
    return _span_cursor(
        found,
        "Could not span the search match (%s). Use a shorter, unique substring.",
    )


def insert_at_target(
    tool: ToolBase,
    ctx: ToolContext,
    kwargs: Mapping[str, Any],
    insert: Callable[[Any], dict[str, Any] | None],
    *,
    what: str,
    replace_match: bool = True,
    undo_title: str | None = None,
    code: str = "TOOL_EXECUTION_ERROR",
) -> dict[str, Any] | None:
    """Resolve `target` / `old_content` / `case_sensitive` from kwargs (the triple
    `target_params` declares), then run one undo step that deletes the search match
    (when replace_match is True and target == "search") and calls insert(cursor).

    insert receives the resolved cursor: collapsed at the insert point when the match was
    deleted untracked, or still spanning the struck match when the deletion was recorded;
    in both cases content attaches at the cursor's start. insert returns None on success or
    an error dict to abort (the step is
    rolled back and that dict is returned unchanged). Returns None on success, else the
    tool error: the resolver's message for a bad target or a search miss;
    UNDO_UNAVAILABLE "Cannot insert {what} as one undo step (no usable undo context);
    the document was not changed."; or `code` with "Failed to insert {what}; the change
    was rolled back (cause)." when insert raises (or, when the change was not undone, a
    message saying part of it may still be in the document). undo_title defaults to
    next_agent_edit_undo_title(). The step runs inside an agent review session, so what
    LibreOffice records of the match deletion and the insert is one tagged change in
    Record/Wait.
    """
    target = kwargs.get("target", "selection")
    try:
        cursor = resolve_target_cursor(
            ctx,
            target,
            kwargs.get("old_content"),
            case_sensitive=kwargs.get("case_sensitive"),
        )
    except ValueError as ve:
        return tool._tool_error(str(ve))
    delete_match = replace_match and target == "search"
    if delete_match:
        refused = search_mod.crossing_match_error(ctx.doc, cursor)
        if refused is not None:
            return refused

    def mutate() -> dict[str, Any] | None:
        if delete_match:
            # Deleting the search match inside the undo bracket ensures the matched text is
            # restored on rollback if the subsequent insert fails. Otherwise, a failed insert
            # would leave the document with missing text.
            cursor.setString("")
        # Upstream LibreOffice SwXText::insertTextContent (sw/source/core/unocore/unotext.cxx)
        # attaches content with bAbsorb=False at xRange->getStart(). When the match is deleted
        # untracked, the cursor is collapsed at the insert point; when the deletion is recorded
        # (or replace_match is False), the cursor still spans the match and the new content
        # attaches at its start.
        return insert(cursor)

    def commit(session: EditReviewSession) -> dict[str, Any] | None:
        return commit_or_rollback_mutation(
            tool,
            ctx.doc,
            undo_title or next_agent_edit_undo_title(),
            lambda: session.record_mutation(mutate),
            f"Cannot insert {what} as one undo step (no usable undo context); the document was not changed.",
            f"Failed to insert {what}; the change was rolled back",
            code=code,
            session=session,
        )

    return run_in_review_session(ctx.doc, ctx.ctx, commit)
