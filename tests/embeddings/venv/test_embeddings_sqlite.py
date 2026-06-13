# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.embeddings.venv.embeddings_sqlite."""

from __future__ import annotations

import pytest

sqlite_vec = pytest.importorskip("sqlite_vec")

from plugin.embeddings.venv import embeddings_sqlite  # noqa: E402


def test_corpus_schema_fts_and_vec_round_trip(tmp_path):
    db_path = tmp_path / "corpus.db"
    conn = embeddings_sqlite.connect_corpus_db(db_path)
    try:
        embeddings_sqlite.ensure_schema(conn, dim=4, with_fts=True, with_vec=True)
        chunk = {
            "doc_url": "file:///a.odt",
            "para_index": 0,
            "char_start": 0,
            "char_end": 11,
            "content_hash": "abc",
            "text": "hello world",
            "file_mtime": 1.0,
        }
        vector = [1.0, 0.0, 0.0, 0.0]
        embeddings_sqlite.upsert_chunk_with_vector(
            conn,
            chunk,
            vector,
            model="test-model",
            with_fts=True,
            with_vec=True,
        )
        conn.commit()
        assert embeddings_sqlite.corpus_chunk_count(conn) == 1
        hits = embeddings_sqlite.vec0_search(conn, vector, k=1, model="test-model")
        assert len(hits) == 1
        assert hits[0]["doc_url"] == "file:///a.odt"
    finally:
        conn.close()


def test_insert_paragraph_rows_for_fts_only(tmp_path):
    db_path = tmp_path / "corpus.db"
    conn = embeddings_sqlite.connect_corpus_db(db_path)
    try:
        embeddings_sqlite.ensure_schema(conn, with_fts=True, with_vec=False)
        count = embeddings_sqlite.insert_paragraph_rows(
            conn,
            [{"text": "budget figures", "doc_url": "file:///b.odt", "para_index": 2, "content_hash": "h1"}],
            with_fts=True,
        )
        assert count == 1
        assert embeddings_sqlite.corpus_chunk_count(conn) == 1
    finally:
        conn.close()


def test_rebuild_fts_corpus_index_enables_search(tmp_path):
    db_path = tmp_path / "corpus.db"
    conn = embeddings_sqlite.connect_corpus_db(db_path)
    try:
        embeddings_sqlite.ensure_schema(conn, with_fts=True, with_vec=False)
        embeddings_sqlite.insert_paragraph_rows(
            conn,
            [{"text": "web search tools", "doc_url": "file:///part2.odt", "para_index": 0, "content_hash": "h1"}],
            with_fts=True,
        )
        before = embeddings_sqlite.fts_corpus_search(conn, "web search", k=1, near_slop=10)
        embeddings_sqlite.rebuild_fts_corpus_index(conn)
        after = embeddings_sqlite.fts_corpus_search(conn, "web search", k=1, near_slop=10)
        assert not before
        assert after
        assert "part2.odt" in after[0]["doc_url"]
    finally:
        conn.close()
