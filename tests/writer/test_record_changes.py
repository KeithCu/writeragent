# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.writer.record_changes."""

from unittest.mock import MagicMock
import pytest

from plugin.writer.record_changes import is_recording_changes, record_changes


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
