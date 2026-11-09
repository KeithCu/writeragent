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

Offsets: visible-text offsets (``get_string_without_tracked_deletions``,
``get_document_length``, ``_read_writer_text_slice`` results) and cursor-step
offsets (``_writer_char_count``, ``get_text_cursor_at_range*``,
``get_selection_range``) are different spaces; convert with a slice read.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, TypedDict

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator, Mapping

import uno

from plugin.framework import doc_type as _doc_type
from plugin.framework.errors import (
    DocumentDisposedError,
    UnoObjectError,
    reraise_if_disposed,
    uno_call,
    uno_read,
)
from plugin.framework.thread_guard import main_thread_only
from plugin.framework.uno_context import enum_has_more, uno_same
from plugin.framework import url_utils as _url_utils

log = logging.getLogger(__name__)

_PARAGRAPH_SERVICE = "com.sun.star.text.Paragraph"
# uno_read default where None is a real UNO result.
FAILED: Any = object()


def iter_elements(enum: Any) -> Generator[Any, None, None]:
    """Safely iterate a UNO XEnumeration."""
    if enum is None:
        return
    while enum_has_more(enum):
        yield enum.nextElement()


def iter_elements_limited(enum: Any, limit: int) -> Generator[Any, None, None]:
    """Yield at most *limit* elements without consuming the next when capped.

    A ``for`` loop over ``iter_elements`` that breaks on a count cap still
    pulls one past the cap before the body runs, so ``hasMoreElements()`` looks
    exhausted when ``record_walk_cap`` checks it. Use this for capped walks.
    """
    if enum is None or limit <= 0:
        return
    seen = 0
    while enum_has_more(enum):
        if seen >= limit:
            break
        seen += 1
        yield enum.nextElement()


def walk_cap_warning(kind: str, seen: int, limit: int) -> str:
    """Agent-facing note when a text walk stopped at a hard cap.

    The walk still returns what it collected; anything after the cap was not read, so
    formatting (or a header scan) may be incomplete. DO: treat the result as partial and
    re-read a smaller range rather than assuming the rest is unformatted.
    """
    return (
        "Walk stopped after %d %s (cap %d). Later content was not read, so formatting "
        "may be incomplete. Read a smaller range (or split the target) rather than "
        "treating this result as the whole picture." % (seen, kind, limit)
    )


def record_walk_cap(enum: Any, seen: int, limit: int, kind: str, dest: list[str] | None = None) -> bool:
    """Log (and optionally collect) a walk-cap warning. Returns True when the cap was hit."""
    if seen < limit or enum is None or not enum_has_more(enum):
        return False
    msg = walk_cap_warning(kind, seen, limit)
    log.warning("%s", msg)
    if dest is not None:
        dest.append(msg)
    return True


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


def go_right(cursor: Any, n: int, expand: bool) -> bool:
    """Move (expand=False) or extend (expand=True) the cursor right by *n* chars, in chunks of
    _GO_RIGHT_CHUNK (UNO caps the count at a C++ short). Returns True only if the FULL *n* was
    consumed; False if goRight stopped early (end of text / an unexpected stop), so the caller can
    refuse to edit at a wrong offset instead of silently landing short."""
    while n > 0:
        step = n if n < _GO_RIGHT_CHUNK else _GO_RIGHT_CHUNK
        if not cursor.goRight(step, expand):
            return False
        n -= step
    return True


