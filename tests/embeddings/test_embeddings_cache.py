# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.embeddings.embeddings_cache."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from plugin.embeddings import embeddings_cache
from plugin.embeddings.embeddings_fs import ParagraphChunk, content_hash


def test_folder_corpus_key_stable_and_normalized():
    a = embeddings_cache.folder_corpus_key("/tmp/foo/bar")
    b = embeddings_cache.folder_corpus_key("/tmp/foo/bar/")
    c = embeddings_cache.folder_corpus_key("/tmp/foo/../foo/bar")
    assert a == b == c
    assert len(a) == 64


def test_corpus_db_path_beside_documents(tmp_path):
    listing = tmp_path / "project"
    listing.mkdir()
    path = embeddings_cache.corpus_db_path(str(listing))
    assert path == listing / "writeragent_embeddings" / "corpus.db"
    assert path.parent.is_dir()


def test_ensure_corpus_meta_writes_json(tmp_path):
    meta_path = tmp_path / "corpus_meta.json"
    embeddings_cache.ensure_corpus_meta(meta_path, embedding_model="all-MiniLM-L6-v2", dim=384, chunk_count=10)
    meta = embeddings_cache.read_corpus_meta(meta_path)
    assert meta["schema_version"] == embeddings_cache.SCHEMA_VERSION
    assert meta["embedding_model"] == "all-MiniLM-L6-v2"
    assert meta["dim"] == "384"
    assert meta["chunk_count"] == "10"
    assert meta["storage_backend"] == embeddings_cache.STORAGE_BACKEND


def test_write_corpus_meta_atomic_behavior(tmp_path):
    meta_path = tmp_path / "corpus_meta.json"

    # Write once
    embeddings_cache.write_corpus_meta(meta_path, foo="bar")
    assert meta_path.is_file()
    assert not meta_path.with_suffix(".tmp").exists()

    # Overwrite
    embeddings_cache.write_corpus_meta(meta_path, baz="qux")
    meta = embeddings_cache.read_corpus_meta(meta_path)
    assert meta["foo"] == "bar"
    assert meta["baz"] == "qux"


def test_maybe_upgrade_legacy_index_does_not_wipe_on_corrupt_json(tmp_path):
    listing = str(tmp_path / "project")
    Path(listing).mkdir()
    base = embeddings_cache.folder_cache_dir(listing)
    meta_path = base / "corpus_meta.json"
    db_path = base / "corpus.db"

    db_path.write_text("sqlite", encoding="utf-8")
    meta_path.write_text("{ corrupt json", encoding="utf-8")

    # Call upgrade
    embeddings_cache.maybe_upgrade_legacy_index(listing)

    # It should NOT have cleared the folder cache
    assert db_path.is_file()
    assert meta_path.is_file()

    # Correct format with wrong version WILL wipe
    meta_path.write_text('{"schema_version": "0.1"}', encoding="utf-8")
    embeddings_cache.maybe_upgrade_legacy_index(listing)
    assert not db_path.is_file()
    assert not meta_path.is_file()


def test_index_is_empty_missing_and_populated(tmp_path):
    meta_path = tmp_path / "corpus_meta.json"
    db_path = tmp_path / "corpus.db"
    assert embeddings_cache.index_is_empty(meta_path, db_path) is True

    embeddings_cache.write_corpus_meta(meta_path, chunk_count="0")
    assert embeddings_cache.index_is_empty(meta_path, db_path) is True

    embeddings_cache.write_corpus_meta(meta_path, chunk_count="3")
    db_path.write_text("", encoding="utf-8")
    assert embeddings_cache.index_is_empty(meta_path, db_path) is False


def test_index_is_empty_zvec_ignores_missing_corpus_db(tmp_path):
    """Missing corpus.db is normal for zvec. Emptiness is meta plus the collection."""
    listing = str(tmp_path / "docs")
    meta_path = embeddings_cache.corpus_meta_path(listing)
    db_path = embeddings_cache.corpus_db_path(listing, create_parent=False)
    embeddings_cache.write_corpus_meta(meta_path, chunk_count="3", embedding_model="m")
    store = embeddings_cache.zvec_collection_path(listing, create_parent=False)
    store.mkdir()
    (store / "segment").write_text("rows", encoding="utf-8")

    assert db_path.is_file() is False
    # Sqlite still treats the missing db as empty, even if a zvec dir exists.
    assert embeddings_cache.index_is_empty(meta_path, db_path) is True
    assert embeddings_cache.index_is_empty(meta_path, db_path, search_mode="zvec", listing_root=listing) is False

    embeddings_cache.write_corpus_meta(meta_path, chunk_count="0")
    assert embeddings_cache.index_is_empty(meta_path, db_path, search_mode="zvec", listing_root=listing) is True


