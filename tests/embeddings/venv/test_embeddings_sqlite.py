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
        embeddings_sqlite.ensure_schema(conn, dim=4, with_fts=True, with_vec=True, model="test-model")
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


def test_multi_model_schema_isolation(tmp_path):
    """Test that different models create separate virtual tables and do not interfere with each other."""
    db_path = tmp_path / "corpus.db"
    conn = embeddings_sqlite.connect_corpus_db(db_path)
    try:
        # Create schema for model A (dim 4) and model B (dim 8)
        embeddings_sqlite.ensure_schema(conn, dim=4, with_fts=False, with_vec=True, model="model-A")
        embeddings_sqlite.ensure_schema(conn, dim=8, with_fts=False, with_vec=True, model="model-B")

        chunk = {
            "doc_url": "file:///doc.odt",
            "para_index": 0,
            "char_start": 0,
            "char_end": 5,
            "content_hash": "hash1",
            "text": "hello",
            "file_mtime": 1.0,
        }

        # Upsert model A vector
        embeddings_sqlite.upsert_chunk_with_vector(
            conn, chunk, [1.0, 0.0, 0.0, 0.0], model="model-A", with_fts=False, with_vec=True
        )

        # Upsert model B vector (different dimension)
        embeddings_sqlite.upsert_chunk_with_vector(
            conn, chunk, [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0], model="model-B", with_fts=False, with_vec=True
        )

        conn.commit()

        # Check search in model A
        hits_a = embeddings_sqlite.vec0_search(conn, [1.0, 0.0, 0.0, 0.0], k=1, model="model-A")
        assert len(hits_a) == 1
        assert hits_a[0]["embedding_model"] == "model-A"

        # Check search in model B
        hits_b = embeddings_sqlite.vec0_search(conn, [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0], k=1, model="model-B")
        assert len(hits_b) == 1
        assert hits_b[0]["embedding_model"] == "model-B"

    finally:
        conn.close()


def test_legacy_vec_chunks_migration(tmp_path):
    """Test that a legacy vec_chunks table is renamed to default model table vec_chunks_all_MiniLM_L6_v2."""
    db_path = tmp_path / "corpus.db"
    conn = embeddings_sqlite.connect_corpus_db(db_path)
    try:
        # Create legacy vec_chunks table manually
        conn.execute("CREATE TABLE chunks (chunk_id INTEGER PRIMARY KEY, doc_url TEXT, para_index INTEGER, char_start INTEGER, char_end INTEGER, content_hash TEXT, file_mtime REAL, body TEXT)")
        embeddings_sqlite._load_vec_extension(conn)
        conn.execute("CREATE VIRTUAL TABLE vec_chunks USING vec0(chunk_id INTEGER PRIMARY KEY, embedding float[4])")
        conn.commit()

        # Run ensure_schema for a model, which should rename the legacy table
        embeddings_sqlite.ensure_schema(conn, dim=4, with_fts=False, with_vec=True, model="all-MiniLM-L6-v2")

        # Verify vec_chunks_all_MiniLM_L6_v2 exists
        res = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='vec_chunks_all_MiniLM_L6_v2'").fetchone()
        assert res is not None

        # Verify legacy vec_chunks table no longer exists
        res_legacy = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='vec_chunks'").fetchone()
        assert res_legacy is None
    finally:
        conn.close()