def _writer_char_count(model: Any) -> int:
    """Length of the Writer body in cursor steps: the space every offset here uses
    (``get_text_cursor_at_range``, ``get_selection_range``, the chat excerpt reads).

    The ``CharacterCount`` statistic leaves out paragraph breaks
    and text deleted by pending tracked changes (54 chars read as
    53; 60 pending edits, 2385 against offsets up to 3404). Adding
    ``ParagraphCount - 1`` puts the breaks back but not the deleted
    text, which the cursor still steps over. Callers treat this
    length as the end of the offset space, so the chat's [DOCUMENT
    END] excerpt drops the end of the
    document, and a selection at the end comes back as (length,
    length). The statistic is not cheap either: after an edit it
    recomputes (253 ms on a 348k-char body; this walk took 55 ms).

    Future: if huge-document chat feels slow, cache this length (and the visible
    ``get_document_length`` walk) until the next edit instead of walking every call.
    """
    try:
        text = uno_call(model.getText, "Get document text")
        cursor = uno_call(text.createTextCursor, "Create text cursor")
        uno_call(lambda: cursor.gotoStart(False), "Cursor gotoStart")
        count = 0
        step = _GO_RIGHT_CHUNK
        while step:
            probe = uno_call(
                lambda: text.createTextCursorByRange(uno_call(cursor.getStart, "Cursor getStart")),
                "Create probe cursor",
            )
            # A goRight that cannot go the full distance still moves to the end and returns
            # False, so retry a smaller step from the last position it fully reached.
            if uno_call(lambda: probe.goRight(step, False), "Cursor goRight") is True:
                cursor = probe
                count += step
            else:
                step //= 2
        return count
    except UnoObjectError:
        log.exception("_writer_char_count failed")
        return 0


def _position_text_is_body(model: Any, position: Any, step_label: str) -> bool:
    get_pos_text = getattr(position, "getText", None)
    if not callable(get_pos_text):
        return True
    pos_text = uno_call(get_pos_text, step_label)
    body = uno_call(model.getText, "Get document text")
    return uno_same(pos_text, body)


def _char_offset_of_position(model: Any, target_start: Any, doc_len: int) -> int:
    """Cursor-step offset of a body text position from the document start, or -1 when the position is outside the body or cannot be measured."""
    if not _position_text_is_body(model, target_start, "Get position text"):
        # Selection is in a table cell, frame, or header. compareRegionStarts
        # Nested vs body compare failures return -1, not 0 (document start).
        return -1
    if doc_len <= 0:
        return 0
    try:
        text = uno_call(model.getText, "Get document text")
        cursor = uno_call(text.createTextCursor, "Create text cursor")
        uno_call(lambda: cursor.gotoStart(False), "Cursor gotoStart")
        lo, hi = 0, doc_len
        while lo <= hi:
            mid = (lo + hi) // 2
            probe = uno_call(
                lambda: text.createTextCursorByRange(uno_call(cursor.getStart, "Cursor getStart")),
                "Create probe cursor",
            )
            uno_call(lambda: probe.goRight(mid, False), "Cursor goRight")
            cmp = uno_call(
                lambda: text.compareRegionStarts(target_start, uno_call(probe.getStart, "Cursor getStart")),
                "compareRegionStarts",
            )
            if cmp == 0:
                return mid
            if cmp < 0:
                lo = mid + 1
            else:
                hi = mid - 1
        return min(lo, doc_len)
    except UnoObjectError:
        log.exception("_char_offset_of_position failed")
        return -1


def selected_text_range(controller: Any) -> Any:
    """Find the non-empty text selection range from the current view.

    Skips shape selections and collapsed ranges when a non-collapsed range is present,
    falling back to the first text range (caret) or None.
    """
    if controller is None:
        return None
    try:
        sel = controller.getSelection()
    except Exception as exc:
        reraise_if_disposed(exc)
        raise
    if not sel or not hasattr(sel, "getCount"):
        return None
    try:
        count_val = sel.getCount()
        count = int(count_val)
    except Exception as exc:
        reraise_if_disposed(exc)
        raise
    if count <= 0:
        return None

    first_text_range = None
    for i in range(count):
        try:
            rng = sel.getByIndex(i)
        except Exception as exc:
            reraise_if_disposed(exc)
            continue
        if rng is None:
            continue
        if hasattr(rng, "supportsService"):
            svc_val = uno_read(lambda: rng.supportsService("com.sun.star.drawing.Shape"), FAILED)
            if svc_val is True or (type(svc_val) is int and svc_val == 1):
                continue

        if first_text_range is None:
            first_text_range = rng

        if not hasattr(rng, "getStart") or not hasattr(rng, "getEnd"):
            continue

        start = uno_read(rng.getStart, None)
        end = uno_read(rng.getEnd, None)
        rng_text = uno_read(rng.getText, None) if hasattr(rng, "getText") else None

        cmp_val = None
        if rng_text is not None and hasattr(rng_text, "compareRegionStarts") and start is not None and end is not None:
            target_text = rng_text
            assert target_text is not None
            cmp_val = uno_read(lambda: target_text.compareRegionStarts(start, end), None)

        if type(cmp_val) is int:
            if cmp_val != 0:
                return rng
        else:
            s = uno_read(rng.getString, "") if hasattr(rng, "getString") else ""
            if s:
                return rng

    return first_text_range


