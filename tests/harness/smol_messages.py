# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared smolagents message formatting helpers for tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from plugin.contrib.smolagents.models import ChatMessage


def flatten_messages(messages: list[ChatMessage]) -> str:
    """Join text parts from a list of ChatMessage objects for substring assertions."""
    parts: list[str] = []
    for msg in messages:
        content = msg.content
        if isinstance(content, list):
            parts.extend(str(item.get("text", item)) if isinstance(item, dict) else str(item) for item in content)
        elif content is not None:
            parts.append(str(content))
    return "\n".join(parts)
