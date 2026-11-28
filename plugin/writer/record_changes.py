# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""Helper for managing LibreOffice Writer RecordChanges (track changes)."""

from __future__ import annotations

import logging
from typing import Any, Iterator

from plugin.doc.compound_undo import WriterCompoundUndo
from plugin.doc.text_helpers import bounds_cover, iter_elements_limited, redline_bounds
from plugin.framework.errors import reraise_if_disposed

log = logging.getLogger("writeragent.writer")

# (RedlineType, (text, start, end)) of one tracked change.
RedlineSpan = tuple[str, tuple[Any, Any, Any]]


def is_recording_changes(model: Any) -> bool:
    """True if *model* is currently recording Track Changes (redlines).

    Agent edits made while recording must land as a clean tracked Delete + Insert so the
    user can accept (-> new text) or reject (-> old text) each one. The paragraph-style
    restore on inline HTML replaces would turn that deletion into a FORMAT redline that
    keeps the old text on Accept, so it is skipped while recording; ``replace_preserving_format``
    uses its recorded path instead of untracked run-by-run replacement.
    """
    # A read failure other than disposal means the model has no RecordChanges
    # (not a text document), which counts as not recording.
    try:
        return bool(model.getPropertyValue("RecordChanges"))
    except Exception as exc:
        reraise_if_disposed(exc)
        return False


class record_changes:  # Named like a function: callers write ``with record_changes(doc, on):``.
    """Temporarily set the document's RecordChanges state, restoring it on exit.

    ``__enter__`` returns the prior recording state (was_recording). A class, not a
    generator context manager, so a UNO exception from the block reaches the caller
    unchanged (see ``plugin.doc.compound_undo.UndoStepOrRollback``).
    """

    _doc: Any
    _on: bool
    _was_recording: bool

    def __init__(self, doc: Any, on: bool) -> None:
        self._doc = doc
        self._on = on
        self._was_recording = False

    def __enter__(self) -> bool:
        self._was_recording = is_recording_changes(self._doc)
        if self._was_recording != self._on:
            try:
                self._doc.setPropertyValue("RecordChanges", self._on)
            except Exception as exc:
                reraise_if_disposed(exc)
                log.warning("record_changes: could not set RecordChanges=%s", self._on)
        return self._was_recording

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: Any) -> None:
        if is_recording_changes(self._doc) != self._was_recording:
            try:
                self._doc.setPropertyValue("RecordChanges", self._was_recording)
            except Exception as exc_restore:
                reraise_if_disposed(exc_restore)
                log.warning("record_changes: failed to restore RecordChanges=%s", self._was_recording)


def insert_recorded_content(model: Any, host: Any, cursor: Any, content: Any) -> bool:
    """Insert *content* into *host* at *cursor*; True when tracking is on, so Writer recorded the insertion
    (callers run it inside the review session, which turns tracking on in Record/Wait)."""
    # An embedded object gets no redline of its own, and an Input field gets one that covers
    # only its first character, so Reject would delete the wrong text. Text inside an Insert
    # redline belongs to it, so deleting our own insertion markers removes them outright.
    # Do not position from content.getAnchor(): its end maps before the object, so deleting
    # the tail marker would delete the object.
    if not is_recording_changes(model):
        host.insertTextContent(cursor, content, False)
        return False
    with WriterCompoundUndo(model, "WriterAgent: Insert object"):
        span = host.createTextCursorByRange(cursor.getStart())
        host.insertString(span, "xx", True)  # two recorded characters, one on each side
        try:
            middle = host.createTextCursorByRange(span.getStart())
            middle.goRight(1, False)
            host.insertTextContent(middle, content, False)
        finally:
            tail = host.createTextCursorByRange(span.getEnd())
            tail.goLeft(1, True)
            tail.setString("")
            head = host.createTextCursorByRange(span.getStart())
            head.goRight(1, True)
            head.setString("")
    return True


def _iter_redlines(doc: Any) -> Iterator[Any]:
    redlines = doc.getRedlines()
    return iter_elements_limited(redlines.createEnumeration(), int(redlines.getCount()))


def redline_spans(doc: Any) -> list[RedlineSpan]:
    """``(RedlineType, bounds)`` of every tracked change in *doc*; unreadable ones are left out.

    Stale once the document is edited: build a new list after a write.
    """
    spans: list[RedlineSpan] = []
    for rl in _iter_redlines(doc):
        try:
            spans.append((str(rl.getPropertyValue("RedlineType")), redline_bounds(rl)))
        except Exception as exc:
            reraise_if_disposed(exc)  # the redline's bounds or type could not be read
    return spans