def starts_at_or_after(text: Any, left: Any, right: Any) -> bool:
    """True when *left* starts at or after *right* in *text* (compareRegionStarts != 1).

    compareRegionStarts returns 1 when the first range starts before the second, 0 when they
    start at the same point, and -1 when after. When comparison fails, returns False.
    A disposed document raises DocumentDisposedError through uno_read.
    """
    if text is None:
        return False
    cmp_val = uno_read(
        lambda: text.compareRegionStarts(left, right),
        FAILED,
    )
    if cmp_val is FAILED or cmp_val is None:
        return False
    return cmp_val != 1


def _get_writer_selection_positions(model: Any) -> tuple[Any, Any, Any] | None:
    """Return (text, sel_start_pos, sel_end_pos) or None when selection unavailable."""
    try:
        if model is None:
            raise UnoObjectError("Document Model is null", code="UNO_NULL_OBJECT")
        controller = uno_call(model.getCurrentController, "Get current controller")
        rng = selected_text_range(controller)
        if not rng:
            rng = uno_call(controller.getViewCursor, "Get view cursor") if controller else None
        if not rng or not hasattr(rng, "getStart") or not hasattr(rng, "getEnd"):
            return None
        text = uno_call(rng.getText, "Get range text")
        return text, uno_call(rng.getStart, "Get range start"), uno_call(rng.getEnd, "Get range end")
    except UnoObjectError:
        return None


@main_thread_only
def get_selection_range(model: Any, doc_len: int | None = None) -> tuple[int, int]:
    """Return (start_offset, end_offset) character positions into the document.
    Cursor (no selection) = same start and end. Returns (0, 0) when there is no
    body-text range: no selection, an error other than a closed document, or the
    selection is in a table cell, frame, or header. A closed document raises
    DocumentDisposedError."""
    try:
        if model is None:
            raise UnoObjectError("Document Model is null", code="UNO_NULL_OBJECT")
        sel_positions = _get_writer_selection_positions(model)
        if sel_positions is None:
            return (0, 0)
        _text, sel_start_pos, sel_end_pos = sel_positions
        if doc_len is None:
            doc_len = _writer_char_count(model)
        start_offset = _char_offset_of_position(model, sel_start_pos, doc_len)
        equal = sel_start_pos == sel_end_pos or uno_same(sel_start_pos, sel_end_pos)
        if not equal and hasattr(_text, "compareRegionStarts"):
            equal = uno_call(
                lambda: _text.compareRegionStarts(sel_start_pos, sel_end_pos),
                "compareRegionStarts",
            ) == 0
        if equal:
            end_offset = start_offset
        else:
            end_offset = _char_offset_of_position(model, sel_end_pos, doc_len)
        if start_offset < 0 or end_offset < 0:
            # Not in the body offset space. (0, 0) is "no body selection",
            # not a cursor at the start. Chat overlap returns False first,
            # so selection markers are not injected.
            return (0, 0)
        return (start_offset, end_offset)
    except UnoObjectError:
        log.exception("get_selection_range failed")
        return (0, 0)


class _HeadingTreeRequired(TypedDict):
    level: int
    text: str
    para_index: int
    children: list["HeadingTreeNode"]
    body_paragraphs: int


class HeadingTreeNode(_HeadingTreeRequired, total=False):
    """Shape of nodes returned by :func:`build_heading_tree` (recursive heading tree).

    Optional ``chapter_number`` (Tools → Chapter Numbering paint label) is set
    only when present — omit the key when numbering is off. See
    :func:`chapter_number_from_para`.
    """

    chapter_number: str


def chapter_number_from_para(para: Any) -> str | None:
    """Read-only chapter / outline label, or ``None`` when numbering is off.

    Authoritative paint label is paragraph ``ListLabelString`` (discussion
    #876 / LO 25.2.3). ``NumberingStyleName`` can be ``\"Outline\"`` even
    when Chapter Numbering is off — do not use it as an on/off signal.
    ``OutlineLevel`` / ``getString()`` never include the label.

    Normalize on emit: strip a trailing ``.`` so Suffix=``\"\"`` and
    Suffix=``\".\"`` both become locator ``chapter_number:3.1``.
    """
    try:
        raw = str(para.getPropertyValue("ListLabelString") or "").rstrip(".")
    except Exception:
        return None
    return raw or None