def test_index_is_empty_lancedb_needs_collection_and_chunks(tmp_path):
    listing = str(tmp_path / "docs")
    meta_path = embeddings_cache.corpus_meta_path(listing)
    db_path = embeddings_cache.corpus_db_path(listing, create_parent=False)
    embeddings_cache.write_corpus_meta(meta_path, chunk_count="2")
    assert embeddings_cache.index_is_empty(meta_path, db_path, search_mode="lancedb", listing_root=listing) is True

    store = embeddings_cache.lancedb_collection_path(listing, create_parent=False)
    store.mkdir()
    (store / "data.lance").write_text("rows", encoding="utf-8")
    assert embeddings_cache.index_is_empty(meta_path, db_path, search_mode="lancedb", listing_root=listing) is False

    meta_path.write_text("{ corrupt", encoding="utf-8")
    assert embeddings_cache.index_is_empty(meta_path, db_path, search_mode="lancedb", listing_root=listing) is False


def test_index_is_empty_with_corrupt_meta_and_db(tmp_path):
    meta_path = tmp_path / "corpus_meta.json"
    db_path = tmp_path / "corpus.db"

    db_path.write_text("sqlite", encoding="utf-8")
    meta_path.write_text("{ corrupt", encoding="utf-8")

    # Even though read_corpus_meta would return {} and chunk_count=0,
    # index_is_empty must explicitly return False because DB exists
    assert embeddings_cache.index_is_empty(meta_path, db_path) is False


def test_needs_cold_rebuild_with_corrupt_meta(tmp_path):
    meta_path = tmp_path / "corpus_meta.json"
    meta_path.write_text("{ corrupt", encoding="utf-8")

    # Should not trigger a wipe
    assert embeddings_cache.needs_cold_rebuild(meta_path, "all-MiniLM-L6-v2") is False


def test_needs_cold_rebuild_on_embedding_model_change(tmp_path):
    meta_path = tmp_path / "corpus_meta.json"
    embeddings_cache.write_corpus_meta(
        meta_path,
        schema_version=embeddings_cache.SCHEMA_VERSION,
        embedding_model="model-a",
        chunk_count="4",
        dim="384",
    )
    assert embeddings_cache.needs_cold_rebuild(meta_path, "model-a") is False
    assert embeddings_cache.needs_cold_rebuild(meta_path, "model-b") is True
    assert embeddings_cache.query_blocked_for_model(meta_path, "model-b") is True
    assert embeddings_cache.query_blocked_for_model(meta_path, "model-a") is False


def test_query_blocked_missing_meta_is_not_a_model_mismatch(tmp_path):
    meta_path = tmp_path / "missing.json"
    assert embeddings_cache.needs_cold_rebuild(meta_path, "model-a") is True
    assert embeddings_cache.query_blocked_for_model(meta_path, "model-a") is False


def test_non_dict_corpus_meta_does_not_look_empty_or_cold(tmp_path):
    meta_path = tmp_path / "corpus_meta.json"
    db_path = tmp_path / "corpus.db"
    db_path.write_text("sqlite", encoding="utf-8")
    meta_path.write_text('["not", "a", "dict"]', encoding="utf-8")

    assert embeddings_cache.read_corpus_meta(meta_path) == {}
    assert embeddings_cache.index_is_empty(meta_path, db_path) is False
    assert embeddings_cache.needs_cold_rebuild(meta_path, "model-a") is False
    assert embeddings_cache.query_blocked_for_model(meta_path, "model-a") is False

    listing = str(tmp_path / "project")
    Path(listing).mkdir()
    base = embeddings_cache.folder_cache_dir(listing)
    live_db = base / "corpus.db"
    live_meta = base / "corpus_meta.json"
    live_db.write_text("sqlite", encoding="utf-8")
    live_meta.write_text("[]", encoding="utf-8")
    embeddings_cache.maybe_upgrade_legacy_index(listing)
    assert live_db.is_file()
    assert live_meta.is_file()