def redline_kinds_covering(spans: list[RedlineSpan], text_range: Any) -> set[str]:
    """``RedlineType`` of every tracked change in *spans* that covers all of *text_range*."""
    return {kind for kind, bounds in spans if bounds_cover(bounds, text_range)}


def redline_kinds_overlapping(spans: list[RedlineSpan], text_range: Any) -> set[str]:
    """``RedlineType`` of every tracked change in *spans* whose bounds intersect *text_range*.

    Cross-text or incomparable spans count as overlapping so callers fail closed.
    """
    kinds: set[str] = set()
    r_start = text_range.getStart()
    r_end = text_range.getEnd()
    for kind, (text, start, end) in spans:
        try:
            # compareRegionStarts(a, b): 1 when a starts before b.
            if text.compareRegionStarts(start, r_end) > 0 and text.compareRegionStarts(r_start, end) > 0:
                kinds.add(kind)
        except Exception as exc:
            reraise_if_disposed(exc)
            kinds.add(kind)
    return kinds


def overlaps_foreign_insertion(doc: Any, ranges: list[Any], author: str) -> bool:
    """True when a tracked insertion by someone other than *author* overlaps one of *ranges*.

    compareRegionStarts compares document positions when both arguments are text ranges
    (``SwXText::compareRegionStarts``, sw/source/core/unocore/unotext.cxx), so insertions in
    cells and frames work too. ``redline_bounds`` maps an insertion that starts at a table node
    to the table's anchor; a single insertion that still cannot be read or compared is skipped,
    so it cannot make every range in the document look overlapped.
    """
    bounds = [(r.getStart(), r.getEnd()) for r in ranges]
    for rl in _iter_redlines(doc):
        if rl.getPropertyValue("RedlineType") != "Insert" or rl.getPropertyValue("RedlineAuthor") == author:
            continue
        try:
            text, start, end = redline_bounds(rl)
            # compareRegionStarts(a, b): 1 when a starts before b.
            if any(
                text.compareRegionStarts(start, r_end) > 0 and text.compareRegionStarts(r_start, end) > 0
                for r_start, r_end in bounds
            ):
                return True
        except Exception as exc:
            reraise_if_disposed(exc)
            log.debug("overlaps_foreign_insertion: skipped an unreadable insertion", exc_info=True)
    return False


def tracked_change_before_point(doc: Any, point: Any) -> bool:
    """True when a pending Insert or Delete redline would be split by inserting at *point*."""
    prev_end = None
    cur = point.getText().createTextCursorByRange(point.getStart())
    if cur.isStartOfParagraph() and cur.goLeft(1, False):
        prev_end = cur.getStart()

    for rl in _iter_redlines(doc):
        kind = rl.getPropertyValue("RedlineType")
        if kind not in ("Insert", "Delete"):
            continue
        try:
            text, start, end = redline_bounds(rl)
            if text.compareRegionStarts(start, point) != 1:
                continue
            end_cmp = text.compareRegionEnds(end, point)
            # A Delete ending at a mid-paragraph point does not stretch on insert; at a
            # paragraph start, a redline ending at the previous paragraph's end still does.
            if end_cmp == -1 or (kind == "Insert" and end_cmp == 0):
                return True
            if prev_end is not None and text.compareRegionEnds(end, prev_end) <= 0:
                return True
        except Exception as exc:
            reraise_if_disposed(exc)  # another text object, or not comparable
    return False


def tracked_change_spans_past(doc: Any, text_range: Any) -> bool:
    """True when a pending Insert or Delete redline starts before *text_range* and ends after it.

    ``SwDoc::InsertSwSection`` calls ``DocumentRedlineManager::SplitRedline`` over the new
    section. A redline that starts before and ends after the span becomes two pieces, and
    accepting or rejecting it then loses text outside the section; a redline that starts or
    ends at or inside the span resolves to the same text. ``tracked_change_before_point``
    checks a single collapsed insertion point and would refuse safe spans.
    """
    for rl in _iter_redlines(doc):
        kind = rl.getPropertyValue("RedlineType")
        if kind not in ("Insert", "Delete"):
            continue
        try:
            text, start, end = redline_bounds(rl)
            if (
                text.compareRegionStarts(start, text_range) == 1
                and text.compareRegionEnds(text_range, end) == 1
            ):
                return True
        except Exception as exc:
            reraise_if_disposed(exc)  # another text object, or not comparable
    return False
