# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.embeddings.venv.embeddings_ingest_graph."""

from __future__ import annotations

import json
from unittest.mock import patch

from plugin.embeddings.venv.embeddings_ingest_graph import delete_stale, embed_and_upsert_batches
from plugin.embeddings.venv.embeddings_sqlite import connect_corpus_db, corpus_chunk_count


def test_delete_stale_skips_vec_schema_until_dim_known(tmp_path):
    """Cold build failed when delete_stale created vec_chunks before embed ran and knew dim."""
    db_path = tmp_path / "corpus.db"
    meta_path = tmp_path / "corpus_meta.json"
    meta_path.write_text(json.dumps({"embedding_model": "all-MiniLM-L6-v2"}), encoding="utf-8")

    state = {
        "db_path": str(db_path),
        "meta_path": str(meta_path),
        "model": "all-MiniLM-L6-v2",
        "build_fts": True,
        "build_vectors": True,
        "chunks": [
            {
                "doc_url": "file:///tmp/a.odt",
                "para_index": 0,
                "char_start": 0,
                "char_end": 12,
                "content_hash": "abc",
                "text": "hello world",
            }
        ],
        "delete_keys": [],
    }
    delete_stale(state)

    conn = connect_corpus_db(db_path)
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "chunks" in tables
        assert "vec_chunks" not in tables
    finally:
        conn.close()


def test_embed_and_upsert_batches_calls_embed_in_windows(tmp_path, monkeypatch):
    """Large ingest runs embed+upsert in fixed-size windows, not one mega batch."""
    monkeypatch.setattr("plugin.embeddings.venv.embeddings_ingest_graph.EMBEDDINGS_INGEST_BATCH_SIZE", 2)

    db_path = tmp_path / "corpus.db"
    meta_path = tmp_path / "corpus_meta.json"
    meta_path.write_text(json.dumps({"embedding_model": "test-model"}), encoding="utf-8")

    chunks = [
        {
            "doc_url": "file:///tmp/a.odt",
            "para_index": i,
            "char_start": 0,
            "char_end": 4,
            "content_hash": f"h{i}",
            "text": f"text{i}",
        }
        for i in range(5)
    ]
    state = {
        "db_path": str(db_path),
        "meta_path": str(meta_path),
        "model": "test-model",
        "build_fts": False,
        "build_vectors": True,
        "chunks": chunks,
    }

    embed_calls: list[list[str]] = []

    def _fake_embed(model: str, texts: list[str], **kwargs: object) -> dict:
        embed_calls.append(list(texts))
        dim = 4
        return {
            "model": model,
            "dim": dim,
            "vectors": [[0.1, 0.2, 0.3, 0.4] for _ in texts],
            "indices": list(range(len(texts))),
        }

    with patch("plugin.embeddings.venv.embeddings_ingest_graph.embed_texts", side_effect=_fake_embed):
        result = embed_and_upsert_batches(state)

    assert result["upserted"] == 5
    assert result["dim"] == 4
    assert embed_calls == [["text0", "text1"], ["text2", "text3"], ["text4"]]

    conn = connect_corpus_db(db_path)
    try:
        assert corpus_chunk_count(conn) == 5
    finally:
        conn.close()


def test_relative_age_expiry_cleanup(tmp_path):
    """Test that model-specific virtual tables and metadata are cleaned up after 7 days relative to the active model."""
    db_path = tmp_path / "corpus.db"
    meta_path = tmp_path / "corpus_meta.json"
    meta_path.write_text('{"embedding_model": "active-model"}', encoding="utf-8")

    from plugin.embeddings.venv.embeddings_sqlite import ensure_schema, model_slug
    import time

    # 1. Pre-create schema and populate model_metadata for active, fresh, and stale models
    conn = connect_corpus_db(db_path)
    try:
        ensure_schema(conn, dim=4, with_fts=False, with_vec=True, model="active-model")
        ensure_schema(conn, dim=4, with_fts=False, with_vec=True, model="fresh-other-model")
        ensure_schema(conn, dim=4, with_fts=False, with_vec=True, model="stale-other-model")

        now = time.time()
        # Insert metadata manually to control timestamps
        conn.execute(
            "INSERT OR REPLACE INTO model_metadata (embedding_model, dim, updated_at) VALUES (?, ?, ?)",
            ("fresh-other-model", 4, now - 3 * 24 * 3600),  # 3 days ago
        )
        conn.execute(
            "INSERT OR REPLACE INTO model_metadata (embedding_model, dim, updated_at) VALUES (?, ?, ?)",
            ("stale-other-model", 4, now - 8 * 24 * 3600),  # 8 days ago
        )
        conn.commit()

        # Verify tables and metadata exist before running ingest
        tables_before = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert f"vec_chunks_{model_slug('active-model')}" in tables_before
        assert f"vec_chunks_{model_slug('fresh-other-model')}" in tables_before
        assert f"vec_chunks_{model_slug('stale-other-model')}" in tables_before

        meta_before = {row[0] for row in conn.execute("SELECT embedding_model FROM model_metadata").fetchall()}
        assert "fresh-other-model" in meta_before
        assert "stale-other-model" in meta_before
    finally:
        conn.close()

    # 2. Run embed_and_upsert_batches for active-model
    state = {
        "db_path": str(db_path),
        "meta_path": str(meta_path),
        "model": "active-model",
        "build_fts": False,
        "build_vectors": True,
        "chunks": [
            {
                "doc_url": "file:///tmp/a.odt",
                "para_index": 0,
                "char_start": 0,
                "char_end": 4,
                "content_hash": "h0",
                "text": "text0",
            }
        ],
    }

    def _fake_embed(model: str, texts: list[str], **kwargs: object) -> dict:
        return {
            "model": model,
            "dim": 4,
            "vectors": [[0.1, 0.2, 0.3, 0.4] for _ in texts],
            "indices": list(range(len(texts))),
        }

    with patch("plugin.embeddings.venv.embeddings_ingest_graph.embed_texts", side_effect=_fake_embed):
        embed_and_upsert_batches(state)

    # 3. Verify that the stale table and metadata are deleted, but the fresh and active ones remain
    conn = connect_corpus_db(db_path)
    try:
        tables_after = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert f"vec_chunks_{model_slug('active-model')}" in tables_after
        assert f"vec_chunks_{model_slug('fresh-other-model')}" in tables_after
        assert f"vec_chunks_{model_slug('stale-other-model')}" not in tables_after

        meta_after = {row[0] for row in conn.execute("SELECT embedding_model FROM model_metadata").fetchall()}
        assert "active-model" in meta_after
        assert "fresh-other-model" in meta_after
        assert "stale-other-model" not in meta_after
    finally:
        conn.close()

