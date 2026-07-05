import logging

from plugin.writer import search as search_mod
from .content import _find_first_range
from .format import content_has_markup, html_to_plain_text

log = logging.getLogger("writeragent.writer")


def resolve_target_cursor(ctx, target, old_content):
    """
    Resolves the `target` ("beginning", "end", "selection", "search")
    and returns a valid TextCursor pointing to the desired location.

    If `target` == "search", it finds the text matching `old_content`
    and returns a TextCursor spanning that match. If not found, raises ValueError.

    Returns:
        com.sun.star.text.XTextCursor positioned or spanning the target area.
    """
    doc = ctx.doc
    text = doc.getText()
    cursor = text.createTextCursor()
    config_svc = ctx.services.get("config")

    if target == "end":
        cursor.gotoEnd(False)
        return cursor
    elif target == "selection":
        # Resolve the explicit selection, else the view cursor. NEVER silently fall back to the end
        # of the document (the old `except: gotoEnd` appended edits at the very end and still
        # reported ok). Only when BOTH are unavailable do we raise a clear error, so a headless MCP
        # client that never set a selection is told to use set_selection / target='search'.
        controller = None
        try:
            controller = doc.getCurrentController()
        except Exception:
            controller = None
        rng = None
        if controller is not None:
            try:
                sel = controller.getSelection()
                if sel and hasattr(sel, "getCount") and int(sel.getCount()) > 0:
                    rng = sel.getByIndex(0)
            except Exception:
                rng = None
            if rng is None:
                try:
                    rng = controller.getViewCursor()
                except Exception:
                    rng = None
        if rng is None:
            raise ValueError(
                "Could not resolve the current selection. Select text first, or use "
                "target='search' with old_content, or call set_selection.")
        # Build the cursor in the SELECTION's own text object: a selection inside a table cell or
        # text frame is a different XText, and gotoRange on a body cursor raises a raw UNO
        # RuntimeException that escapes callers expecting ValueError. Same pattern as the search
        # branch below.
        try:
            scursor = rng.getText().createTextCursorByRange(rng.getStart())
            scursor.gotoRange(rng.getEnd(), True)
            return scursor
        except Exception as e:
            raise ValueError("Could not span the current selection (%s). Use target='search' "
                             "with old_content, or call set_selection." % e)
    elif target == "beginning":
        cursor.gotoStart(False)
        return cursor
    elif target == "full_document":
        cursor.gotoStart(False)
        cursor.gotoEnd(True)
        return cursor

    # target == "search" or fallback to search if old_content is provided

    # target == "search"

    search_string = str(old_content).strip() if old_content is not None else ""
    if content_has_markup(search_string):
        search_string = html_to_plain_text(search_string, ctx.ctx, config_svc)

    search_string = search_mod.normalize_search_string_for_find(search_string)
    if not search_string:
        raise ValueError("old_content is empty after normalization.")

    found = _find_first_range(doc, search_string)

    if found is None:
        shape_name = search_mod.drawing_shape_containing(doc, search_string)
        if shape_name:
            raise ValueError(
                "old_content is only inside a drawing shape / floating text box ('%s'). "
                "Edit it via the shapes toolset (delegate_to_specialized_writer_toolset domain='shapes')."
                % shape_name)
        raise ValueError("old_content not found in document. Try a shorter, unique substring.")

    # Build the cursor in the MATCH's own text object, not the body: a match inside a table cell
    # or text frame is a different XText, and gotoRange across text objects raises (format.py
    # documents this). createTextCursorByRange on found.getText() keeps the cursor in-scope.
    ftext = found.getText()
    fcursor = ftext.createTextCursorByRange(found.getStart())
    fcursor.gotoRange(found.getEnd(), True)
    return fcursor
