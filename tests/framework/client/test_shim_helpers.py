# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for plugin.framework.client.shim_helpers."""

from __future__ import annotations

from plugin.framework.client.shim_helpers import annotate_anthropic_cache, snapshot_text


def test_cache_marker_lands_before_blank_line_snapshot_block() -> None:
    """A leading blank line on the user-turn snapshot block must still be recognized as the tail block."""
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "hi"},
                {"type": "text", "text": "\n\n" + snapshot_text("d")},
            ],
        }
    ]

    annotated = annotate_anthropic_cache(messages)
    parts = annotated[0]["content"]
    assert "cache_control" in parts[0]
    assert "cache_control" not in parts[1]
