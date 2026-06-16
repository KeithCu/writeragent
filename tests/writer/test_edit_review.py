# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for agent edit review config helpers (no UNO)."""

from unittest.mock import MagicMock, patch

import pytest

from plugin.writer.edit_review import (
    edit_review_wait_seconds,
    get_agent_edit_review_mode,
    review_recording_enabled,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("off", "off"),
        ("record", "record"),
        ("wait", "wait"),
        ("RECORD", "record"),
        (" Wait ", "wait"),
        ("bogus", "off"),
        ("", "off"),
        (None, "off"),
    ],
)
def test_get_agent_edit_review_mode(raw, expected):
    ctx = MagicMock()
    with patch("plugin.framework.config.get_config", return_value=raw):
        assert get_agent_edit_review_mode(ctx) == expected


@pytest.mark.parametrize(
    "mode,recording",
    [
        ("off", False),
        ("record", True),
        ("wait", True),
    ],
)
def test_review_recording_enabled(mode, recording):
    ctx = MagicMock()
    with patch("plugin.writer.edit_review.get_agent_edit_review_mode", return_value=mode):
        assert review_recording_enabled(ctx) is recording


@pytest.mark.parametrize(
    "mode,timeout,expected",
    [
        ("off", 900, 0),
        ("record", 900, 0),
        ("wait", 900, 900),
        ("wait", 0, 0),
        ("wait", -5, 0),
    ],
)
def test_edit_review_wait_seconds(mode, timeout, expected):
    ctx = MagicMock()
    with patch("plugin.writer.edit_review.get_agent_edit_review_mode", return_value=mode), \
         patch("plugin.framework.config.get_config_int_safe", return_value=timeout):
        assert edit_review_wait_seconds(ctx) == expected
