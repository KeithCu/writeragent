# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for parent-paragraph hierarchical hit expansion."""
from __future__ import annotations

from unittest.mock import MagicMock

from plugin.embeddings.embeddings_fs import content_hash
from plugin.embeddings.venv.embeddings_parent_hits import (
    expand_candidates_to_parent_paragraphs,
    merge_candidates_by_paragraph,
    merge_nodes_by_paragraph,
)
from plugin.embeddings.venv.embeddings_sqlite import connect_corpus_db, ensure_schema, paragraph_bodies_for_locators, upsert_chunk_with_vector


def _seed_multi_chunk_paragraph(db_path, *, doc_url: str = "file:///a.odt", para_index: int = 3) -> None:
    conn = connect_corpus_db(db_path)
    try:
        ensure_schema(conn, with_fts=False, with_vec=False)
        for char_start, char_end, text in (
            (0, 10, "First part of"),
            (10, 20, "a long para"),
            (20, 30, "graph body."),
        ):
            upsert_chunk_with_vector(
                conn,
                {
                    "doc_url": doc_url,
                    "para_index": para_index,
                    "char_start": char_start,
                    "char_end": char_end,
                    "content_hash": content_hash(text),
                    "text": text,
                    "file_mtime": 1.0,
                },
                [],
                model="",
                with_fts=False,
                with_vec=False,
            )
        conn.commit()
    finally:
        conn.close()


def test_paragraph_bodies_for_locators_concat_chunks(tmp_path) -> None:
    db_path = tmp_path / "corpus.db"
    _seed_multi_chunk_paragraph(db_path)

    conn = connect_corpus_db(db_path)
    try:
        bodies = paragraph_bodies_for_locators(conn, {("file:///a.odt", 3)})
    finally:
        conn.close()

    assert bodies[("file:///a.odt", 3)] == "First part of a long para graph body."


def test_merge_candidates_by_paragraph_keeps_highest_score() -> None:
    bodies = {("file:///a.odt", 1): "Full paragraph text."}
    candidates = [
        {"doc_url": "file:///a.odt", "para_index": 1, "snippet": "chunk A", "score": 0.4},
        {"doc_url": "file:///a.odt", "para_index": 1, "snippet": "chunk B", "score": 0.9, "matched_by": ["vec"]},
        {"doc_url": "file:///b.odt", "para_index": 0, "snippet": "other", "score": 0.5},
    ]
    merged = merge_candidates_by_paragraph(candidates, bodies)

    assert len(merged) == 2
    assert merged[0]["doc_url"] == "file:///a.odt"
    assert merged[0]["score"] == 0.9
    assert merged[0]["snippet"] == "Full paragraph text."
    assert merged[0]["parent_expanded"] is True


def test_expand_candidates_to_parent_paragraphs_from_db(tmp_path) -> None:
    db_path = tmp_path / "corpus.db"
    _seed_multi_chunk_paragraph(db_path)
    candidates = [
        {"doc_url": "file:///a.odt", "para_index": 3, "snippet": "First part of", "score": 0.7},
        {"doc_url": "file:///a.odt", "para_index": 3, "snippet": "graph body.", "score": 0.6},
    ]

    merged = expand_candidates_to_parent_paragraphs(str(db_path), candidates)

    assert len(merged) == 1
    assert merged[0]["snippet"] == "First part of a long para graph body."
    assert merged[0]["parent_expanded"] is True


def test_merge_nodes_by_paragraph_expands_text() -> None:
    node_low = MagicMock()
    node_low.node.text = "chunk low"
    node_low.node.metadata = {"doc_url": "file:///a.odt", "para_index": 2}
    node_low.score = 0.3

    node_high = MagicMock()
    node_high.node.text = "chunk high"
    node_high.node.metadata = {"doc_url": "file:///a.odt", "para_index": 2, "matched_by": ["fts"]}
    node_high.score = 0.8

    bodies = {("file:///a.odt", 2): "Expanded parent paragraph."}
    merged = merge_nodes_by_paragraph([node_low, node_high], bodies)

    assert len(merged) == 1
    assert merged[0].score == 0.8
    assert merged[0].node.text == "Expanded parent paragraph."
    assert merged[0].node.metadata["parent_expanded"] is True
