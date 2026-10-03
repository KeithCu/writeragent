"""Annotation lock for panel_wiring leftovers (reportMissingParameterType)."""

from __future__ import annotations

import logging
from typing import Any, get_type_hints
from unittest.mock import MagicMock

import pytest

from plugin.chatbot.panel_wiring import _install_frame_session_listeners, _wireControls


def test_wire_controls_annotates_self() -> None:
    """Module-level `_wireControls(self, ...)` is not a method; `self` still needs a type."""
    hints = get_type_hints(_wireControls)
    assert hints["self"] is Any
    assert hints["root_window"] is Any
    assert hints["has_recording"] is bool


def test_install_thread_violation_propagates() -> None:
    """FrameSession.install re-raises a UNO thread violation; wiring must not swallow it."""
    session = MagicMock()
    session.install.side_effect = RuntimeError("UNO thread violation: addFocusListener")
    query = object()
    with pytest.raises(RuntimeError, match="UNO thread violation"):
        _install_frame_session_listeners(session, object(), query, ())
    session.set_focus_pin.assert_called_once_with(query)


def test_install_other_errors_stay_a_debug_log(caplog: pytest.LogCaptureFixture) -> None:
    """A normal attach failure is still logged and does not leave wiring."""
    session = MagicMock()
    session.install.side_effect = RuntimeError("focus attach failed")
    with caplog.at_level(logging.DEBUG, logger="plugin.chatbot.panel_wiring"):
        _install_frame_session_listeners(session, object(), object(), ())
    assert any("frame session focus install" in record.message for record in caplog.records)
