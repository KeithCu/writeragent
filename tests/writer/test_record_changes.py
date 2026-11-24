# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.writer.record_changes."""

from unittest.mock import MagicMock
import pytest

from plugin.framework.errors import DocumentDisposedError
from plugin.writer.record_changes import (
    is_recording_changes,
    overlaps_foreign_insertion,
    record_changes,
    redline_kinds_covering,
)


class _FakeDoc:
    def __init__(self, recording: bool = False):
        self.recording = recording

    def getPropertyValue(self, name: str):
        if name == "RecordChanges":
            return self.recording
        raise KeyError(name)

    def setPropertyValue(self, name: str, val):
        if name == "RecordChanges":
            self.recording = bool(val)


def test_is_recording_changes_reads_property():
    doc = _FakeDoc(recording=True)
    assert is_recording_changes(doc) is True

    doc.recording = False
    assert is_recording_changes(doc) is False


def test_is_recording_changes_handles_read_exception():
    doc = MagicMock()
    doc.getPropertyValue.side_effect = RuntimeError("failed to read property")
    assert is_recording_changes(doc) is False


def test_record_changes_context_manager_temporarily_sets_state():
    doc = _FakeDoc(recording=False)
    assert doc.recording is False

    with record_changes(doc, True) as was_recording:
        assert was_recording is False
        assert doc.recording is True

    assert doc.recording is False


def test_record_changes_preserves_initial_state_on_exception():
    doc = _FakeDoc(recording=True)

    with pytest.raises(ValueError, match="inside error"):
        with record_changes(doc, False) as was_recording:
            assert was_recording is True
            assert doc.recording is False
            raise ValueError("inside error")

    assert doc.recording is True


def test_record_changes_no_op_when_already_in_desired_state():
    doc = _FakeDoc(recording=True)
    with record_changes(doc, True) as was_recording:
        assert was_recording is True
        assert doc.recording is True
    assert doc.recording is True


def test_redline_kinds_covering():
    class _Text:
        def compareRegionStarts(self, a, b):
            if a == "bad" or b == "bad":
                raise RuntimeError("different text")
            return 1 if a[0] < b[0] else (0 if a[0] == b[0] else -1)

        def compareRegionEnds(self, a, b):
            if a == "bad" or b == "bad":
                raise RuntimeError("different text")
            return 1 if a[1] < b[1] else (0 if a[1] == b[1] else -1)

    text = _Text()

    class _Point:
        def __init__(self, lo, hi):
            self._lo = lo
            self._hi = hi

        def __getitem__(self, idx):
            return (self._lo, self._hi)[idx]

        def getText(self):
            return text

    class _RL:
        def __init__(self, kind, start, end):
            self._kind = kind
            self._start = start
            self._end = end

        def getPropertyValue(self, name):
            if name == "RedlineType":
                return self._kind
            if name == "RedlineStart":
                return self._start
            if name == "RedlineEnd":
                return self._end
            raise KeyError(name)

    class _Enum:
        def __init__(self, items):
            self._items = list(items)
            self._i = 0

        def hasMoreElements(self):
            return self._i < len(self._items)

        def nextElement(self):
            v = self._items[self._i]
            self._i += 1
            return v

    class _Redlines:
        def __init__(self, items):
            self._items = items

        def getCount(self):
            return len(self._items)

        def createEnumeration(self):
            return _Enum(self._items)

    class _Doc:
        def __init__(self, items):
            self._items = items

        def getRedlines(self):
            return _Redlines(self._items)

    target = _Point(5, 10)
    doc = _Doc([
        _RL("Delete", _Point(0, 0), _Point(15, 15)),   # covers
        _RL("Insert", _Point(5, 5), _Point(10, 10)),   # exact match covers
        _RL("Format", _Point(6, 6), _Point(12, 12)),   # starts after target
        _RL("Format", "bad", "bad"),                   # compare raises -> skipped
    ])
    assert redline_kinds_covering(doc, target) == {"Delete", "Insert"}

    class _DisposedRL:
        def getPropertyValue(self, name):
            if name == "RedlineStart":
                raise DocumentDisposedError("gone")
            return _Point(0, 0)

    with pytest.raises(DocumentDisposedError):
        redline_kinds_covering(_Doc([_DisposedRL()]), target)


def test_overlaps_foreign_insertion():
    class _Text:
        def compareRegionStarts(self, a, b):
            if a == "bad" or b == "bad":
                raise RuntimeError("different text")
            return 1 if a < b else (0 if a == b else -1)

    text = _Text()

    class _Point:
        def __init__(self, pos):
            self._pos = pos

        def __lt__(self, other):
            if other == "bad":
                raise RuntimeError("different text")
            return self._pos < other._pos

        def __eq__(self, other):
            return isinstance(other, _Point) and self._pos == other._pos

        def getText(self):
            return text

    class _Range:
        def __init__(self, lo, hi):
            self._lo = _Point(lo)
            self._hi = _Point(hi)

        def getStart(self):
            return self._lo

        def getEnd(self):
            return self._hi

    class _RL:
        def __init__(self, kind, author, start, end):
            self._kind = kind
            self._author = author
            self._start = start
            self._end = end

        def getPropertyValue(self, name):
            if name == "RedlineType":
                return self._kind
            if name == "RedlineAuthor":
                return self._author
            if name == "RedlineStart":
                return self._start
            if name == "RedlineEnd":
                return self._end
            raise KeyError(name)

    class _Enum:
        def __init__(self, items):
            self._items = list(items)
            self._i = 0

        def hasMoreElements(self):
            return self._i < len(self._items)

        def nextElement(self):
            v = self._items[self._i]
            self._i += 1
            return v

    class _Redlines:
        def __init__(self, items):
            self._items = items

        def getCount(self):
            return len(self._items)

        def createEnumeration(self):
            return _Enum(self._items)

    class _Doc:
        def __init__(self, items):
            self._items = items

        def getRedlines(self):
            return _Redlines(self._items)

    ranges = [_Range(10, 20)]
    # Own author and non-Insert redlines are ignored even when overlapping.
    assert overlaps_foreign_insertion(_Doc([
        _RL("Insert", "WriterAgent", _Point(12), _Point(18)),
        _RL("Delete", "Alice", _Point(12), _Point(18)),
        _RL("Format", "Alice", _Point(12), _Point(18)),
    ]), ranges, "WriterAgent") is False

    # Adjacent or disjoint foreign insertions do not overlap.
    assert overlaps_foreign_insertion(_Doc([
        _RL("Insert", "Alice", _Point(0), _Point(10)),
        _RL("Insert", "Alice", _Point(20), _Point(30)),
    ]), ranges, "WriterAgent") is False

    # Overlapping foreign insertion returns True.
    assert overlaps_foreign_insertion(_Doc([
        _RL("Insert", "Alice", _Point(15), _Point(25)),
    ]), ranges, "WriterAgent") is True

    # Comparison error fails closed (returns True).
    assert overlaps_foreign_insertion(_Doc([
        _RL("Insert", "Alice", "bad", "bad"),
    ]), ranges, "WriterAgent") is True

    class _DisposedRL:
        def getPropertyValue(self, name):
            if name == "RedlineType":
                return "Insert"
            if name == "RedlineAuthor":
                return "Alice"
            if name == "RedlineStart":
                raise DocumentDisposedError("gone")
            return _Point(0)

    with pytest.raises(DocumentDisposedError):
        overlaps_foreign_insertion(_Doc([_DisposedRL()]), ranges, "WriterAgent")
