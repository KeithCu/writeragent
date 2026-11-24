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


def insert_recorded_content(model: Any, host: Any, cursor: Any, content: Any) -> None:
    """Insert *content* into *host* at *cursor*, recording an Insert redline when tracking is on."""
    # insertTextContent adds no redline, but text inside an Insert redline belongs to it;
    # deleting our own insertion removes it outright. Do not position from content.getAnchor():
    # its end maps before the object, so deleting the tail marker would delete the object.
    if not is_recording_changes(model):
        host.insertTextContent(cursor, content, False)
        return
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


def redline_kinds_covering(doc: Any, text_range: Any) -> set[str]:
    """``RedlineType`` of every tracked change that covers all of *text_range*."""
    kinds: set[str] = set()
    redlines = doc.getRedlines()
    for rl in iter_elements_limited(redlines.createEnumeration(), int(redlines.getCount())):
        try:
            if bounds_cover(redline_bounds(rl), text_range):
                kinds.add(str(rl.getPropertyValue("RedlineType")))
        except Exception as exc:
            reraise_if_disposed(exc)
            continue  # the redline's bounds or type could not be read
    return kinds


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
