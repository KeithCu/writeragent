"""Annotation lock for panel_wiring leftovers (reportMissingParameterType)."""

from __future__ import annotations

from typing import Any, get_type_hints

from plugin.chatbot.panel_wiring import _wireControls


def test_wire_controls_annotates_self() -> None:
    """Module-level `_wireControls(self, ...)` is not a method; `self` still needs a type."""
    hints = get_type_hints(_wireControls)
    assert hints["self"] is Any
    assert hints["root_window"] is Any
    assert hints["has_recording"] is bool
