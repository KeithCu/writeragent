"""Writer helper operations used by tools.

Low-level UNO helpers for paragraph navigation, selection ranges,
and text cursor manipulation. Tools delegate here rather than
duplicating UNO details.
"""

import logging

log = logging.getLogger("localwriter.writer")


def find_paragraph_for_range(anchor, para_ranges, text_obj):
    """Return the 0-based paragraph index that contains *anchor*.

    Iterates *para_ranges* and uses ``compareRegionStarts`` on
    *text_obj* to locate the paragraph whose start/end brackets
    the anchor's start position.

    Returns 0 if no match is found.
    """
    try:
        match_start = anchor.getStart()
        for i, para in enumerate(para_ranges):
            try:
                cmp_start = text_obj.compareRegionStarts(
                    match_start, para.getStart()
                )
                cmp_end = text_obj.compareRegionStarts(
                    match_start, para.getEnd()
                )
                if cmp_start <= 0 and cmp_end >= 0:
                    return i
            except Exception:
                continue
    except Exception:
        log.debug("find_paragraph_for_range: failed", exc_info=True)
    return 0


def get_selection_range(model):
    """Return ``(start_offset, end_offset)`` character positions of the
    current selection (or cursor insertion point).

    Returns ``(0, 0)`` on error or when no text range is available.
    """
    try:
        sel = model.getCurrentController().getSelection()
        if not sel or sel.getCount() == 0:
            rng = model.getCurrentController().getViewCursor()
        else:
            rng = sel.getByIndex(0)

        if not rng or not hasattr(rng, "getStart") or not hasattr(rng, "getEnd"):
            return (0, 0)

        text = model.getText()
        cursor = text.createTextCursor()
        cursor.gotoStart(False)
        cursor.gotoRange(rng.getStart(), True)
        start_offset = len(cursor.getString())

        cursor.gotoStart(False)
        cursor.gotoRange(rng.getEnd(), True)
        end_offset = len(cursor.getString())

        return (start_offset, end_offset)
    except Exception:
        log.debug("get_selection_range: failed", exc_info=True)
        return (0, 0)


# goRight(nCount, bExpand) takes a short; max 32767 per call.
_GO_RIGHT_CHUNK = 8192


def get_text_cursor_at_range(model, start, end):
    """Create a text cursor that selects the character range ``[start, end)``.

    The cursor is positioned at *start* and expanded to *end* so the
    caller can ``setString("")`` or insert content.  ``goRight`` is
    used in chunks because UNO's ``goRight`` takes a short (max 32767).

    Returns ``None`` on error or invalid range.
    """
    try:
        doc_len = _doc_length(model)
        start = max(0, min(start, doc_len))
        end = max(0, min(end, doc_len))
        if start > end:
            start, end = end, start

        text = model.getText()
        cursor = text.createTextCursor()
        cursor.gotoStart(False)

        remaining = start
        while remaining > 0:
            n = min(remaining, _GO_RIGHT_CHUNK)
            cursor.goRight(n, False)
            remaining -= n

        remaining = end - start
        while remaining > 0:
            n = min(remaining, _GO_RIGHT_CHUNK)
            cursor.goRight(n, True)
            remaining -= n

        return cursor
    except Exception:
        log.debug("get_text_cursor_at_range: failed", exc_info=True)
        return None


from plugin.framework.document import get_document_length as _doc_length
