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

import contextlib
import logging
from typing import Any, Generator

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
    # RecordChanges get/set does not fail on a live document.
    try:
        return bool(model.getPropertyValue("RecordChanges"))
    except Exception as exc:
        reraise_if_disposed(exc)
        return False


@contextlib.contextmanager
def record_changes(doc: Any, on: bool) -> Generator[bool, None, None]:
    """Temporarily set the document's RecordChanges state, restoring it on exit.

    Yields the prior recording state (was_recording).
    """
    was_recording = is_recording_changes(doc)
    if was_recording != on:
        try:
            doc.setPropertyValue("RecordChanges", on)
        except Exception as exc:
            reraise_if_disposed(exc)
            log.warning("record_changes: could not set RecordChanges=%s", on)
    try:
        yield was_recording
    finally:
        if is_recording_changes(doc) != was_recording:
            try:
                doc.setPropertyValue("RecordChanges", was_recording)
            except Exception as exc:
                reraise_if_disposed(exc)
                log.warning("record_changes: failed to restore RecordChanges=%s", was_recording)


def insert_recorded_content(model: Any, host: Any, cursor: Any, content: Any) -> bool:
    """Insert *content* into *host* at *cursor*; True when tracking is on, so Writer recorded the insertion."""
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


def redline_spans(doc: Any) -> list[RedlineSpan]:
    """``(RedlineType, bounds)`` of every tracked change in *doc*; unreadable ones are left out.

    Stale once the document is edited: build a new list after a write.
    """
    spans: list[RedlineSpan] = []
    redlines = doc.getRedlines()
    for rl in iter_elements_limited(redlines.createEnumeration(), int(redlines.getCount())):
        try:
            spans.append((str(rl.getPropertyValue("RedlineType")), redline_bounds(rl)))
        except Exception as exc:
            reraise_if_disposed(exc)
            continue  # the redline's bounds or type could not be read
    return spans


def redline_kinds_covering(spans: list[RedlineSpan], text_range: Any) -> set[str]:
    """``RedlineType`` of every tracked change in *spans* that covers all of *text_range*."""
    return {kind for kind, bounds in spans if bounds_cover(bounds, text_range)}


def overlaps_foreign_insertion(doc: Any, ranges: list[Any], author: str) -> bool:
    """True when a tracked insertion by someone other than *author* overlaps one of *ranges*.

    compareRegionStarts compares document positions, so insertions in cells and frames work
    too; one that cannot be compared (a table insertion) counts as overlapping.
    """
    bounds = [(r.getStart(), r.getEnd()) for r in ranges]
    redlines = doc.getRedlines()
    for rl in iter_elements_limited(redlines.createEnumeration(), int(redlines.getCount())):
        if rl.getPropertyValue("RedlineType") != "Insert" or rl.getPropertyValue("RedlineAuthor") == author:
            continue
        try:
            start = rl.getPropertyValue("RedlineStart")
            end = rl.getPropertyValue("RedlineEnd")
            text = start.getText()
            # compareRegionStarts(a, b): 1 when a starts before b.
            if any(
                text.compareRegionStarts(start, r_end) > 0 and text.compareRegionStarts(r_start, end) > 0
                for r_start, r_end in bounds
            ):
                return True
        except Exception as exc:
            reraise_if_disposed(exc)
            return True
    return False


def tracked_change_before_point(doc: Any, point: Any) -> bool:
    """True when a pending Insert or Delete redline would be split by inserting at *point*."""
    prev_end = None
    cur = point.getText().createTextCursorByRange(point.getStart())
    if cur.isStartOfParagraph() and cur.goLeft(1, False):
        prev_end = cur.getStart()

    redlines = doc.getRedlines()
    for rl in iter_elements_limited(redlines.createEnumeration(), int(redlines.getCount())):
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
            reraise_if_disposed(exc)
            continue  # another text object, or not comparable
    return False


def tracked_change_spans_past(doc: Any, text_range: Any) -> bool:
    """True when a pending Insert or Delete redline starts before *text_range* and ends after it.

    ``SwDoc::InsertSwSection`` calls ``DocumentRedlineManager::SplitRedline`` over the new
    section. A redline that starts before and ends after the span becomes two pieces, and
    accepting or rejecting it then loses text outside the section; a redline that starts or
    ends at or inside the span resolves to the same text. ``tracked_change_before_point``
    checks a single collapsed insertion point and would refuse safe spans.
    """
    redlines = doc.getRedlines()
    for rl in iter_elements_limited(redlines.createEnumeration(), int(redlines.getCount())):
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
            reraise_if_disposed(exc)
            continue  # another text object, or not comparable
    return False
