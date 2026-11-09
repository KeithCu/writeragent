from __future__ import annotations

from typing import TYPE_CHECKING, Any

from plugin.framework.errors import reraise_if_disposed, uno_read
from plugin.writer import search as search_mod

if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext


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
        # Resolve the explicit selection, else the view cursor. NEVER silently fall back to the end
        # of the document (the old `except: gotoEnd` appended edits at the very end and still
        # reported ok). Only when BOTH are unavailable do we raise a clear error, so a headless MCP
        # client that never set a selection is told to use set_selection / target='search'.
        controller = uno_read(doc.getCurrentController, None)
        rng = None
        if controller is not None:
            from plugin.doc.text_helpers import selected_text_range

            rng = uno_read(lambda: selected_text_range(controller), None)
            if rng is None:
                rng = uno_read(controller.getViewCursor, None)
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

    find_kw: dict[str, Any] = {}
    if case_sensitive is not None:
        find_kw["case_sensitive"] = search_mod.resolve_case_sensitive(case_sensitive)

    found = search_mod.find_first_range(doc, search_string, **find_kw)

    if found is None:
        shape_name = search_mod.drawing_shape_containing(doc, search_string, **find_kw)
        raise ValueError(
            search_mod.search_miss_message(doc, search_string, shape_name=shape_name)
        )

    # Build the cursor in the MATCH's own text object, not the body: a match inside a table cell
    # or text frame is a different XText, and gotoRange across text objects raises (format.py
    # documents this). createTextCursorByRange on found.getText() keeps the cursor in-scope.
    return _span_cursor(
        found,
        "Could not span the search match (%s). Use a shorter, unique substring.",
    )
