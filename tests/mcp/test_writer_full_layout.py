# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for MCP layout helpers: mutate-then-read pair scheduling."""

from __future__ import annotations

import random

from tests.mcp.writer_full_layout import WRITER_MUTATE_READ_PAIRS, pick_followup_tool


def test_pick_followup_prefers_get_after_table_insert():
    rng = random.Random(0)
    names = ["table_insert", "get_document_content", "apply_document_content", "ping_unused"]
    follow = pick_followup_tool("table_insert", names, rng)
    assert follow in {"get_document_content", "apply_document_content", "table_get_cells"}
    assert follow in names


def test_pick_followup_skips_missing_tools():
    rng = random.Random(1)
    assert pick_followup_tool("table_insert", ["table_insert"], rng) is None
    assert pick_followup_tool("unknown_tool", ["get_document_content"], rng) is None


def test_pair_table_covers_apply_and_table_set():
    assert "get_document_content" in WRITER_MUTATE_READ_PAIRS["apply_document_content"]
    assert "get_document_content" in WRITER_MUTATE_READ_PAIRS["table_set_cell"]


def test_biased_sequence_is_not_independent():
    """With pair_bias=1 and only table_insert last, follow-up is always a pair."""
    rng = random.Random(7)
    names = list(WRITER_MUTATE_READ_PAIRS["table_insert"]) + ["table_insert", "unrelated"]
    for _i in range(20):
        follow = pick_followup_tool("table_insert", names, rng)
        assert follow in WRITER_MUTATE_READ_PAIRS["table_insert"]
