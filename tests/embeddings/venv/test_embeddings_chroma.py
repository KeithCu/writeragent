# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.embeddings.venv.embeddings_chroma."""

from __future__ import annotations

import json
from unittest.mock import patch, MagicMock
import pytest

chromadb = pytest.importorskip("chromadb")

from plugin.embeddings.venv import embeddings_chroma
from plugin.embeddings.venv.embeddings_sqlite import connect_corpus_db, corpus_chunk_count


@pytest.fixture
def temp_meta_file(tmp_path):
    p = tmp_path / "corpus_meta.json"
    p.write_text("{}", encoding="utf-8")
    return p


def test_chroma_ingest_and_search_round_trip(tmp_path, temp_meta_file):
    db_path = tmp_path / "corpus.db"
    
    rows = [
        {
            "doc_url": "file:///doc1.odt",
            "para_index": 0,
            "char_start": 0,
            "char_end": 11,
            "content_hash": "h1",
            "text": "hello world",
            "file_mtime": 1.0,
        },
        {
            "doc_url": "file:///doc1.odt",
            "para_index": 1,
            "char_start": 0,
            "char_end": 12,
            "content_hash": "h2",
            "text": "chroma tests",
            "file_mtime": 1.0,
        }
    ]

    # Mock embed_texts to return dummy vectors
    dummy_vectors = [[0.1, 0.2, 0.3, 0.4], [0.5, 0.6, 0.7, 0.8]]
    with patch("plugin.embeddings.venv.embeddings_chroma.embed_texts", return_value={"vectors": dummy_vectors, "dim": 4}):
        res = embeddings_chroma.chroma_ingest(
            str(db_path),
            str(temp_meta_file),
            "dummy-model",
            rows,
            build_fts=True,
            build_vectors=True
        )

        assert res["storage_backend"] == "chroma"
        assert res["dim"] == 4
        assert res["indexed"] == 2

    # Check SQLite contents (metadata + FTS)
    conn = connect_corpus_db(db_path)
    try:
        assert corpus_chunk_count(conn) == 2
    finally:
        conn.close()

    # Query Chroma KNN Search
    with patch("plugin.embeddings.venv.embeddings_chroma.embed_texts", return_value={"vectors": [[0.1, 0.2, 0.3, 0.4]], "dim": 4}):
        search_res = embeddings_chroma.chroma_knn_search(
            str(db_path),
            "hello",
            k=1,
            model_name="dummy-model",
            use_mmr=False
        )
        assert len(search_res["hits"]) == 1
        assert search_res["hits"][0]["doc_url"] == "file:///doc1.odt"
        assert "hello world" in search_res["hits"][0]["snippet"]

    # Query Chroma Hybrid Search
    with patch("plugin.embeddings.venv.embeddings_chroma.embed_texts", return_value={"vectors": [[0.1, 0.2, 0.3, 0.4]], "dim": 4}):
        hybrid_res = embeddings_chroma.chroma_hybrid_search(
            str(db_path),
            "chroma",
            k=1,
            model_name="dummy-model",
            use_mmr=False
        )
        assert len(hybrid_res["hits"]) == 1
        assert hybrid_res["hits"][0]["doc_url"] == "file:///doc1.odt"

    # Test Deletion
    del_keys = [{"doc_url": "file:///doc1.odt", "para_index": 0}]
    embeddings_chroma.chroma_ingest(
        str(db_path),
        str(temp_meta_file),
        "dummy-model",
        [],
        delete_keys=del_keys,
        build_fts=True,
        build_vectors=True
    )

    # Verify deleted in SQLite
    conn = connect_corpus_db(db_path)
    try:
        assert corpus_chunk_count(conn) == 1
    finally:
        conn.close()
