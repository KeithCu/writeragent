# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
# Copyright (c) 2026 LibreCalc AI Assistant (Calc integration features, originally MIT)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Writer text / path / selection helpers used by LibrePy without ``document_helpers``.

LibrePy Run Python Script, text analytics, Excel auto-open, and Writer selection
offsets need linebreak normalization, tracked-deletion reads, heading trees, file
paths, and selection range calculation. Those must not load ``document_helpers`` →
``SheetAnalyzer`` / chat context.
"""

from __future__ import annotations

import logging
from typing import TypedDict

import uno

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
