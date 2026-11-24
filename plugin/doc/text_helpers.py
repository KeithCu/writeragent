# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
# Copyright (c) 2026 LibreCalc AI Assistant (Calc integration features, originally MIT)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Writer text / path / selection helpers used by LibrePy without ``document_helpers``.

LibrePy Run Python Script, text analytics, Excel auto-open, and Writer visible
body reads need linebreak normalization, tracked-deletion reads, heading trees,
file paths, and selection positions. Those must not load ``document_helpers``
→ chat context / ``DocumentService``.

Offsets: visible-text offsets come from ``get_string_without_tracked_deletions``,
``get_document_length``, and ``read_visible_body``. Cursor-step offsets are used
only by ``get_text_cursor_at_range_reached``. They are not additive across tables
(``SwUnoCursor::IsSelOvr``, ``sw/source/core/unocore/unocrsr.cxx``), so never
convert one to the other by counting.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypedDict

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator, Mapping, Sequence

import uno

from plugin.framework import doc_type as _doc_type
from plugin.framework.errors import (
    DocumentDisposedError,
    UnoObjectError,
    best_effort,
    is_disposal,
    reraise_if_disposed,
    uno_call,
    uno_read,
)
from plugin.framework.thread_guard import main_thread_only
from plugin.framework.uno_context import enum_has_more, uno_same
from plugin.framework import url_utils as _url_utils

log = logging.getLogger(__name__)

_PARAGRAPH_SERVICE = "com.sun.star.text.Paragraph"
_DRAW_SHAPE_SERVICE = "com.sun.star.drawing.Shape"
_TEXT_TABLE_SERVICE = "com.sun.star.text.TextTable"
# uno_read default where None is a real UNO result.
FAILED: Any = object()


def _is_text_table(obj: Any) -> bool:
    return uno_read(lambda: obj.supportsService(_TEXT_TABLE_SERVICE) is True, False, object_type="TextRange")


def redline_bounds(redline: Any) -> tuple[Any, Any, Any]:
    """Return ``(text, start, end)`` of a tracked change; a start at a table is the table's anchor."""
    start = redline.getPropertyValue("RedlineStart")
    end = redline.getPropertyValue("RedlineEnd")
    # SwXRedline::getPropertyValue (sw/source/core/unocore/unoredline.cxx, SwNodeType::Table case)
    # returns SwXTextTable for RedlineStart when a deletion starts at a table, which has no getText().
    if _is_text_table(start):
        start = start.getAnchor()
    return end.getText(), start, end


def bounds_cover(bounds: tuple[Any, Any, Any], text_range: Any) -> bool:
    """True when the tracked change spans all of *text_range*, edges included.

    False when the two cannot be compared (another text object). Disposal raises
    ``DocumentDisposedError``.
    """
    text, start, end = bounds
    # compareRegionStarts(a, b): 1 when a starts before b, 0 when equal
    # (SwXText::ComparePositions, sw/source/core/unocore/unotext.cxx).
    return uno_read(
        lambda: text.compareRegionStarts(start, text_range) >= 0
        and text.compareRegionEnds(text_range, end) >= 0,
        False,
        object_type="TextRange",
    )


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