def apply_chapter_number(node: Any, para: Any) -> None:
    """Set optional ``chapter_number`` on a heading node; omit the key when off."""
    label = chapter_number_from_para(para)
    if label:
        node["chapter_number"] = label


def iter_heading_nodes(node: Mapping[str, Any]) -> Iterator[Mapping[str, Any]]:
    """Yield heading children in document order (not the synthetic root)."""
    for child in node.get("children", []):
        yield child
        yield from iter_heading_nodes(child)


def find_heading_by_chapter_number(tree: Mapping[str, Any], label: str) -> Mapping[str, Any] | None:
    """Exact match on the emitted ``chapter_number`` field (after normalize).

    Does **not** invent a label from sibling ordinals. ``heading:1.2`` remains
    the ordinal path; this looks up the paint label only.
    """
    want = str(label or "").rstrip(".")
    if not want:
        return None
    for child in iter_heading_nodes(tree):
        if child.get("chapter_number") == want:
            return child
    return None


def _portion_type(portion: Any) -> str | None:
    # PyUNO attribute access is the same property call; a second read cannot succeed.
    return uno_read(lambda: portion.getPropertyValue("TextPortionType"), None, object_type="TextPortion")


def _range_is_paragraph(text_range: Any) -> bool:
    """True when *text_range* is a paragraph: children are portions, not paragraphs.

    ``createEnumeration()`` on a document or multi-para cursor yields paragraphs.
    On a paragraph ``XTextRange`` it yields text portions (bold runs, redlines).
    Redlines must not be split as paragraph boundaries—extra newlines would appear between runs.
    """
    if uno_read(lambda: text_range.supportsService(_PARAGRAPH_SERVICE) is True, False, object_type="TextRange"):
        return True

    def first_is_portion() -> bool:
        enum = text_range.createEnumeration()
        for elem in iter_elements(enum):
            return _portion_type(elem) is not None
        return False

    return uno_read(first_is_portion, False, object_type="TextRange")


def _visible_portions(
    para: Any,
    *,
    abort_on_portion_error: bool = False,
    limit: int | None = None,
    truncated_out: list[int] | None = None,
) -> Iterator[tuple[Any, str]]:
    """Yield ``(portion, text)`` for visible text, skipping tracked deletions.

    Shared by ``get_string_without_tracked_deletions`` and range_copy paint so
    an offset taken from the helper string indexes these chunks without drift.

    Paint (``range_copy._paint_direct_formatting``) passes
    ``abort_on_portion_error=True``: a failed ``nextElement`` / portion type
    would desync character offsets, so the walk stops. The helper continues
    past a bad portion so later runs can still contribute text.

    When *limit* stops the walk with more portions waiting, *truncated_out*
    (if given) receives the seen count so the caller can warn.
    """
    try:
        portion_enum = para.createEnumeration()
    except Exception:
        return
    in_delete = False
    seen = 0
    while enum_has_more(portion_enum):
        if limit is not None and seen >= limit:
            # Cap hit with more portions waiting — caller can warn that paint is partial.
            if truncated_out is not None:
                truncated_out.append(seen)
            return
        seen += 1
        try:
            portion = portion_enum.nextElement()
        except Exception:
            if abort_on_portion_error:
                return
            continue
        portion_type = _portion_type(portion)
        if portion_type is None:
            if abort_on_portion_error:
                return
            continue
        if portion_type == "Redline":
            try:
                if str(portion.getPropertyValue("RedlineType")) == "Delete":
                    in_delete = not in_delete
            except Exception as exc:
                log.debug("visible portion RedlineType unreadable (%s)", type(exc).__name__)
            continue
        if in_delete:
            continue
        try:
            chunk = portion.getString()
        except Exception as exc:
            log.debug("visible portion getString failed (%s)", type(exc).__name__)
            continue
        # A Frame portion (as-character formula/object) has no text but html export
        # still needs it in place; it adds zero to offsets.
        if chunk or portion_type == "Frame":
            yield portion, chunk


