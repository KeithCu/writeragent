# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.embeddings.venv.embeddings_sqlite."""

from __future__ import annotations

import pytest

sqlite_vec = pytest.importorskip("sqlite_vec")

from plugin.embeddings.venv import embeddings_sqlite  # noqa: E402


def test_build_match_query_single_word():
    assert embeddings_sqlite.build_match_query("numpy") == '"numpy"'


def test_build_match_query_multi_word_near():
    q = embeddings_sqlite.build_match_query("web search", near_slop=10)
    assert q.startswith("NEAR(")
    assert '"web"' in q
    assert '"search"' in q
    assert ", 10)" in q


def test_build_match_query_near_slash_syntax():
    q = embeddings_sqlite.build_match_query("ocean NEAR/3 warming", near_slop=10)
    assert "NEAR(" in q
    assert ", 3)" in q


def test_build_match_query_empty_raises():
    with pytest.raises(ValueError, match="query"):
        embeddings_sqlite.build_match_query("   ")


def test_strip_fts_snippet_markers():
    assert embeddings_sqlite.strip_fts_snippet_markers("[web] [search] tool-call") == "web search tool-call"
    assert embeddings_sqlite.strip_fts_snippet_markers("…[WEB]_[SEARCH]…") == "…WEB_SEARCH…"


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


@pytest.mark.skipif(
    not hasattr(__import__("sqlite3").Connection, "enable_load_extension"),
    reason="stdlib sqlite3 cannot load extensions",
)
def test_connect_corpus_db_keeps_stdlib_sqlite(tmp_path):
    """Linux/Windows/Homebrew stay on stdlib sqlite3 when it can load extensions."""
    import sqlite3

    embeddings_sqlite._dbapi_mod = None
    conn = embeddings_sqlite.connect_corpus_db(tmp_path / "corpus.db")
    try:
        assert type(conn) is sqlite3.Connection
    finally:
        conn.close()
        embeddings_sqlite._dbapi_mod = None


def test_connect_corpus_db_uses_pysqlite3_when_stdlib_cannot_load_extensions(tmp_path, monkeypatch):
    """python.org macOS CPython omits enable_load_extension; pysqlite3 still loads vec0."""
    import sqlite3

    pysqlite3 = pytest.importorskip("pysqlite3.dbapi2")
    embeddings_sqlite._dbapi_mod = None

    def fake_hasattr(obj, name, _real=hasattr):
        if name == "enable_load_extension" and obj is sqlite3.Connection:
            return False
        return _real(obj, name)

    monkeypatch.setattr(embeddings_sqlite, "hasattr", fake_hasattr, raising=False)
    conn = embeddings_sqlite.connect_corpus_db(tmp_path / "corpus.db")
    try:
        assert isinstance(conn, pysqlite3.Connection)
        embeddings_sqlite.ensure_schema(conn, dim=4, with_fts=True, with_vec=True, model="test-model")
        row = conn.execute("SELECT vec_version()").fetchone()
        assert row is not None
        assert row[0]
    finally:
        conn.close()
        embeddings_sqlite._dbapi_mod = None


def test_connect_corpus_db_stays_on_stdlib_without_pysqlite3(tmp_path, monkeypatch):
    """Host Python without pysqlite3 can still open the corpus for metadata."""
    import builtins
    import sqlite3

    embeddings_sqlite._dbapi_mod = None
    real_import = builtins.__import__

    def fake_hasattr(obj, name, _real=hasattr):
        if name == "enable_load_extension" and obj is sqlite3.Connection:
            return False
        return _real(obj, name)

    def fake_import(name, *args, **kwargs):
        if name == "pysqlite3" or (isinstance(name, str) and name.startswith("pysqlite3.")):
            raise ImportError("blocked")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(embeddings_sqlite, "hasattr", fake_hasattr, raising=False)
    monkeypatch.setattr(builtins, "__import__", fake_import)
    conn = embeddings_sqlite.connect_corpus_db(tmp_path / "corpus.db")
    try:
        assert type(conn) is sqlite3.Connection
        embeddings_sqlite.ensure_schema(conn)
    finally:
        conn.close()
        embeddings_sqlite._dbapi_mod = None


def test_load_vec_extension_names_pysqlite3_when_loader_missing():
    """A connection that cannot load extensions gets the install hint, not AttributeError."""
    import sqlite3

    class _Conn:
        def execute(self, sql, params=None):
            del sql, params
            raise sqlite3.OperationalError("no such function: vec_version")

    with pytest.raises(ImportError, match="pysqlite3"):
        embeddings_sqlite._load_vec_extension(_Conn())


def test_stored_model_dim_reads_metadata(tmp_path):
    db_path = tmp_path / "corpus.db"
    conn = embeddings_sqlite.connect_corpus_db(db_path)
    try:
        embeddings_sqlite.ensure_schema(conn, dim=8, with_vec=True, model="m1")
        conn.execute(
            "INSERT OR REPLACE INTO model_metadata (embedding_model, dim, updated_at) VALUES (?, ?, ?)",
            ("m1", 8, 1.0),
        )
        conn.commit()
        assert embeddings_sqlite.stored_model_dim(conn, "m1") == 8
        assert embeddings_sqlite.stored_model_dim(conn, "missing") is None
    finally:
        conn.close()