def selected_text_range(controller: Any) -> Any:
    """Find the non-empty text selection range from the current view.

    Skips shape selections and collapsed ranges when a non-collapsed range is present,
    falling back to the first text range (caret) or None. Do not read ``getByIndex(0)``
    directly because index 0 may be a Draw shape selection or a collapsed range.
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
        if _is_draw_shape_text(rng):
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


def live_selection_range(controller: Any) -> Any | None:
    """Text range the view is on: selected_text_range(controller), else the view cursor, else None.

    Errors other than disposal yield None (uno_read); disposal raises DocumentDisposedError.
    """
    if controller is None:
        return None
    rng = uno_read(lambda: selected_text_range(controller), None)
    if rng is None:
        rng = uno_read(lambda: controller.getViewCursor(), None)
    return rng


def selection_cursor_snapshot(doc: Any) -> Any | None:
    """Return a model cursor cloned from the live selection/caret, or None.

    Uses a model cursor (``SwXTextCursor``) rather than the live view cursor or a
    mark-backed ``SwXTextRange``: the view cursor moves when the user clicks
    elsewhere, and a mark-backed range is deleted when surrounding text is
    removed (``sw/source/core/doc/docbm.cxx`` ``isDeleteMark``) and stays before
    text inserted at its collapsed position (``sw/source/core/txtnode/ndtxt.cxx``
    ``SwTextNode::Update``). A model cursor keeps its place across caret moves,
    follows text inserted before it, collapses rather than throwing on
    surrounding deletion, and sits after text inserted at it. Frame/object
    selections raise inside ``getText()`` and yield None here.
    """
    if doc is None:
        return None
    controller = uno_read(lambda: doc.getCurrentController(), None)
    rng = live_selection_range(controller)
    return uno_read(lambda: clone_text_range(rng), None) if rng is not None else None


_TEXT_RANGES_SERVICE = "com.sun.star.text.TextRanges"


def _restorable_selection(selection: Any) -> Any:
    """Return what to hand back to controller.select so the selection comes back unchanged.

    Writer's ``SwXTextRanges`` container restores its own ``SwUnoCursor``
    (``sw/source/core/unocore/unocrsrhelper.cxx`` ``GetSelectableFromAny``), which
    stretches over text inserted at its end. Each item inside it is a mark-backed
    ``SwXTextRange``, and bookmarks never grow over text inserted at their end
    (``sw/source/core/txtnode/ndtxt.cxx`` ``SwTextNode::Update``). One non-collapsed
    text range is restored from that range; a caret, several ranges, or an object
    (frame, image, shape) is restored from the selection object itself so a caret
    still moves with text inserted at it.
    """
    if not selection.supportsService(_TEXT_RANGES_SERVICE) or selection.getCount() != 1:
        return selection
    rng = selection.getByIndex(0)
    if rng.getText().compareRegionStarts(rng.getStart(), rng.getEnd()) == 0:
        return selection
    return rng


class KeepViewSelection:
    """Put the view's selection back when the ``with`` body ends, whether or not it raised.

    Implemented as a class rather than ``@contextmanager`` because pyuno turns an
    exception re-raised from a generator context manager into a ``RuntimeException``
    (see ``plugin.doc.compound_undo.UndoStepOrRollback``). A ``None`` controller
    is a no-op.
    """

    def __init__(self, controller: Any) -> None:
        self._controller: Any = controller
        self._saved: Any = None

    def __enter__(self) -> KeepViewSelection:
        controller = self._controller
        if controller is not None:
            selection = uno_read(lambda: controller.getSelection(), None)
            if selection is not None:
                self._saved = uno_read(lambda: _restorable_selection(selection), selection)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: Any,
    ) -> None:
        if self._saved is None:
            return
        try:
            self._controller.select(self._saved)
        except Exception as restore_exc:
            if is_disposal(restore_exc):
                raise
            log.exception("could not restore the view selection")


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


def get_writer_selection_positions(model: Any, selection: Any | None = None) -> tuple[Any, Any, Any] | None:
    """Return (text, sel_start_pos, sel_end_pos) or None when selection unavailable."""
    try:
        if model is None:
            raise UnoObjectError("Document Model is null", code="UNO_NULL_OBJECT")
        if selection is not None:
            rng = selection
        else:
            controller = uno_call(model.getCurrentController, "Get current controller")
            rng = live_selection_range(controller)
        if not rng or not hasattr(rng, "getStart") or not hasattr(rng, "getEnd"):
            return None
        text = uno_call(rng.getText, "Get range text")
        return text, uno_call(rng.getStart, "Get range start"), uno_call(rng.getEnd, "Get range end")
    except UnoObjectError:
        return None


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


def clear_direct_numbering(cursor: Any) -> None:
    """Drop direct paragraph numbering so the cursor reverts to the style's list rules.

    In LibreOffice Writer, ``setPropertyToDefault("NumberingStyleName")`` and
    ``setPropertyToDefault("NumberingRules")`` leave direct numbering untouched
    (see ``test_full_document_drops_old_first_paragraph_direct_margins_uno``).
    Setting ``NumberingRules=None`` invokes Writer's ``DelNumRules`` path, which
    clears direct numbering without leaving a hard empty ``NumberingStyleName=""``
    override that would mask an underlying paragraph style's list formatting.
    This also drops leftover numbering after ``insertDocumentFromURL`` in notebook
    HTML imports before following blocks.
    """
    best_effort(
        lambda: cursor.setPropertyValue("NumberingRules", None),
        "clear direct NumberingRules failed",
    )


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


def _is_draw_shape_text(text_range: Any) -> bool:
    """True when text_range is a draw shape (or its XText) in Writer, Calc, Draw or Impress."""
    svc_val = uno_read(lambda: text_range.supportsService(_DRAW_SHAPE_SERVICE), FAILED, object_type="TextRange")
    return svc_val is True or (type(svc_val) is int and svc_val == 1)


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


@dataclass
class DeletionState:
    """Tracked-deletion state across consecutive paragraphs of one ``XText``.

    Redline marker portions exist only in a redline's start and end paragraphs
    (``lcl_FillRedlineArray`` in ``sw/source/core/unocore/unoportenum.cxx``). Pass one
    instance across every paragraph of a text, in order, so paragraphs between the
    markers are skipped too. A table reached while ``in_delete`` is true is struck whole:
    its cells carry no markers. ``saw_delete`` records that a Delete marker was read, for
    callers that must tell struck text from text they cannot read.
    """

    in_delete: bool = False
    saw_delete: bool = False

    def apply_marker(self, portion: Any) -> None:
        """Update the state from one ``Redline`` portion."""
        try:
            if str(portion.getPropertyValue("RedlineType")) == "Delete":
                self.saw_delete = True
                # A Delete that ends at offset 0 of this paragraph shows only its end marker (IsStart=False), so a toggle would hide the paragraph.
                self.in_delete = bool(portion.getPropertyValue("IsStart"))
        except Exception as exc:
            log.debug("visible portion RedlineType unreadable (%s)", type(exc).__name__)


def visible_portions(
    para: Any,
    *,
    abort_on_portion_error: bool = False,
    limit: int | None = None,
    truncated_out: list[int] | None = None,
    state: DeletionState | None = None,
) -> Iterator[tuple[Any, str]]:
    """Yield ``(portion, text)`` for visible text, skipping tracked deletions.

    Shared by ``get_string_without_tracked_deletions`` and range_copy paint so
    an offset taken from the helper string indexes these chunks without drift.

    Paint (``range_copy.paint_direct_formatting``) passes
    ``abort_on_portion_error=True``: a failed ``nextElement`` / portion type
    would desync character offsets, so the walk stops. The helper continues
    past a bad portion so later runs can still contribute text.

    When *limit* stops the walk with more portions waiting, *truncated_out*
    (if given) receives the seen count so the caller can warn.

    Pass a shared *state* across consecutive paragraphs of one ``XText`` so
    deletions spanning multiple paragraphs stay hidden.
    """
    if state is None:
        state = DeletionState()
    try:
        portion_enum = para.createEnumeration()
    except Exception:
        state.in_delete = False
        return
    seen = 0
    while enum_has_more(portion_enum):
        if limit is not None and seen >= limit:
            # Cap hit with more portions waiting — caller can warn that paint is partial.
            # The end marker was not read, and an open state would hide the rest of the document.
            state.in_delete = False
            if truncated_out is not None:
                truncated_out.append(seen)
            return
        seen += 1
        try:
            portion = portion_enum.nextElement()
        except Exception:
            if abort_on_portion_error:
                # The end marker was not read, and an open state would hide the rest of the document.
                state.in_delete = False
                return
            continue
        portion_type = _portion_type(portion)
        if portion_type is None:
            if abort_on_portion_error:
                # The end marker was not read, and an open state would hide the rest of the document.
                state.in_delete = False
                return
            continue
        if portion_type == "Redline":
            state.apply_marker(portion)
            continue
        if state.in_delete:
            continue
        try:
            chunk = portion.getString()
        except Exception as exc:
            log.debug("visible portion getString failed (%s)", type(exc).__name__)
            continue
        # A Frame has no text and a field can show none, but HTML export needs
        # both in place; they add zero to offsets.
        if chunk or portion_type in ("Frame", "TextField"):
            yield portion, chunk


def _paragraph_visible_text(para: Any, state: DeletionState | None = None) -> str:
    """Visible text of one paragraph via ``visible_portions``."""
    return "".join(chunk for _unused, chunk in visible_portions(para, state=state))


class TrackedDeletions:
    """The pending Delete redlines of a Writer document, read once.

    Share one instance across the reads of one request. Build a new one after the
    document is edited, stored, or exported, because an export hides deletions while
    it runs and collapses these ranges (``SwXMLExport::exportDoc`` in ``xmlexp.cxx``,
    ``SwRangeRedline::MoveToSection`` in ``docredln.cxx``).
    """

    def __init__(self, doc: Any) -> None:
        self._bounds: list[tuple[Any, Any, Any]] = []
        redlines = uno_read(lambda: doc.getRedlines(), None)
        if redlines is None or uno_read(lambda: redlines.hasElements() is True, False) is not True:
            return
        enum = uno_read(lambda: redlines.createEnumeration(), None)
        for rl in iter_elements(enum):
            if uno_read(lambda: str(rl.getPropertyValue("RedlineType")), None) != "Delete":
                continue
            bounds = uno_read(lambda: redline_bounds(rl), None)
            if bounds is not None and bounds[0] is not None:
                self._bounds.append(bounds)

    def __bool__(self) -> bool:
        return bool(self._bounds)

    def state_at(self, text_range: Any) -> DeletionState:
        """Return ``DeletionState`` at the start of *text_range* (XText, cursor, paragraph, or table)."""
        if not self._bounds:
            return DeletionState()
        if _is_text_table(text_range):
            point = uno_read(lambda: text_range.getAnchor(), None, object_type="TextTable")
        else:
            point = None
            # getStart() skips a leading table; seed from the first enumerated element if it is a table.
            enum = uno_read(lambda: text_range.createEnumeration(), None, object_type="TextRange")
            if enum is not None:
                for first in iter_elements(enum):
                    if _is_text_table(first):
                        point = uno_read(lambda: first.getAnchor(), None, object_type="TextTable")
                    break
            if point is None:
                point = uno_read(lambda: text_range.getStart(), None, object_type="TextRange")
        if point is None:
            return DeletionState()
        for text, start, end in self._bounds:
            # Half-open [start, end): the end marker sits at end and has already been passed.
            inside = uno_read(
                lambda: text.compareRegionStarts(start, point) >= 0
                and text.compareRegionStarts(point, end) == 1,
                False,
                object_type="TextRange",
            )
            if inside:
                return DeletionState(in_delete=True)
        return DeletionState()

    def covers(self, text_range: Any) -> bool:
        """True when *text_range* lies wholly inside one pending Delete redline."""
        return any(bounds_cover(b, text_range) for b in self._bounds)


def iter_table_paragraphs(table: Any) -> Iterator[tuple[Any, DeletionState]]:
    """Yield ``(para, state)`` for every paragraph in *table*, including nested tables.

    Each cell starts with a fresh ``DeletionState()``. Consumers must walk each
    paragraph with its yielded *state* before advancing to the next item.
    """
    try:
        names = table.getCellNames() or ()
    except Exception as exc:
        reraise_if_disposed(exc)
        log.debug("iter_table_paragraphs: getCellNames failed: %s", exc)
        return
    for name in names:
        try:
            cell = table.getCellByName(name)
        except Exception as exc:
            reraise_if_disposed(exc)
            log.debug("iter_table_paragraphs: getCellByName failed: %s", exc)
            continue
        if cell is not None:
            yield from iter_xtext_paragraphs(cell, DeletionState())


def iter_xtext_paragraphs(text_obj: Any, state: DeletionState) -> Iterator[tuple[Any, DeletionState]]:
    """Yield ``(para, state)`` for every paragraph in *text_obj*, including tables.

    Lazy: the consumer must walk each paragraph with the yielded *state* before
    advancing to the next item so ``state.in_delete`` reflects that paragraph's
    markers. A table reached while ``state.in_delete`` is true is skipped;
    otherwise each cell gets a fresh ``DeletionState()``.
    """
    try:
        enum = text_obj.createEnumeration()
    except Exception as exc:
        reraise_if_disposed(exc)
        log.debug("iter_xtext_paragraphs: createEnumeration failed: %s", exc)
        return
    for el in iter_elements(enum):
        if _is_text_table(el):
            if not state.in_delete:
                yield from iter_table_paragraphs(el)
            continue
        yield el, state


def _table_visible_text(table: Any) -> str:
    """Visible text of *table* cells in document order, cells joined like paragraphs."""
    names: Any = uno_read(lambda: table.getCellNames() or (), (), object_type="TextTable")
    cells: list[str] = []
    for name in names:
        cell = uno_read(lambda: table.getCellByName(name), None, object_type="TextTable")
        if cell is not None:
            cells.append(get_string_without_tracked_deletions(cell))
    return "\n".join(cells)


def _visible_blocks(para_enum: Any, state: DeletionState) -> Iterator[tuple[Any, str, bool]]:
    """Yield (paragraph, visible text, opened inside a deletion) per element of a text
    enumeration; paragraph is None for a table. One shared ``DeletionState`` covers the walk
    because redline markers may be in an earlier paragraph. A mid-walk error ends the walk
    with what was read (never falling back to ``getString()``, which would return deleted
    text); disposal raises DocumentDisposedError."""
    try:
        for element in iter_elements(para_enum):
            opened = state.in_delete
            if _is_text_table(element):
                yield None, "" if opened else _table_visible_text(element), opened
            else:
                yield element, _paragraph_visible_text(element, state), opened
    except Exception as exc:
        reraise_if_disposed(exc, object_type="TextRange")
        log.debug("visible blocks: walk stopped early", exc_info=True)


@main_thread_only
def get_string_without_tracked_deletions(text_range: Any, state: DeletionState | None = None) -> str:
    """Return *text_range* text while skipping tracked deletions when possible.

    A draw shape returns ``getString()`` directly. A paragraph
    (``com.sun.star.text.Paragraph``, or first child has ``TextPortionType``)
    concatenates visible portions without a mid-``\\n``. A document or
    multi-paragraph range still joins paragraphs with ``\\n`` (keeping
    deleted paragraph breaks) and includes table-cell text, including nested
    tables, unless the table falls inside an open tracked deletion.
    """
    try:
        # Draw-shape text is editeng, which never reports a Redline portion
        # (editeng/source/uno/unotext.cxx, WID_PORTIONTYPE case), and getString()
        # joins paragraphs with \n like the walk. Every content/portion enumeration
        # proxy registers in the shape's shared mvTextRanges and unregisters from
        # its destructor without the SolarMutex
        # (svx/source/unodraw/unoshtxt.cxx:SvxTextEditSourceImpl::removeRange),
        # so a remote (URP) client that discards those proxies while the next call
        # adds one corrupts the heap and aborts soffice. Writer shapes with an
        # attached text box (TextBox property true) also take this path and return
        # getString(), reading the frame text without filtering redlines.
        if _is_draw_shape_text(text_range):
            return text_range.getString()
        if _range_is_paragraph(text_range):
            return _paragraph_visible_text(text_range, state)
        para_enum = text_range.createEnumeration()
    except Exception:
        return text_range.getString()

    if state is None:
        state = DeletionState()
    return "\n".join(chunk for _para, chunk, _opened in _visible_blocks(para_enum, state))


@main_thread_only
def visible_range_text(doc: Any, text_range: Any) -> str:
    """Return *text_range* visible text, seeded from *doc*'s pending tracked deletions."""
    return get_string_without_tracked_deletions(text_range, TrackedDeletions(doc).state_at(text_range))