def _paragraph_visible_text(para: Any) -> str:
    """Visible text of one paragraph via ``_visible_portions``.

    Falls back to ``getString()`` only when the portion enum cannot be opened
    (same as the old helper). An empty walk after a successful enum is kept
    empty so tracked-only paragraphs do not re-include deleted text.
    """
    chunks = list(_visible_portions(para))
    if chunks:
        return "".join(chunk for _unused, chunk in chunks)
    try:
        para.createEnumeration()
    except Exception:
        try:
            return para.getString()
        except Exception:
            return ""
    return ""


def _table_visible_text(table: Any) -> str:
    """Visible text of *table* cells in document order, cells joined like paragraphs."""
    names: Any = uno_read(lambda: table.getCellNames() or (), (), object_type="TextTable")
    cells: list[str] = []
    for name in names:
        cell = uno_read(lambda: table.getCellByName(name), None, object_type="TextTable")
        if cell is not None:
            cells.append(get_string_without_tracked_deletions(cell))
    return "\n".join(cells)


@main_thread_only
def get_string_without_tracked_deletions(text_range: Any) -> str:
    """Return *text_range* text while skipping tracked deletions when possible.

    A paragraph (``com.sun.star.text.Paragraph``, or first child has
    ``TextPortionType``) concatenates visible portions without a mid-``\\n``.
    A document or multi-paragraph range still joins paragraphs with ``\\n``
    and includes table-cell text, including nested tables.
    """
    try:
        if _range_is_paragraph(text_range):
            return _paragraph_visible_text(text_range)
        para_enum = text_range.createEnumeration()
    except Exception:
        return text_range.getString()

    parts: list[str] = []
    try:
        first_para = True
        for para in iter_elements(para_enum):
            if not first_para:
                parts.append("\n")
            first_para = False
            if uno_read(lambda: para.supportsService("com.sun.star.text.TextTable") is True, False, object_type="TextRange"):
                parts.append(_table_visible_text(para))
                continue
            # Each paragraph's portion enum is independent; Delete start/end
            # markers for this walk live in that para. Reset is inside
            # _visible_portions (same as UNO per-paragraph redline markers).
            parts.append(_paragraph_visible_text(para))
    except Exception as exc:
        # Return deletion-free prefix on mid-walk error; do not fall back to getString().
        reraise_if_disposed(exc, object_type="TextRange")
        log.debug(
            "get_string_without_tracked_deletions: portion walk stopped early", exc_info=True)
        return "".join(parts)

    return "".join(parts)


@main_thread_only
def get_document_path(model: Any) -> str | None:
    """Return the local filesystem path for the document, or None if not a file URL (e.g. untitled)."""
    try:
        url = model.getURL()
        if not url:
            return None
        # Legacy urljoin produced file:/path; require file:// after that repair.
        url = _url_utils.normalize_file_url(str(url))
        if not url.startswith("file://"):
            return None
        return str(uno.fileUrlToSystemPath(url))
    except Exception as e:
        log.debug("get_document_path exception: %s", type(e).__name__)
        return None


@main_thread_only
def build_heading_tree(model: Any, elements: list[Any] | None = None) -> HeadingTreeNode:
    """Build a hierarchical heading tree. Single pass enumeration."""
    try:
        if model is None:
            raise UnoObjectError("Document Model is null", code="UNO_NULL_OBJECT")
        root: HeadingTreeNode = {"level": 0, "text": "root", "para_index": -1, "children": [], "body_paragraphs": 0}
        stack: list[HeadingTreeNode] = [root]

        def _iter_elements():
            if elements is not None:
                yield from elements
            else:
                text = uno_call(model.getText, "Get document text")
                enum = uno_call(text.createEnumeration, "Create enumeration")
                while uno_call(enum.hasMoreElements, "Check more elements"):
                    yield uno_call(enum.nextElement, "Get next element")

        for para_index, element in enumerate(_iter_elements()):
            if uno_call(lambda: element.supportsService("com.sun.star.text.Paragraph"), "Check supportsService Paragraph"):
                outline_level = 0
                try:
                    outline_level = uno_call(lambda: element.getPropertyValue("OutlineLevel"), "Get OutlineLevel")
                except UnoObjectError as e:
                    log.debug("build_heading_tree could not get OutlineLevel: %s", e)

                if isinstance(outline_level, int) and outline_level > 0:
                    while len(stack) > 1 and int(stack[-1]["level"]) >= outline_level:
                        stack.pop()
                    node: HeadingTreeNode = {
                        "level": outline_level,
                        "text": get_string_without_tracked_deletions(element),
                        "para_index": para_index,
                        "children": [],
                        "body_paragraphs": 0,
                    }
                    # OutlineLevel > 0 only — list-item ListLabelString is not a chapter label.
                    # Paint label only; the key is omitted when ListLabelString is empty.
                    apply_chapter_number(node, element)
                    stack[-1]["children"].append(node)
                    stack.append(node)
                else:
                    stack[-1]["body_paragraphs"] += 1
            elif uno_call(lambda: element.supportsService("com.sun.star.text.TextTable"), "Check supportsService TextTable"):
                stack[-1]["body_paragraphs"] += 1
        return root
    except UnoObjectError:
        # UnoObjectError must propagate—callers must not read a failed walk as an empty outline.
        log.exception("build_heading_tree error")
        raise


