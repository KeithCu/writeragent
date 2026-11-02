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

from plugin.framework.errors import reraise_if_disposed

log = logging.getLogger("writeragent.writer")


def is_recording_changes(model: Any) -> bool:
    """True if *model* is currently recording Track Changes (redlines).

    Agent edits made while recording must land as a clean tracked Delete + Insert so the
    user can accept (-> new text) or reject (-> old text) each one. The format-preserving
    replace paths (the char-by-char diff in ``replace_preserving_format`` and the
    paragraph-style restore) corrupt that into a per-character mess or a FORMAT
    redline that keeps the old text on Accept -- so those steps are skipped while recording.
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