def test_missing_vector_chunks_with_and_without_vec_table(tmp_path):
    db_path = tmp_path / "corpus.db"
    conn = embeddings_sqlite.connect_corpus_db(db_path)
    try:
        embeddings_sqlite.ensure_schema(conn, with_fts=False, with_vec=False)
        conn.execute(
            """
            INSERT INTO chunks (
                doc_url, para_index, char_start, char_end, content_hash, file_mtime, body
            ) VALUES ('file:///a.odt', 0, 0, 5, 'h1', 1.0, 'hello')
            """
        )
        conn.commit()
        assert embeddings_sqlite._vec_table_if_exists(conn, "m1") is None
        all_missing = embeddings_sqlite.missing_vector_chunks(conn, "m1")
        assert len(all_missing) == 1
        assert all_missing[0]["text"] == "hello"

        embeddings_sqlite.ensure_schema(conn, dim=4, with_vec=True, model="m1")
        assert embeddings_sqlite._vec_table_if_exists(conn, "m1") == f"vec_chunks_{embeddings_sqlite.model_slug('m1')}"
        chunk = {
            "doc_url": "file:///b.odt",
            "para_index": 0,
            "char_start": 0,
            "char_end": 5,
            "content_hash": "h2",
            "text": "world",
            "file_mtime": 2.0,
        }
        embeddings_sqlite.upsert_chunk_with_vector(
            conn,
            chunk,
            [1.0, 0.0, 0.0, 0.0],
            model="m1",
            with_fts=False,
            with_vec=True,
        )
        conn.commit()
        gaps = embeddings_sqlite.missing_vector_chunks(conn, "m1")
        assert len(gaps) == 1
        assert gaps[0]["doc_url"] == "file:///a.odt"
        assert embeddings_sqlite.has_missing_vector_chunks(conn, "m1") is True
        embeddings_sqlite.ensure_schema(conn, dim=4, with_vec=True, model="m1")
        embeddings_sqlite.upsert_chunk_with_vector(
            conn,
            {
                "doc_url": "file:///a.odt",
                "para_index": 0,
                "char_start": 0,
                "char_end": 5,
                "content_hash": "h1",
                "text": "hello",
                "file_mtime": 1.0,
            },
            [1.0, 0.0, 0.0, 0.0],
            model="m1",
            with_fts=False,
            with_vec=True,
        )
        conn.commit()
        assert embeddings_sqlite.has_missing_vector_chunks(conn, "m1") is False
    finally:
        conn.close()


def test_connect_corpus_journal_wal_on_local_delete_on_remote(tmp_path, monkeypatch):
    db_path = tmp_path / "corpus.db"
    monkeypatch.setattr(embeddings_sqlite, "path_is_local_disk", lambda _p: True)
    conn1 = embeddings_sqlite.connect_corpus_db(db_path)
    try:
        mode = conn1.execute("PRAGMA journal_mode").fetchone()[0]
        assert str(mode).casefold() == "wal"
    finally:
        conn1.close()

    monkeypatch.setattr(embeddings_sqlite, "path_is_local_disk", lambda _p: False)
    conn2 = embeddings_sqlite.connect_corpus_db(db_path)
    try:
        mode = conn2.execute("PRAGMA journal_mode").fetchone()[0]
        assert str(mode).casefold() == "delete"
    finally:
        conn2.close()


def _fake_partition(device: str, mountpoint: str, fstype: str, opts: str):
    return type("Part", (), {"device": device, "mountpoint": mountpoint, "fstype": fstype, "opts": opts})()


def _mock_disk_partitions(monkeypatch, parts):
    import sys
    from types import ModuleType

    mod = ModuleType("psutil")
    mod.disk_partitions = lambda all=True: list(parts)  # noqa: ARG005
    monkeypatch.setitem(sys.modules, "psutil", mod)


def test_fstype_is_network():
    from plugin.contrib.fstype_network import fstype_is_network

    for remote in ("nfs", "cifs", "fuse.sshfs", "9p"):
        assert fstype_is_network(remote) is True
    for local in (None, "", "ext4", "tmpfs", "fuse", "fuse.ntfs-3g"):
        assert fstype_is_network(local) is False


def test_longest_mount_partition_remote_prefix(monkeypatch):
    parts = [
        _fake_partition("", "/", "ext4", "rw"),
        _fake_partition("", "/tmp", "tmpfs", "rw"),
        _fake_partition("", "/mnt/share", "nfs", "rw"),
    ]
    _mock_disk_partitions(monkeypatch, parts)
    assert embeddings_sqlite.path_is_local_disk("/mnt/share/sub/file.txt") is False
    assert embeddings_sqlite.path_is_local_disk("/tmp/scratch") is True
    assert embeddings_sqlite.path_is_local_disk("/home/user") is True

    _mock_disk_partitions(monkeypatch, [_fake_partition("", "/mnt/share", "nfs", "rw")])
    assert embeddings_sqlite.path_is_local_disk("/var/log") is True


def test_windows_unc_and_remote_opts(monkeypatch):
    assert embeddings_sqlite.path_is_local_disk(r"\\server\share\file") is False
    assert embeddings_sqlite.path_is_local_disk("//server/share/file") is False
    monkeypatch.setattr(embeddings_sqlite.sys, "platform", "win32")
    _mock_disk_partitions(
        monkeypatch,
        [_fake_partition("", "Z:\\", "NTFS", "rw,remote")],
    )
    assert embeddings_sqlite.path_is_local_disk("Z:\\data") is False
    _mock_disk_partitions(
        monkeypatch,
        [_fake_partition("", "C:\\", "NTFS", "rw,fixed")],
    )
    assert embeddings_sqlite.path_is_local_disk("C:\\data") is True


def test_path_is_local_disk_real_path_on_linux(tmp_path):
    """Local scratch directory on Linux resolves to local disk via mountinfo inspection."""
    import sys

    if sys.platform != "linux":
        pytest.skip("mountinfo inspection is Linux-specific")
    assert embeddings_sqlite.path_is_local_disk(tmp_path) is True
