# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.embeddings.embeddings_heartbeat."""

from __future__ import annotations

from plugin.embeddings.embeddings_heartbeat import format_index_heartbeat_line, heartbeat_counts_from_payload
import pytest


@pytest.mark.parametrize(
    "value, paragraphs, chunks, elapsed_sec, expected",
    [
        pytest.param("a.odt", 1, 1, 0.12, "a.odt: 1 paragraph, 1 chunk, 0.12s", id="test_format_index_heartbeat_line_singular"),
        pytest.param("b.odt", 105, 107, 1.234, "b.odt: 105 paragraphs, 107 chunks, 1.23s", id="test_format_index_heartbeat_line_plural"),
    ],
)
def test_format_index_heartbeat_line_singular(value, paragraphs, chunks, elapsed_sec, expected):
    line = format_index_heartbeat_line(value, paragraphs=paragraphs, chunks=chunks, elapsed_sec=elapsed_sec)
    assert line == expected

def test_heartbeat_counts_from_payload_prefers_upserted():
    paragraphs, chunks = heartbeat_counts_from_payload({"paragraphs": 5, "upserted": 6})
    assert paragraphs == 5
    assert chunks == 6
