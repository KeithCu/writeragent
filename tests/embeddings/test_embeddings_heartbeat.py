# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.embeddings.embeddings_heartbeat."""

from __future__ import annotations

from plugin.embeddings.embeddings_heartbeat import format_index_heartbeat_line, heartbeat_counts_from_payload


def test_format_index_heartbeat_line_singular():
    line = format_index_heartbeat_line("a.odt", paragraphs=1, chunks=1, elapsed_sec=0.12)
    assert line == "a.odt: 1 paragraph, 1 chunk, 0.12s"


def test_format_index_heartbeat_line_plural():
    line = format_index_heartbeat_line("b.odt", paragraphs=105, chunks=107, elapsed_sec=1.234)
    assert line == "b.odt: 105 paragraphs, 107 chunks, 1.23s"


def test_heartbeat_counts_from_payload_prefers_upserted():
    paragraphs, chunks = heartbeat_counts_from_payload({"paragraphs": 5, "upserted": 6})
    assert paragraphs == 5
    assert chunks == 6