@main_thread_only
def get_selection_text(model: Any) -> str | None:
    """Return the selected text or None if selection is empty/unavailable/fails. Handles Writer, Calc, Draw."""
    try:
        if model is None:
            raise UnoObjectError("Document Model is null", code="UNO_NULL_OBJECT")
        controller = uno_call(model.getCurrentController, "Get current controller")
        if not controller:
            return None

        doc_type = _doc_type.get_document_type(model)

        if doc_type == _doc_type.DocumentType.WRITER:
            rng = selected_text_range(controller)
            if not rng:
                vc = uno_call(controller.getViewCursor, "Get view cursor")
                if vc:
                    return uno_call(vc.getString, "Get view cursor string")
            else:
                return uno_call(rng.getString, "Get selection string")
        elif doc_type == _doc_type.DocumentType.CALC:
            selection = uno_call(controller.getSelection, "Get selection")
            if selection and hasattr(selection, "getString"):
                return uno_call(selection.getString, "Get selection string")
        elif doc_type in (_doc_type.DocumentType.DRAW, _doc_type.DocumentType.IMPRESS):
            selection = uno_call(controller.getSelection, "Get selection")
            if selection and hasattr(selection, "getCount"):
                count = uno_call(selection.getCount, "Get selection count")
                parts = []
                for i in range(count):
                    shape = uno_call(lambda: selection.getByIndex(i), "Get selection shape")
                    if shape and hasattr(shape, "getString"):
                        parts.append(uno_call(shape.getString, "Get shape string"))
                if parts:
                    return "\n".join(parts)
    except DocumentDisposedError:
        # uno_call maps disposal to DocumentDisposedError; do not swallow it.
        raise
    except Exception as exc:
        log.debug("get_selection_text failed (%s)", type(exc).__name__)
    return None


def _read_writer_text_slice(model: Any, start_offset: int, length: int) -> str:  # pyright: ignore[reportUnusedFunction]
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


@main_thread_only
def get_document_length(model: Any) -> int:
    """Return total character length of the document. Returns 0 on error.

    Writer: the length of the visible text (pending tracked deletions hidden, paragraph breaks
    counted, fields and footnote numbers as shown), read with the same helper
    get_document_content scope='range' counts its offsets with. It was the ``CharacterCount``
    statistic, which leaves out the breaks and the deleted text, so a range read near the end
    was cut short ("o dan"). Cursor steps minus the deleted text is no substitute: a field or
    footnote anchor is one step but several characters (314 against 324 on 12 footnotes).
    Returns 0 for a non-Writer document or on error.
    """
    try:
        if model is None:
            raise UnoObjectError("Document Model is null", code="UNO_NULL_OBJECT")
        if _doc_type.get_document_type(model) == _doc_type.DocumentType.WRITER:
            body = uno_call(model.getText, "Get document text")
            whole = uno_call(body.createTextCursor, "Create text cursor")
            uno_call(lambda: whole.gotoStart(False), "Cursor gotoStart")
            uno_call(lambda: whole.gotoEnd(True), "Cursor gotoEnd")
            return len(normalize_linebreaks(get_string_without_tracked_deletions(whole)))
        return 0
    except UnoObjectError:
        log.exception("get_document_length failed")
        return 0