@main_thread_only
def paragraph_deletion_flags(model: Any, elements: list[Any]) -> list[bool]:
    """For each element of a text enumeration: does it start inside a tracked deletion?

    For readers that jump to one paragraph. The list is all False, without a portion
    walk, when the document has no redlines.
    """
    # A portion walk costs 1.0–1.7 s per 1000 paragraphs over the bridge; hasElements() is under 1 ms.
    if not uno_read(lambda: model.getRedlines().hasElements() is True, False):
        return [False] * len(elements)
    state = TrackedDeletions(model).state_at(elements[0]) if elements else DeletionState()
    flags: list[bool] = []
    for element in elements:
        flags.append(state.in_delete)
        if uno_read(lambda: element.supportsService(_PARAGRAPH_SERVICE) is True, False):
            for _unused in visible_portions(element, state=state):
                pass
    return flags


def live_paragraph_text(para: Any, starts_deleted: bool) -> str | None:
    """Visible text of *para*, or None when a tracked deletion strikes all of it.

    Portions cannot read text inside an inline content control or metadata field
    (``lcl_ExportHints`` in ``sw/source/core/unocore/unoportenum.cxx`` moves inner portions into a
    child list), so the raw ``para.getString()`` read is used when no deletion touches the
    paragraph. Blank visible text alone does not mean struck; it is struck when it starts inside a
    deletion, or has raw text and a Delete marker.
    """
    state = DeletionState(starts_deleted)
    text = get_string_without_tracked_deletions(para, state)
    if not (starts_deleted or state.saw_delete):
        # No marker seen, so raw text is the visible text, except for a deletion wholly inside a
        # content control or metadata field: its markers are in the child list.
        return normalize_linebreaks(para.getString())
    if not text.strip() and (starts_deleted or para.getString().strip()):
        return None
    return text


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
    """Build a hierarchical heading tree, skipping tracked-deleted heading text."""
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
                while uno_call(lambda: enum_has_more(enum), "Check more elements"):
                    yield uno_call(enum.nextElement, "Get next element")

        walked = list(_iter_elements())
        flags = paragraph_deletion_flags(model, walked)

        for para_index, element in enumerate(walked):
            if uno_call(lambda: element.supportsService("com.sun.star.text.Paragraph"), "Check supportsService Paragraph"):
                outline_level = 0
                try:
                    outline_level = uno_call(lambda: element.getPropertyValue("OutlineLevel"), "Get OutlineLevel")
                except UnoObjectError as e:
                    log.debug("build_heading_tree could not get OutlineLevel: %s", e)

                if isinstance(outline_level, int) and outline_level > 0:
                    text = live_paragraph_text(element, flags[para_index])
                    if text is None:
                        stack[-1]["body_paragraphs"] += 1
                        continue
                    while len(stack) > 1 and int(stack[-1]["level"]) >= outline_level:
                        stack.pop()
                    node: HeadingTreeNode = {
                        "level": outline_level,
                        "text": text,
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
            elif _is_text_table(element):
                stack[-1]["body_paragraphs"] += 1
        return root
    except UnoObjectError:
        # UnoObjectError must propagate—callers must not read a failed walk as an empty outline.
        log.exception("build_heading_tree error")
        raise


@main_thread_only
def get_selection_text(model: Any) -> str | None:
    """Return the selected text or None if selection is empty/unavailable/fails.

    Handles Writer (tracked deletions hidden; None when the selection is not text), Calc, and Draw.
    """
    try:
        if model is None:
            raise UnoObjectError("Document Model is null", code="UNO_NULL_OBJECT")
        controller = uno_call(model.getCurrentController, "Get current controller")
        if not controller:
            return None

        doc_type = _doc_type.get_document_type(model)

        if doc_type == _doc_type.DocumentType.WRITER:
            # No view-cursor fallback: for cell, frame and shape selections SwXTextViewCursor::getString is empty (sw/source/uibase/uno/unotxvw.cxx).
            rng = selected_text_range(controller)
            if rng is not None:
                return uno_call(lambda: visible_range_text(model, rng), "Get selection string")
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


def _visible_lead(body: Any, para: Any, position: Any, opened: bool) -> int:
    """Measure visible characters from *para* start to *position*.

    Measured per paragraph rather than from the document start because a range from the
    document start to a paragraph after a table drops that paragraph and its break.
    *opened* carries whether the paragraph began inside a deletion whose start marker was
    in an earlier paragraph.
    """
    lead = body.createTextCursorByRange(para.getStart())
    lead.gotoRange(position, True)
    return len(normalize_linebreaks(get_string_without_tracked_deletions(lead, DeletionState(opened))))


@main_thread_only
def read_visible_body(model: Any, positions: Sequence[Any] = ()) -> tuple[str, list[int | None]]:
    """Visible body text (the get_content document_length space) and the visible offset of each
    position in it; None for a position outside the body (table cell, frame, header, footnote)
    or a position that cannot be measured. Walks the body rather than using ``CharacterCount``
    (omits paragraph breaks) or cursor steps (fields and footnote anchors are one step but
    several visible characters). Raises UnoObjectError / DocumentDisposedError."""
    if model is None:
        raise UnoObjectError("Document Model is null", code="UNO_NULL_OBJECT")
    body = uno_call(model.getText, "Get document text")
    whole = uno_call(body.createTextCursor, "Create text cursor")
    uno_call(lambda: whole.gotoStart(False), "Cursor gotoStart")
    uno_call(lambda: whole.gotoEnd(True), "Cursor gotoEnd")
    pending = {i: p for i, p in enumerate(positions) if uno_read(lambda: uno_same(p.getText(), body), False)}
    offsets: list[int | None] = [None] * len(positions)
    parts: list[str] = []
    start = 0
    state = TrackedDeletions(model).state_at(whole)
    for para, chunk, opened in _visible_blocks(uno_call(whole.createEnumeration, "Enumerate body"), state):
        chunk = normalize_linebreaks(chunk)
        if pending and para is not None:
            para_end = uno_read(para.getEnd, None)
            if para_end is not None:
                for i in [i for i, p in pending.items() if starts_at_or_after(body, para_end, p)]:
                    pos = pending.pop(i)
                    lead = uno_read(lambda: _visible_lead(body, para, pos, opened), None)
                    if lead is not None:
                        offsets[i] = start + lead
        parts.append(chunk)
        start += len(chunk) + 1
    text = "\n".join(parts)
    for i in pending:
        # An empty last paragraph after a table is not enumerated
        # (SwXParagraphEnumerationImpl::IgnoreLastElement, sw/source/core/unocore/unoobj2.cxx);
        # a walk that stopped early or a failed compareRegionStarts also lands here.
        offsets[i] = len(text)
    return text, offsets


@main_thread_only
def get_document_length(model: Any) -> int:
    """Return total visible character length of the Writer body, or 0 for non-Writer / on error.

    Pending tracked deletions are hidden, paragraph breaks are counted, and fields /
    footnote numbers are measured as shown (matching ``get_content`` ``document_length``
    and ``read_visible_body``).
    """
    try:
        if model is None:
            raise UnoObjectError("Document Model is null", code="UNO_NULL_OBJECT")
        if _doc_type.get_document_type(model) == _doc_type.DocumentType.WRITER:
            return len(read_visible_body(model)[0])
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


def visible_point_in_paragraph(para: Any, offset: int, *, opened: bool, at_end: bool) -> Any:
    """Collapsed position of visible *offset* in *para* (*opened*: it starts inside a deletion).

    Walks portions rather than stepping from the paragraph start: ``goRight`` also counts struck
    text, and a field or footnote number is one step but several visible characters. An offset
    inside such a portion moves out to its edge. Struck text beside the offset is left out of a
    range that starts here (*at_end* False) or ends here (*at_end* True).
    """
    if at_end and offset == 0:
        return para.getStart()
    run = 0
    for portion, chunk in visible_portions(para, state=DeletionState(opened)):
        run_end = run + len(normalize_linebreaks(chunk))
        if (offset <= run_end) if at_end else (offset < run_end):
            if _portion_type(portion) != "Text":
                return portion.getEnd() if at_end else portion.getStart()
            cursor = clone_text_range(portion.getStart())
            go_right(cursor, offset - run, False)  # one step per code point inside a Text portion
            return cursor
        run = run_end
    return para.getEnd()


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


def jump_view_cursor_to_page(vc: Any, page: int) -> int | None:
    """Move the Writer view cursor to the start of 0-based *page* (first page = 0).

    Returns None when the cursor landed on *page*; otherwise returns the document's page
    count and leaves the cursor on the last page. XTextViewCursor.jumpToPage is 1-based and
    reports success for a missing page: SwRootFrame::SetCurrPage
    (sw/source/core/layout/trvlfrm.cxx) stops on the last page, and jumpToPage(0) lands
    there too, so the landed page is compared instead. jumpToPage takes a sal_Int16, so
    larger values raise CannotConvertException and are not passed.
    """
    if 0 <= page < 0x7FFF:
        vc.jumpToPage(page + 1)
        if int(vc.getPage()) == page + 1:
            return None
    vc.jumpToLastPage()
    return int(vc.getPage())


def page_not_found_message(page: int, total: int) -> str:
    """Format the error message for an out-of-range 0-based page index."""
    return f"Page {page} does not exist (document has {total} page(s); first page = 0)."


@main_thread_only
def get_text_cursor_at_range_reached(model: Any, start_offset: int, end_offset: int) -> tuple[Any, bool]:
    """Return (cursor, fully_reached) selecting cursor-step range [start_offset, end_offset).

    ``goRight`` is called in chunks because UNO's ``goRight`` takes a ``short`` (max 32767).
    ``fully_reached`` is false when any ``goRight`` returns false (e.g. past document end).
    Returns (None, False) on error.
    """
    try:
        if model is None:
            raise UnoObjectError("Document Model is null", code="UNO_NULL_OBJECT")
        # Offsets are cursor steps, not clamped; goRight stops at the end of the text on its own.
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
        log.exception("get_text_cursor_at_range_reached failed")
        return None, False


