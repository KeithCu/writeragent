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


class _FakeText:
    def __init__(self, *, raise_on_compare: bool = False):
        self._raise_on_compare = raise_on_compare

    def compareRegionStarts(self, a, b):
        if self._raise_on_compare:
            raise RuntimeError("different text")
        return 1 if a[0] < b[0] else (0 if a[0] == b[0] else -1)

    def compareRegionEnds(self, a, b):
        if self._raise_on_compare:
            raise RuntimeError("different text")
        return 1 if a[1] < b[1] else (0 if a[1] == b[1] else -1)


class _FakePoint:
    def __init__(self, lo, hi=None, *, text=None):
        self._lo = lo
        self._hi = lo if hi is None else hi
        self._text = text

    def __getitem__(self, idx):
        return (self._lo, self._hi)[idx]

    def getText(self):
        return self._text


class _FakeRange:
    def __init__(self, lo, hi, *, text=None):
        self._lo = _FakePoint(lo, text=text)
        self._hi = _FakePoint(hi, text=text)

    def getStart(self):
        return self._lo

    def getEnd(self):
        return self._hi


class _FakeRL:
    def __init__(self, kind, start, end, *, author="WriterAgent"):
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


class _FakeEnum:
    def __init__(self, items):
        self._items = list(items)
        self._i = 0

    def hasMoreElements(self):
        return self._i < len(self._items)

    def nextElement(self):
        v = self._items[self._i]
        self._i += 1
        return v


class _FakeRedlines:
    def __init__(self, items):
        self._items = items

    def getCount(self):
        return len(self._items)

    def createEnumeration(self):
        return _FakeEnum(self._items)


class _FakeRedlineDoc:
    def __init__(self, items):
        self._items = items

    def getRedlines(self):
        return _FakeRedlines(self._items)


def test_redline_kinds_covering():
    text = _FakeText()
    bad_text = _FakeText(raise_on_compare=True)
    target = _FakePoint(5, 10, text=text)
    doc = _FakeRedlineDoc([
        _FakeRL("Delete", _FakePoint(0, 0, text=text), _FakePoint(15, 15, text=text)),   # covers
        _FakeRL("Insert", _FakePoint(5, 5, text=text), _FakePoint(10, 10, text=text)),   # exact match covers
        _FakeRL("Format", _FakePoint(6, 6, text=text), _FakePoint(12, 12, text=text)),   # starts after target
        _FakeRL("Format", _FakePoint(0, 0, text=bad_text), _FakePoint(15, 15, text=bad_text)),  # compare raises -> skipped
    ])
    assert redline_kinds_covering(doc, target) == {"Delete", "Insert"}

    class _DisposedRL:
        def getPropertyValue(self, name):
            if name == "RedlineStart":
                raise DocumentDisposedError("gone")
            return _FakePoint(0, 0, text=text)

    with pytest.raises(DocumentDisposedError):
        redline_kinds_covering(_FakeRedlineDoc([_DisposedRL()]), target)


@pytest.mark.parametrize(
    "start,end,expected",
    [
        pytest.param(5, 15, True, id="overlaps_start"),
        pytest.param(15, 25, True, id="overlaps_end"),
        pytest.param(5, 25, True, id="contains_range"),
        pytest.param(12, 18, True, id="inside_range"),
        pytest.param(10, 20, True, id="exact_match"),
        pytest.param(0, 10, False, id="adjacent_before"),
        pytest.param(20, 30, False, id="adjacent_after"),
    ],
)
def test_overlaps_foreign_insertion_bounds(start, end, expected):
    text = _FakeText()
    ranges = [_FakeRange(10, 20, text=text)]
    doc = _FakeRedlineDoc([_FakeRL("Insert", _FakePoint(start, text=text), _FakePoint(end, text=text), author="Alice")])
    assert overlaps_foreign_insertion(doc, ranges, "WriterAgent") is expected


def test_overlaps_foreign_insertion():
    text = _FakeText()
    ranges = [_FakeRange(10, 20, text=text), _FakeRange(40, 50, text=text)]

    # Own author and non-Insert redlines are ignored even when overlapping.
    assert overlaps_foreign_insertion(_FakeRedlineDoc([
        _FakeRL("Insert", _FakePoint(12, text=text), _FakePoint(18, text=text), author="WriterAgent"),
        _FakeRL("Delete", _FakePoint(12, text=text), _FakePoint(18, text=text), author="Alice"),
        _FakeRL("Format", _FakePoint(12, text=text), _FakePoint(18, text=text), author="Alice"),
    ]), ranges, "WriterAgent") is False

    # Later range in a multi-range target is also checked.
    assert overlaps_foreign_insertion(_FakeRedlineDoc([
        _FakeRL("Insert", _FakePoint(42, text=text), _FakePoint(48, text=text), author="Alice"),
    ]), ranges, "WriterAgent") is True

    # Real compareRegionStarts error fails closed (returns True).
    bad_text = _FakeText(raise_on_compare=True)
    assert overlaps_foreign_insertion(_FakeRedlineDoc([
        _FakeRL("Insert", _FakePoint(0, text=bad_text), _FakePoint(5, text=bad_text), author="Alice"),
    ]), ranges, "WriterAgent") is True

    # Table-insertion shape (RedlineStart has no getText) also fails closed (returns True).
    assert overlaps_foreign_insertion(_FakeRedlineDoc([
        _FakeRL("Insert", object(), object(), author="Alice"),
    ]), ranges, "WriterAgent") is True

    class _DisposedRL:
        def getPropertyValue(self, name):
            if name == "RedlineType":
                return "Insert"
            if name == "RedlineAuthor":
                return "Alice"
            if name == "RedlineStart":
                raise DocumentDisposedError("gone")
            return _FakePoint(0, text=text)

    with pytest.raises(DocumentDisposedError):
        overlaps_foreign_insertion(_FakeRedlineDoc([_DisposedRL()]), ranges, "WriterAgent")


def test_redline_kinds_covering_table_start():
    text = MagicMock()
    text.compareRegionStarts.return_value = 0
    text.compareRegionEnds.return_value = 0
    anchor = MagicMock()
    table = MagicMock()
    table.supportsService.side_effect = lambda s: s == "com.sun.star.text.TextTable"
    table.getAnchor.return_value = anchor
    end = MagicMock()
    end.getText.return_value = text

    rl = MagicMock()
    rl.getPropertyValue.side_effect = lambda k: {
        "RedlineStart": table,
        "RedlineEnd": end,
        "RedlineType": "Delete",
    }[k]
    redlines = MagicMock()
    redlines.getCount.return_value = 1
    redlines.createEnumeration.return_value = MagicMock(
        hasMoreElements=MagicMock(side_effect=[True, False]),
        nextElement=MagicMock(return_value=rl),
    )
    doc = MagicMock()
    doc.getRedlines.return_value = redlines
    assert redline_kinds_covering(doc, MagicMock()) == {"Delete"}
    text.compareRegionStarts.assert_called_once()
    assert text.compareRegionStarts.call_args[0][0] is anchor