def clone_text_range(text_range: Any) -> Any:
    """Clone *text_range* via its own XText (nested table/frame safe).

    ``doc.getText().createTextCursorByRange(range)`` raises UNO
    RuntimeException ("End of content node doesn't have the proper start node")
    when the range lives in a table cell or frame. The range's XText is the
    cell, frame, or body that actually owns it.
    """
    return text_range.getText().createTextCursorByRange(text_range)


def with_view_cursor_left_body_locked(doc: Any, vc: Any, action_fn: Any) -> Any:
    """Leave nested XText, lock for the action, unlock before restore.

    Leave nested XText, lock controllers, run the scan, unlock, then restore the view cursor.
    Body getText() cannot clone a cursor inside a table cell or frame.
    """
    saved = None
    try:
        # Nested XText (table cell / frame): body getText() cannot clone this range.
        saved = clone_text_range(vc)
    except Exception as exc:
        # Failed clone: run action without moving the view cursor—no saved position means no restore jump.
        reraise_if_disposed(exc, object_type="ViewCursor")
        log.debug(
            "with_view_cursor_left_body_locked: clone failed; not moving the view cursor",
            exc_info=True,
        )
        return action_fn()
    in_body = False
    try:
        vc.gotoRange(doc.getText().getStart(), False)
        in_body = True
    except Exception as exc:
        log.debug("with_view_cursor_left_body_locked: gotoRange to body start failed (%s)", type(exc).__name__)
    if in_body:
        doc.lockControllers()
    try:
        return action_fn()
    finally:
        if in_body:
            doc.unlockControllers()
        if saved is not None:
            try:
                vc.gotoRange(saved, False)
            except Exception as exc:
                log.debug("with_view_cursor_left_body_locked: restore view cursor failed (%s)", type(exc).__name__)


@main_thread_only
def get_text_cursor_at_range(model: Any, start_offset: int, end_offset: int) -> Any:
    """Return a text cursor that selects the character range [start_offset, end_offset).
    The cursor is positioned at start and expanded to end so caller can setString('') and insert.
    goRight is used in chunks because UNO's goRight takes short (max 32767).
    Returns None on error or invalid range."""
    cursor, _ = get_text_cursor_at_range_reached(model, start_offset, end_offset)
    return cursor


@main_thread_only
def get_text_cursor_at_range_reached(model: Any, start_offset: int, end_offset: int) -> tuple[Any, bool]:
    """Return (cursor, fully_reached) selecting character range [start_offset, end_offset).

    fully_reached is false when any goRight returns false (e.g. past document end).
    """
    try:
        if model is None:
            raise UnoObjectError("Document Model is null", code="UNO_NULL_OBJECT")
        # Offsets are not clamped here. Writer callers use visible-text offsets
        # (get_document_length) or cursor-step offsets (_writer_char_count);
        # either way goRight stops at the end of the text on its own.
        start_offset = max(0, start_offset)
        end_offset = max(0, end_offset)
        if start_offset > end_offset:
            start_offset, end_offset = end_offset, start_offset
        text = uno_call(model.getText, "Get document text")
        cursor = uno_call(text.createTextCursor, "Create text cursor")
        uno_call(lambda: cursor.gotoStart(False), "Cursor gotoStart")
        # Move to start_offset in chunks. A goRight that runs out of text stops at the end and
        # returns False: stop there, or an offset like 10**12 runs millions of UNO calls.
        remaining = start_offset
        while remaining > 0:
            n = min(remaining, _GO_RIGHT_CHUNK)
            if uno_call(lambda: cursor.goRight(n, False), "Cursor goRight") is False:
                return cursor, False
            remaining -= n
        # Expand selection by (end_offset - start_offset)
        fully_reached = True
        remaining = end_offset - start_offset
        while remaining > 0:
            n = min(remaining, _GO_RIGHT_CHUNK)
            if uno_call(lambda: cursor.goRight(n, True), "Cursor goRight") is False:
                fully_reached = False
                break
            remaining -= n
        return cursor, fully_reached
    except UnoObjectError:
        log.exception("get_text_cursor_at_range failed")
        return None, False


