# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.embeddings.venv.embeddings_ingest_graph."""

from __future__ import annotations

import json

from plugin.embeddings.venv.embeddings_ingest_graph import delete_stale
from plugin.embeddings.venv.embeddings_sqlite import connect_corpus_db


def test_delete_stale_skips_vec_schema_until_dim_known(tmp_path):
    """Cold build failed when delete_stale created vec_chunks before embed_chunks knew dim."""
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
