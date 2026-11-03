# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for the nested-text container oracle."""

from __future__ import annotations

import random

import pytest

from tests.mcp.nested_text_model import (
    CrossContainerError,
    NestedDocument,
    generate_story,
    mineru_story,
    mineru_story_wrong_body,
    run_ops,
    shrink_failing,
)


def test_mineru_story_stays_in_cell():
    doc = mineru_story()
    assert doc.cursor_kind == "cell"
    assert doc.cursor_id == "Table1:A2"
    assert doc.tables["Table1"][1][0] == "nerXU"
    assert doc.body == ""


def test_wrong_body_apply_fails_like_table_cell_bug():
    with pytest.raises(CrossContainerError, match="body XText"):
        mineru_story_wrong_body()


def test_range_cannot_leave_container():
    doc = NestedDocument(body="hello")
    doc.create_table("Table1", 3, 2)
    doc.set_cell("Table1", "A2", "MinerU")
    doc.move_selection("cell", "Table1:A2", 0, 6)
    with pytest.raises(CrossContainerError):
        doc.get_content("range", start=0, end=99)


def test_search_apply_finds_cell_not_body():
    doc = NestedDocument(body="MinerU in body")
    doc.create_table("Table1", 3, 2)
    doc.set_cell("Table1", "A2", "MinerU")
    doc.apply_content("MinerU-EDIT", target="search", old_content="MinerU")
    # First match is body (scan order); cell unchanged.
    assert "MinerU-EDIT" in doc.body
    assert doc.tables["Table1"][1][0] == "MinerU"

    doc2 = NestedDocument()
    doc2.create_table("Table1", 3, 2)
    doc2.set_cell("Table1", "A2", "MinerU")
    doc2.apply_content("MinerU-EDIT", target="search", old_content="MinerU")
    assert doc2.tables["Table1"][1][0] == "MinerU-EDIT"
    assert doc2.body == ""


def test_seeded_stories_keep_cursor_in_one_container():
    rng = random.Random(42)
    for i in range(20):
        ops = generate_story(rng, n_ops=10)
        doc = NestedDocument()
        run_ops(doc, ops)
        doc.check_cursor()


def test_shrink_keeps_a_failing_core():
    def fails(ops):
        return any(name == "apply_selection" for name, _kw in ops)

    ops = [
        ("create_table", {"name": "Table1", "rows": 3, "cols": 2}),
        ("get_selection", {}),
        ("apply_selection", {"content": "Z"}),
        ("get_selection", {}),
    ]
    shrunk = shrink_failing(ops, fails)
    assert any(name == "apply_selection" for name, _kw in shrunk)
    assert len(shrunk) <= len(ops)
