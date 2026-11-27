# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Tests for plugin.doc.peer_prompt."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from plugin.doc.peer_prompt import format_peer_outer_delegate_hint
from plugin.doc.specialized_base import DelegateToSubagent


def _model(service: str) -> MagicMock:
    m = MagicMock()
    m.supportsService.side_effect = lambda s: s == service
    return m


@pytest.mark.parametrize(
    ("model", "gateway"),
    [
        (_model("com.sun.star.sheet.SpreadsheetDocument"), DelegateToSubagent),
        (_model("com.sun.star.drawing.DrawingDocument"), DelegateToSubagent),
        (_model("com.sun.star.presentation.PresentationDocument"), DelegateToSubagent),
        (_model("com.sun.star.text.TextDocument"), DelegateToSubagent),
        (None, DelegateToSubagent),
    ],
)
def test_outer_delegate_hint_names_the_registered_gateway(model: Any, gateway: Any) -> None:
    """Pin that format_peer_outer_delegate_hint matches the registered gateway name."""
    assert gateway.name in format_peer_outer_delegate_hint()
