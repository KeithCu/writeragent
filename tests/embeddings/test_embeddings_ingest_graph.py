# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.embeddings.venv.embeddings_ingest_graph."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from plugin.embeddings.venv.embeddings_ingest_graph import delete_stale, embed_and_upsert_batches
from plugin.embeddings.venv.embeddings_sqlite import connect_corpus_db, corpus_chunk_count


def _pysqlite3_shaped_module(monkeypatch) -> str:
    """Module name whose OperationalError is not sqlite3.OperationalError.

    Uses pysqlite3 when that class is installed and is not a stdlib subclass.
    Otherwise registers a stand-in module. ``_conn_operational_error`` resolves
    the class by importing ``type(conn).__module__`` and reading OperationalError.
    """
    import sqlite3
    import sys
    import types

    try:
        from pysqlite3 import dbapi2 as pysqlite3
    except ImportError:
        pysqlite3 = None
    if pysqlite3 is not None:
        err = getattr(pysqlite3, "OperationalError", None)
        if isinstance(err, type) and issubclass(err, Exception) and not issubclass(err, sqlite3.OperationalError):
            probe = pysqlite3.connect(":memory:")
            try:
                return type(probe).__module__
            finally:
                probe.close()

    module_name = "writeragent_test_fake_pysqlite3"

    class OperationalError(Exception):
        """Stand-in for pysqlite3.OperationalError; not a sqlite3 subclass."""

    fake = types.ModuleType(module_name)
    fake.OperationalError = OperationalError
    monkeypatch.setitem(sys.modules, module_name, fake)
    return module_name


def test_cold_model_metadata_swallows_pysqlite3_operational_error(tmp_path, monkeypatch):
    """A missing model_metadata table must not abort when OperationalError is not sqlite3's."""
    import sqlite3

    from plugin.embeddings.venv.embeddings_sqlite import _conn_operational_error

    module_name = _pysqlite3_shaped_module(monkeypatch)

    class _Conn:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, params=()):
            if isinstance(sql, str) and "SELECT dim FROM model_metadata" in sql:
                raise _conn_operational_error(self)("no such table: model_metadata")
            if params:
                return self._inner.execute(sql, params)
            return self._inner.execute(sql)

        def commit(self):
            return self._inner.commit()

        def close(self):
            return self._inner.close()

        def __getattr__(self, name):
            return getattr(self._inner, name)

    # Helper looks up OperationalError on the connection class's module.
    _Conn.__module__ = module_name

    def _connect(path):
        return _Conn(connect_corpus_db(path))

    probe = _connect(tmp_path / "probe.db")
    try:
        resolved = _conn_operational_error(probe)
    finally:
        probe.close()
    assert resolved is not sqlite3.OperationalError
    assert not issubclass(resolved, sqlite3.OperationalError)

    monkeypatch.setattr("plugin.embeddings.venv.embeddings_ingest_graph.connect_corpus_db", _connect)
    monkeypatch.setattr("plugin.embeddings.venv.embeddings_sqlite._load_vec_extension", lambda conn: None)

    meta_path = tmp_path / "corpus_meta.json"
    meta_path.write_text(json.dumps({"embedding_model": "all-MiniLM-L6-v2"}), encoding="utf-8")
    db_path = tmp_path / "corpus.db"
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
        assert "model_metadata" in tables
        assert "vec_chunks" not in tables
    finally:
        conn.close()

    # embed_and_upsert_batches hits the same lookup before any vec0 DDL.
    empty = {
        "db_path": str(tmp_path / "embed.db"),
        "meta_path": str(meta_path),
        "model": "all-MiniLM-L6-v2",
        "build_fts": False,
        "build_vectors": True,
        "chunks": [],
    }
    assert embed_and_upsert_batches(empty) == {"upserted": 0, "dim": 0}


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


def _require_sqlite_vec() -> None:
    pytest.importorskip("sqlite_vec")
    from plugin.embeddings.venv.embeddings_sqlite import _dbapi

    # connect_corpus_db uses pysqlite3 when stdlib sqlite3 cannot load
    # extensions. Skip only if that opener still has no loader.
    dbapi = _dbapi()
    conn = dbapi.connect(":memory:")
    try:
        if not hasattr(conn, "enable_load_extension"):
            pytest.skip("sqlite cannot load extensions on this platform")
    finally:
        conn.close()


def test_embed_and_upsert_batches_calls_embed_in_windows(tmp_path, monkeypatch):
    """Large ingest runs embed+upsert in fixed-size windows, not one mega batch."""
    _require_sqlite_vec()
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
    _require_sqlite_vec()
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

