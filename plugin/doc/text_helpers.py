# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
# Copyright (c) 2026 LibreCalc AI Assistant (Calc integration features, originally MIT)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Writer text / path / selection helpers used by LibrePy without ``document_helpers``.

LibrePy Run Python Script, text analytics, Excel auto-open, and Writer selection
offsets need linebreak normalization, tracked-deletion reads, heading trees, file
paths, selection range calculation, and Writer text-slice reads. Those must not
load ``document_helpers`` → chat context / ``DocumentService``.
"""

from __future__ import annotations

import logging
from typing import TypedDict

import uno

from plugin.doc import doc_type as _doc_type
from plugin.framework.errors import UnoObjectError, check_disposed, safe_call
from plugin.framework.thread_guard import main_thread_only


def normalize_linebreaks(text: str | None) -> str:
    """Ensure all linebreaks use \\n (LF).

    Some UNO APIs (especially on Windows) or clipboard paths can return \\r\\n
    or \\r. This ensures consistent offsets and string length for the LLM.
    """
    if text is None:
        return ""
    # Normalize \r\n -> \n
    text = text.replace("\r\n", "\n")
    # Normalize \n\r (rare but possible) -> \n
    text = text.replace("\n\r", "\n")
    # Normalize remaining \r -> \n
    text = text.replace("\r", "\n")
    return text


# goRight(nCount, bExpand) takes short; max 32767 per call
_GO_RIGHT_CHUNK = 8192


def _writer_char_count(model) -> int:
    """Writer document character count; prefers O(1) CharacterCount over full getString()."""
    try:
        check_disposed(model, "Document Model")
        count = getattr(model, "CharacterCount", None)
        if count is not None:
            return max(0, int(count))
    except Exception:
        pass
    try:
        text = safe_call(model.getText, "Get document text")
        cursor = safe_call(text.createTextCursor, "Create text cursor")
        safe_call(cursor.gotoStart, "Cursor gotoStart", False)
        safe_call(cursor.gotoEnd, "Cursor gotoEnd", True)
        return len(normalize_linebreaks(safe_call(cursor.getString, "Cursor getString")))
    except UnoObjectError:
        logging.getLogger(__name__).exception("_writer_char_count failed")
        return 0


def _char_offset_of_position(model, target_start, doc_len: int) -> int:
    """Character offset of a UNO text position from document start (no prefix getString())."""
    if doc_len <= 0:
        return 0
    try:
        text = safe_call(model.getText, "Get document text")
        cursor = safe_call(text.createTextCursor, "Create text cursor")
        safe_call(cursor.gotoStart, "Cursor gotoStart", False)
        offset = 0
        while offset < doc_len:
            cmp = safe_call(text.compareRegionStarts, "compareRegionStarts", target_start, safe_call(cursor.getStart, "Cursor getStart"))
            if cmp == 0:
                return offset
            if cmp > 0:
                if offset == 0:
                    return 0
                safe_call(cursor.goLeft, "Cursor goLeft", 1, False)
                offset -= 1
                continue
            step = min(_GO_RIGHT_CHUNK, doc_len - offset)
            if step <= 0:
                return offset
            safe_call(cursor.goRight, "Cursor goRight", step, False)
            offset += step
            cmp_after = safe_call(text.compareRegionStarts, "compareRegionStarts", target_start, safe_call(cursor.getStart, "Cursor getStart"))
            if cmp_after >= 0:
                while offset > 0 and safe_call(text.compareRegionStarts, "compareRegionStarts", target_start, safe_call(cursor.getStart, "Cursor getStart")) > 0:
                    safe_call(cursor.goLeft, "Cursor goLeft", 1, False)
                    offset -= 1
                while safe_call(text.compareRegionStarts, "compareRegionStarts", target_start, safe_call(cursor.getStart, "Cursor getStart")) < 0 and offset < doc_len:
                    safe_call(cursor.goRight, "Cursor goRight", 1, False)
                    offset += 1
                return offset
        return doc_len
    except UnoObjectError:
        logging.getLogger(__name__).exception("_char_offset_of_position failed")
        return 0


def _get_writer_selection_positions(model):
    """Return (text, sel_start_pos, sel_end_pos) or None when selection unavailable."""
    try:
        check_disposed(model, "Document Model")
        controller = safe_call(model.getCurrentController, "Get current controller")
        sel = safe_call(controller.getSelection, "Get selection")
        sel_count = 0
        if sel and hasattr(sel, "getCount"):
            sel_count = safe_call(sel.getCount, "Get selection count")
        if not sel or sel_count == 0:
            vc = safe_call(controller.getViewCursor, "Get view cursor")
            rng = vc
        else:
            rng = safe_call(sel.getByIndex, "Get selection by index", 0)
        if not rng or not hasattr(rng, "getStart") or not hasattr(rng, "getEnd"):
            return None
        text = safe_call(model.getText, "Get document text")
        return text, safe_call(rng.getStart, "Get range start"), safe_call(rng.getEnd, "Get range end")
    except UnoObjectError:
        return None


@main_thread_only
def get_selection_range(model):
    """Return (start_offset, end_offset) character positions into the document.
    Cursor (no selection) = same start and end. Returns (0, 0) on error or no text range."""
    try:
        check_disposed(model, "Document Model")
        sel_positions = _get_writer_selection_positions(model)
        if sel_positions is None:
            return (0, 0)
        _text, sel_start_pos, sel_end_pos = sel_positions
        doc_len = _writer_char_count(model)
        start_offset = _char_offset_of_position(model, sel_start_pos, doc_len)
        end_offset = _char_offset_of_position(model, sel_end_pos, doc_len)
        return (start_offset, end_offset)
    except UnoObjectError:
        logging.getLogger(__name__).exception("get_selection_range failed")
        return (0, 0)


class HeadingTreeNode(TypedDict):
    """Shape of nodes returned by :func:`build_heading_tree` (recursive heading tree)."""

    level: int
    text: str
    para_index: int
    children: list["HeadingTreeNode"]
    body_paragraphs: int


def get_string_without_tracked_deletions(text_range) -> str:
    """Return text_range text while skipping tracked deletions when possible."""
    if hasattr(text_range, "_mock_return_value") or type(text_range).__name__ in ("Mock", "MagicMock"):
        return text_range.getString()
    try:
        para_enum = text_range.createEnumeration()
    except Exception:
        return text_range.getString()

    parts: list[str] = []
    try:
        first_para = True
        while para_enum.hasMoreElements():
            para = para_enum.nextElement()
            if not first_para:
                parts.append("\n")
            first_para = False

            try:
                portion_enum = para.createEnumeration()
            except Exception:
                parts.append(para.getString())
                continue

            in_delete = False
            while portion_enum.hasMoreElements():
                portion = portion_enum.nextElement()
                try:
                    try:
                        portion_type = portion.getPropertyValue("TextPortionType")
                    except Exception:
                        portion_type = portion.TextPortionType
                except Exception:
                    continue

                if portion_type == "Redline":
                    try:
                        if str(portion.getPropertyValue("RedlineType")) == "Delete":
                            in_delete = not in_delete
                    except Exception:
                        pass
                    continue

                if in_delete:
                    continue

                try:
                    chunk = portion.getString()
                except Exception:
                    continue
                if chunk:
                    parts.append(chunk)
    except Exception:
        return text_range.getString()

    return "".join(parts)


def get_document_path(model):
    """Return the local filesystem path for the document, or None if not a file URL (e.g. untitled)."""
    try:
        url = model.getURL()
        if not url or not str(url).startswith("file://"):
            return None
        return str(uno.fileUrlToSystemPath(url))
    except Exception as e:
        logging.getLogger(__name__).debug("get_document_path exception: %s", type(e).__name__)
        return None


@main_thread_only
def build_heading_tree(model) -> HeadingTreeNode:
    """Build a hierarchical heading tree. Single pass enumeration."""
    try:
        check_disposed(model, "Document Model")
        text = safe_call(model.getText, "Get document text")
        enum = safe_call(text.createEnumeration, "Create enumeration")
        root: HeadingTreeNode = {"level": 0, "text": "root", "para_index": -1, "children": [], "body_paragraphs": 0}
        stack: list[HeadingTreeNode] = [root]
        para_index = 0

        while safe_call(enum.hasMoreElements, "Check more elements"):
            element = safe_call(enum.nextElement, "Get next element")
            if safe_call(element.supportsService, "Check supportsService Paragraph", "com.sun.star.text.Paragraph"):
                outline_level = 0
                try:
                    outline_level = safe_call(element.getPropertyValue, "Get OutlineLevel", "OutlineLevel")
                except UnoObjectError as e:
                    logging.getLogger(__name__).debug("build_heading_tree could not get OutlineLevel: %s", e)

                if isinstance(outline_level, int) and outline_level > 0:
                    while len(stack) > 1 and int(stack[-1]["level"]) >= outline_level:
                        stack.pop()
                    node: HeadingTreeNode = {
                        "level": outline_level,
                        "text": safe_call(element.getString, "Get paragraph string"),
                        "para_index": para_index,
                        "children": [],
                        "body_paragraphs": 0,
                    }
                    stack[-1]["children"].append(node)
                    stack.append(node)
                else:
                    stack[-1]["body_paragraphs"] += 1
            elif safe_call(element.supportsService, "Check supportsService TextTable", "com.sun.star.text.TextTable"):
                stack[-1]["body_paragraphs"] += 1
            para_index += 1
        return root
    except UnoObjectError:
        logging.getLogger(__name__).exception("build_heading_tree error")
        return {"level": 0, "text": "root", "para_index": -1, "children": [], "body_paragraphs": 0}


def collect_tracked_changes(text_range, max_per_change: int = 300, max_changes: int = 100):
    """Walk text portions and collect tracked insertions/deletions WITH their text, so a reader can
    see what is pending and that it awaits the user's review (rather than the default read, which
    hides deletions and gives no hint that changes are pending).

    Returns a list of ``{"type": "insertion"|"deletion", "text": str}`` in document order. Best-effort:
    returns ``[]`` on any failure. Mirrors get_string_without_tracked_deletions' portion walk, but also
    toggles on Insert redlines and buffers the text of each change instead of dropping deletions."""
    out: list[dict] = []
    if hasattr(text_range, "_mock_return_value") or type(text_range).__name__ in ("Mock", "MagicMock"):
        return out
    try:
        para_enum = text_range.createEnumeration()
    except Exception:
        return out

    in_delete = False
    in_insert = False
    del_buf: list[str] = []
    ins_buf: list[str] = []

    def _flush(buf, kind):
        if buf and len(out) < max_changes:
            out.append({"type": kind, "text": "".join(buf)[:max_per_change]})
        buf.clear()

    try:
        while para_enum.hasMoreElements() and len(out) < max_changes:
            para = para_enum.nextElement()
            try:
                portion_enum = para.createEnumeration()
            except Exception:
                continue
            while portion_enum.hasMoreElements():
                portion = portion_enum.nextElement()
                try:
                    try:
                        ptype = portion.getPropertyValue("TextPortionType")
                    except Exception:
                        ptype = portion.TextPortionType
                except Exception:
                    continue

                if ptype == "Redline":
                    try:
                        rtype = str(portion.getPropertyValue("RedlineType"))
                    except Exception:
                        rtype = ""
                    if rtype == "Delete":
                        if in_delete:
                            _flush(del_buf, "deletion")
                        in_delete = not in_delete
                    elif rtype == "Insert":
                        if in_insert:
                            _flush(ins_buf, "insertion")
                        in_insert = not in_insert
                    continue

                try:
                    chunk = portion.getString()
                except Exception:
                    chunk = ""
                if not chunk:
                    continue
                if in_delete:
                    del_buf.append(chunk)
                elif in_insert:
                    ins_buf.append(chunk)
        _flush(del_buf, "deletion")
        _flush(ins_buf, "insertion")
    except Exception:
        return out
    return out


@main_thread_only
def get_selection_text(model):
    """Return the selected text or None if selection is empty/unavailable/fails. Handles Writer, Calc, Draw."""
    try:
        check_disposed(model, "Document Model")
        controller = safe_call(model.getCurrentController, "Get current controller")
        if not controller:
            return None
        check_disposed(controller, "Controller")

        doc_type = _doc_type.get_document_type(model)

        if doc_type == _doc_type.DocumentType.WRITER:
            sel = safe_call(controller.getSelection, "Get selection")
            sel_count = 0
            if sel and hasattr(sel, "getCount"):
                sel_count = safe_call(sel.getCount, "Get selection count")
            if not sel or sel_count == 0:
                vc = safe_call(controller.getViewCursor, "Get view cursor")
                if vc:
                    check_disposed(vc, "View Cursor")
                    return safe_call(vc.getString, "Get view cursor string")
            else:
                rng = safe_call(sel.getByIndex, "Get selection by index", 0)
                if rng:
                    check_disposed(rng, "Selection Range")
                    return safe_call(rng.getString, "Get selection string")
        elif doc_type == _doc_type.DocumentType.CALC:
            selection = safe_call(controller.getSelection, "Get selection")
            if selection:
                if hasattr(selection, "getString"):
                    return safe_call(selection.getString, "Get selection string")
        elif doc_type in (_doc_type.DocumentType.DRAW, _doc_type.DocumentType.IMPRESS):
            selection = safe_call(controller.getSelection, "Get selection")
            if selection and hasattr(selection, "getCount"):
                count = safe_call(selection.getCount, "Get selection count")
                parts = []
                for i in range(count):
                    shape = safe_call(selection.getByIndex, "Get selection shape", i)
                    if shape and hasattr(shape, "getString"):
                        parts.append(safe_call(shape.getString, "Get shape string"))
                if parts:
                    return "\n".join(parts)
    except Exception:
        pass
    return None


def get_document_end(model, max_chars=4000):
    """Get the last max_chars of the document."""
    try:
        check_disposed(model, "Document Model")
        text = safe_call(model.getText, "Get document text")
        cursor = safe_call(text.createTextCursor, "Create text cursor")
        safe_call(cursor.gotoEnd, "Cursor gotoEnd", False)
        safe_call(cursor.gotoStart, "Cursor gotoStart", True)  # expand backward to select from start to end
        full = get_string_without_tracked_deletions(cursor)
        if len(full) <= max_chars:
            return full
        return full[-max_chars:]
    except UnoObjectError:
        logging.getLogger(__name__).exception("get_document_end failed")
        return ""


def _read_writer_text_slice(model, start_offset: int, length: int) -> str:  # pyright: ignore[reportUnusedFunction]
    """Read up to *length* characters from *start_offset* without loading the full document.

    Used by ``document_helpers.get_document_context_for_chat`` (Writer excerpts).
    """
    if length <= 0:
        return ""
    end_offset = start_offset + length
    cursor = get_text_cursor_at_range(model, start_offset, end_offset)
    if cursor is None:
        return ""
    # cursor.getString() concatenates tracked deletions as plain text; enumerate portions instead.
    return normalize_linebreaks(get_string_without_tracked_deletions(cursor))


def get_full_writer_text(model, max_chars):
    """Prefix of Writer body text, truncated. Hides tracked deletions."""
    doc_len = _writer_char_count(model)
    take = min(doc_len, max_chars)
    excerpt = _read_writer_text_slice(model, 0, take)
    if doc_len > max_chars:
        excerpt += "\n\n[... document truncated ...]"
    return excerpt


def _writer_excerpt_overlaps_selection(model, excerpt_start: int, excerpt_end: int, sel_start_pos, sel_end_pos) -> bool:
    """True when selection UNO range overlaps [excerpt_start, excerpt_end) character window."""
    exc_cursor = get_text_cursor_at_range(model, excerpt_start, excerpt_end)
    if exc_cursor is None:
        return False
    text = safe_call(model.getText, "Get document text")
    exc_start = safe_call(exc_cursor.getStart, "Excerpt getStart")
    exc_end = safe_call(exc_cursor.getEnd, "Excerpt getEnd")
    if safe_call(text.compareRegionStarts, "compareRegionStarts sel_end exc_start", sel_end_pos, exc_start) > 0:
        return False
    if safe_call(text.compareRegionStarts, "compareRegionStarts exc_end sel_start", exc_end, sel_start_pos) > 0:
        return False
    return True


def _writer_selection_overlaps_windows(model, windows: list[tuple[int, int]], sel_start_pos, sel_end_pos) -> bool:  # pyright: ignore[reportUnusedFunction]
    for win_start, win_end in windows:
        if _writer_excerpt_overlaps_selection(model, win_start, win_end, sel_start_pos, sel_end_pos):
            return True
    return False


def get_document_length(model):
    """Return total character length of the document. Returns 0 on error."""
    try:
        check_disposed(model, "Document Model")
        if _doc_type.get_document_type(model) == _doc_type.DocumentType.WRITER:
            return _writer_char_count(model)
        text = safe_call(model.getText, "Get document text")
        cursor = safe_call(text.createTextCursor, "Create text cursor")
        safe_call(cursor.gotoStart, "Cursor gotoStart", False)
        safe_call(cursor.gotoEnd, "Cursor gotoEnd", True)
        length = len(normalize_linebreaks(safe_call(cursor.getString, "Cursor getString")))
        return length
    except UnoObjectError:
        logging.getLogger(__name__).exception("get_document_length failed")
        return 0


def get_text_cursor_at_range(model, start_offset, end_offset):
    """Return a text cursor that selects the character range [start_offset, end_offset).
    The cursor is positioned at start and expanded to end so caller can setString('') and insert.
    goRight is used in chunks because UNO's goRight takes short (max 32767).
    Returns None on error or invalid range."""
    try:
        check_disposed(model, "Document Model")
        doc_len = get_document_length(model)
        start_offset = max(0, min(start_offset, doc_len))
        end_offset = max(0, min(end_offset, doc_len))
        if start_offset > end_offset:
            start_offset, end_offset = end_offset, start_offset
        text = safe_call(model.getText, "Get document text")
        cursor = safe_call(text.createTextCursor, "Create text cursor")
        safe_call(cursor.gotoStart, "Cursor gotoStart", False)
        # Move to start_offset in chunks
        remaining = start_offset
        while remaining > 0:
            n = min(remaining, _GO_RIGHT_CHUNK)
            safe_call(cursor.goRight, "Cursor goRight", n, False)
            remaining -= n
        # Expand selection by (end_offset - start_offset)
        remaining = end_offset - start_offset
        while remaining > 0:
            n = min(remaining, _GO_RIGHT_CHUNK)
            safe_call(cursor.goRight, "Cursor goRight", n, True)
            remaining -= n
        return cursor
    except UnoObjectError:
        logging.getLogger(__name__).exception("get_text_cursor_at_range failed")
        return None