def test_resolve_index_context_no_listing_root():
    ctx = MagicMock()
    model = MagicMock()
    with patch("plugin.embeddings.embeddings_cache.resolve_folder_for_active_doc", return_value=None):
        key, db_path, meta, err = embeddings_cache.resolve_index_context(ctx, model)
    assert key is None
    assert db_path is None
    assert meta is None
    assert "Save the document" in err


def test_resolve_index_context_ok(tmp_path):
    ctx = MagicMock()
    model = MagicMock()
    listing = str(tmp_path / "project")
    Path(listing).mkdir()
    with patch("plugin.embeddings.embeddings_cache.resolve_folder_for_active_doc", return_value=listing):
        key, db_path, meta, root = embeddings_cache.resolve_index_context(ctx, model)
    assert root == listing
    assert key == embeddings_cache.folder_corpus_key(listing)
    assert db_path == Path(listing) / "writeragent_embeddings" / "corpus.db"
    assert meta == Path(listing) / "writeragent_embeddings" / "corpus_meta.json"


def test_resolve_index_context_untitled_uses_work_directory(tmp_path):
    ctx = MagicMock()
    model = MagicMock()
    my_docs = str(tmp_path / "Documents")
    Path(my_docs).mkdir()
    with patch("plugin.doc.text_helpers.get_document_path", return_value=None):
        with patch("plugin.doc.document_research.get_work_directory", return_value=my_docs):
            key, db_path, meta, root = embeddings_cache.resolve_index_context(ctx, model)
    assert root == my_docs
    assert key == embeddings_cache.folder_corpus_key(my_docs)
    assert db_path == Path(my_docs) / "writeragent_embeddings" / "corpus.db"
    assert meta == Path(my_docs) / "writeragent_embeddings" / "corpus_meta.json"


def test_model_matches_index(tmp_path):
    meta_path = tmp_path / "corpus_meta.json"
    # When stored model is specified
    embeddings_cache.write_corpus_meta(meta_path, embedding_model="model-a")
    assert embeddings_cache.model_matches_index(meta_path, "model-a") is True
    assert embeddings_cache.model_matches_index(meta_path, "model-b") is False

    # When stored model is empty
    embeddings_cache.write_corpus_meta(meta_path, embedding_model="")
    assert embeddings_cache.model_matches_index(meta_path, "model-a") is False
    assert embeddings_cache.model_matches_index(meta_path, "") is True


def test_remove_stale_corpus_stores_db(tmp_path):
    listing = str(tmp_path / "project")
    Path(listing).mkdir()
    base = embeddings_cache.folder_cache_dir(listing)
    legacy = base / "index.db"
    legacy.write_text("sqlite", encoding="utf-8")
    assert embeddings_cache.remove_stale_corpus_stores(listing) is True
    assert not legacy.is_file()


def test_file_index_state_and_diff(tmp_path):
    from plugin.embeddings.venv.embeddings_sqlite import connect_corpus_db, ensure_schema, upsert_chunk_with_vector

    db_path = tmp_path / "corpus.db"
    chunk = ParagraphChunk(
        doc_url="file:///a.odt",
        para_index=0,
        char_start=0,
        char_end=3,
        text="new",
        content_hash=content_hash("new"),
        file_mtime=1.0,
    )
    stale = ParagraphChunk(
        doc_url="file:///a.odt",
        para_index=2,
        char_start=0,
        char_end=4,
        text="gone",
        content_hash=content_hash("gone"),
        file_mtime=1.0,
    )
    conn = connect_corpus_db(db_path)
    try:
        ensure_schema(conn, with_fts=False, with_vec=False)
        upsert_chunk_with_vector(
            conn,
            {
                "doc_url": stale.doc_url,
                "para_index": stale.para_index,
                "char_start": stale.char_start,
                "char_end": stale.char_end,
                "content_hash": stale.content_hash,
                "text": stale.text,
                "file_mtime": stale.file_mtime,
            },
            [],
            model="",
            with_fts=False,
            with_vec=False,
        )
        conn.commit()
    finally:
        conn.close()

    to_index, to_delete = embeddings_cache.diff_chunk_rows(db_path, 'file:///a.odt', [chunk])
    assert len(to_index) == 1
    assert to_delete == [
        {
            "doc_url": "file:///a.odt",
            "para_index": 2,
            "char_start": 0,
            "char_end": 4,
        }
    ]
