import re as re_mod
import logging
log = logging.getLogger("writeragent.writer")

# backward-compat constants matching content.py
_OLD_CONTENT_BEGIN = "_BEGIN_"
_OLD_CONTENT_END = "_END_"
_OLD_CONTENT_SELECTION = "_SELECTION_"


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
        try:
            controller = doc.getCurrentController()
            sel = controller.getSelection()
            if sel and sel.getCount() > 0:
                rng = sel.getByIndex(0)
                cursor.gotoRange(rng.getStart(), False)
                cursor.gotoRange(rng.getEnd(), True)
            else:
                vc = controller.getViewCursor()
                cursor.gotoRange(vc.getStart(), False)
                cursor.gotoRange(vc.getEnd(), True)
        except Exception:
            cursor.gotoEnd(False)
        return cursor
    elif target == "beginning":
        cursor.gotoStart(False)
        return cursor
    elif target == "full_document":
        cursor.gotoStart(False)
        cursor.gotoEnd(True)
        return cursor

    # Fallback/Backward-compatibility for old_content special values when target=search
    old_stripped = str(old_content).strip() if old_content is not None else ""
    if old_stripped == _OLD_CONTENT_END:
        cursor.gotoEnd(False)
        return cursor
    elif old_stripped == _OLD_CONTENT_SELECTION:
        try:
            controller = doc.getCurrentController()
            sel = controller.getSelection()
            if sel and sel.getCount() > 0:
                rng = sel.getByIndex(0)
                cursor.gotoRange(rng.getStart(), False)
                cursor.gotoRange(rng.getEnd(), True)
            else:
                vc = controller.getViewCursor()
                cursor.gotoRange(vc.getStart(), False)
                cursor.gotoRange(vc.getEnd(), True)
        except Exception:
            cursor.gotoEnd(False)
        return cursor
    elif not old_stripped or old_stripped == _OLD_CONTENT_BEGIN:
        cursor.gotoStart(False)
        return cursor

    # target == "search"
    from plugin.modules.writer.content import _normalize_search_string_for_find, _find_range_by_offset
    from plugin.modules.writer.format_support import content_has_markup, html_to_plain_text

    search_string = old_stripped
    if content_has_markup(search_string):
        search_string = html_to_plain_text(search_string, ctx.ctx, config_svc)

    search_string = _normalize_search_string_for_find(search_string)
    if not search_string:
        raise ValueError("old_content is empty after normalization.")

    sd = doc.createSearchDescriptor()
    sd.SearchRegularExpression = False
    found = None

    for try_string in (search_string, re_mod.sub(r" +", " ", search_string.replace("\n", " ")).strip()):
        if not try_string:
            continue
        sd.SearchString = try_string
        for case_sens in (True, False):
            sd.SearchCaseSensitive = case_sens
            found = doc.findFirst(sd)
            if found is not None:
                break
        if found is not None:
            break

    if found is None:
        found = _find_range_by_offset(doc, search_string)

    if found is None:
        raise ValueError("old_content not found in document. Try a shorter, unique substring.")

    cursor.gotoRange(found.getStart(), False)
    cursor.gotoRange(found.getEnd(), True)
    return cursor
