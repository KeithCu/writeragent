# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.embeddings.embeddings_cache."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

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
    embeddings_cache.CorpusMeta.ensure(meta_path, embedding_model="all-MiniLM-L6-v2", dim=384, chunk_count=10)
    meta = embeddings_cache.CorpusMeta.load(meta_path).data
    assert meta["schema_version"] == embeddings_cache.SCHEMA_VERSION
    assert meta["embedding_model"] == "all-MiniLM-L6-v2"
    assert meta["dim"] == "384"
    assert meta["chunk_count"] == "10"
    assert meta["storage_backend"] == embeddings_cache.STORAGE_BACKEND


def test_write_row_count_meta(tmp_path):
    listing = tmp_path / "project"
    listing.mkdir()
    embeddings_cache.CorpusMeta.write_row_count(str(listing), 42, embedding_model="all-MiniLM-L6-v2", dim=384)
    meta_path = embeddings_cache.corpus_meta_path(str(listing))
    meta = embeddings_cache.CorpusMeta.load(meta_path).data
    assert meta["schema_version"] == embeddings_cache.SCHEMA_VERSION
    assert meta["storage_backend"] == "sqlite_vec"
    assert meta["row_count"] == "42"
    assert meta["chunk_count"] == "42"
    assert meta["embedding_model"] == "all-MiniLM-L6-v2"
    assert meta["dim"] == "384"
    assert "updated_at" in meta


def test_write_corpus_meta_atomic_behavior(tmp_path):
    meta_path = tmp_path / "corpus_meta.json"

    # Write once
    embeddings_cache.CorpusMeta.load(meta_path).update(foo="bar").save(meta_path)
    assert meta_path.is_file()
    assert not meta_path.with_suffix(".tmp").exists()

    # Overwrite without re-reading destination disk file
    meta_obj = embeddings_cache.CorpusMeta.load(meta_path).update(baz="qux")
    with patch.object(Path, "read_text", side_effect=AssertionError("save re-read disk")), \
         patch("json.loads", side_effect=AssertionError("save called json.loads")):
        meta_obj.save(meta_path)

    reloaded = embeddings_cache.CorpusMeta.load(meta_path)
    assert reloaded.data["foo"] == "bar"
    assert reloaded.data["baz"] == "qux"


def test_write_corpus_meta_does_not_clobber_fixed_tmp_name(tmp_path):
    meta_path = tmp_path / "corpus_meta.json"
    fixed_tmp = meta_path.with_suffix(".tmp")
    fixed_tmp.write_text("PARTIAL", encoding="utf-8")

    embeddings_cache.CorpusMeta.load(meta_path).update(foo="bar").save(meta_path)

    assert embeddings_cache.CorpusMeta.load(meta_path).data["foo"] == "bar"
    assert fixed_tmp.read_text(encoding="utf-8") == "PARTIAL"
    assert set(tmp_path.iterdir()) == {meta_path, fixed_tmp}


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


def test_clear_folder_cache_removes_wal_sidecars(tmp_path):
    listing = str(tmp_path / "project")
    Path(listing).mkdir()
    base = embeddings_cache.folder_cache_dir(listing)
    db = base / "corpus.db"
    wal = base / "corpus.db-wal"
    shm = base / "corpus.db-shm"
    db.write_text("sqlite", encoding="utf-8")
    wal.write_text("wal", encoding="utf-8")
    shm.write_text("shm", encoding="utf-8")

    embeddings_cache.clear_folder_cache(listing)

    assert not db.exists()
    assert not wal.exists()
    assert not shm.exists()


def test_clear_folder_cache_removes_vector_stores_and_evicts_only_this_zvec_handle(tmp_path, monkeypatch):
    from plugin.embeddings.venv import embeddings_zvec

    listing = str(tmp_path / "project")
    Path(listing).mkdir()
    zdir = embeddings_cache.zvec_collection_path(listing)
    ldir = embeddings_cache.lancedb_collection_path(listing)
    zdir.mkdir()
    (zdir / "segment").write_text("z", encoding="utf-8")
    (ldir / "data").write_text("l", encoding="utf-8")

    other_key = str(tmp_path / "other" / "zvec")
    monkeypatch.setitem(embeddings_zvec._COLL_CACHE, str(zdir), object())
    monkeypatch.setitem(embeddings_zvec._COLL_CACHE, other_key, object())

    embeddings_cache.clear_folder_cache(listing)

    assert not zdir.exists()
    assert not ldir.exists()
    assert str(zdir) not in embeddings_zvec._COLL_CACHE
    assert other_key in embeddings_zvec._COLL_CACHE


def test_index_is_empty_missing_and_populated(tmp_path):
    meta_path = tmp_path / "corpus_meta.json"
    db_path = tmp_path / "corpus.db"
    assert embeddings_cache.index_is_empty(meta_path, db_path) is True

    embeddings_cache.CorpusMeta.load(meta_path).update(chunk_count="0").save(meta_path)
    assert embeddings_cache.index_is_empty(meta_path, db_path) is True

    embeddings_cache.CorpusMeta.load(meta_path).update(chunk_count="3").save(meta_path)
    db_path.write_text("", encoding="utf-8")
    assert embeddings_cache.index_is_empty(meta_path, db_path) is False


def test_index_is_empty_zvec_ignores_missing_corpus_db(tmp_path):
    """Missing corpus.db is normal for zvec. Emptiness is meta plus the collection."""
    listing = str(tmp_path / "docs")
    meta_path = embeddings_cache.corpus_meta_path(listing)
    db_path = embeddings_cache.corpus_db_path(listing, create_parent=False)
    embeddings_cache.CorpusMeta.load(meta_path).update(chunk_count="3", embedding_model="m").save(meta_path)
    store = embeddings_cache.zvec_collection_path(listing, create_parent=False)
    store.mkdir()
    (store / "segment").write_text("rows", encoding="utf-8")

    assert db_path.is_file() is False
    # Sqlite still treats the missing db as empty, even if a zvec dir exists.
    assert embeddings_cache.index_is_empty(meta_path, db_path) is True
    assert embeddings_cache.index_is_empty(meta_path, db_path, search_mode="zvec", listing_root=listing) is False

    embeddings_cache.CorpusMeta.load(meta_path).update(chunk_count="0").save(meta_path)
    assert embeddings_cache.index_is_empty(meta_path, db_path, search_mode="zvec", listing_root=listing) is True


def test_index_is_empty_lancedb_needs_collection_and_chunks(tmp_path):
    listing = str(tmp_path / "docs")
    meta_path = embeddings_cache.corpus_meta_path(listing)
    db_path = embeddings_cache.corpus_db_path(listing, create_parent=False)
    embeddings_cache.CorpusMeta.load(meta_path).update(chunk_count="2").save(meta_path)
    assert embeddings_cache.index_is_empty(meta_path, db_path, search_mode="lancedb", listing_root=listing) is True

    store = embeddings_cache.lancedb_collection_path(listing, create_parent=False)
    store.mkdir()
    (store / "data.lance").write_text("rows", encoding="utf-8")
    assert embeddings_cache.index_is_empty(meta_path, db_path, search_mode="lancedb", listing_root=listing) is False

    meta_path.write_text("{ corrupt", encoding="utf-8")
    assert embeddings_cache.index_is_empty(meta_path, db_path, search_mode="lancedb", listing_root=listing) is False


def test_index_is_empty_missing_meta_with_live_db_is_empty(tmp_path):
    meta_path = tmp_path / "corpus_meta.json"
    db_path = tmp_path / "corpus.db"
    db_path.write_text("sqlite", encoding="utf-8")

    assert embeddings_cache.index_is_empty(meta_path, db_path) is True


def test_index_is_empty_with_corrupt_meta_and_db(tmp_path):
    meta_path = tmp_path / "corpus_meta.json"
    db_path = tmp_path / "corpus.db"

    db_path.write_text("sqlite", encoding="utf-8")
    meta_path.write_text("{ corrupt", encoding="utf-8")

    # Even though read_corpus_meta would return {} and chunk_count=0,
    # index_is_empty must explicitly return False because DB exists
    assert embeddings_cache.index_is_empty(meta_path, db_path) is False


def test_index_is_empty_omitted_db_path_uses_sibling_corpus(tmp_path):
    meta_path = tmp_path / "corpus_meta.json"
    db_path = tmp_path / "corpus.db"
    db_path.write_text("sqlite", encoding="utf-8")
    meta_path.write_text("{ corrupt", encoding="utf-8")

    assert embeddings_cache.index_is_empty(meta_path) is False

    meta_path.write_text("[]", encoding="utf-8")
    assert embeddings_cache.index_is_empty(meta_path) is False


def test_index_is_empty_omitted_db_path_without_sibling_db_is_empty(tmp_path):
    meta_path = tmp_path / "corpus_meta.json"
    meta_path.write_text("{ corrupt", encoding="utf-8")
    assert embeddings_cache.index_is_empty(meta_path) is True


def test_index_is_empty_omitted_db_path_missing_meta_with_sibling_db_is_empty(tmp_path):
    meta_path = tmp_path / "corpus_meta.json"
    db_path = tmp_path / "corpus.db"
    db_path.write_text("sqlite", encoding="utf-8")
    assert embeddings_cache.index_is_empty(meta_path) is True


def test_needs_cold_rebuild_with_corrupt_meta(tmp_path):
    meta_path = tmp_path / "corpus_meta.json"
    meta_path.write_text("{ corrupt", encoding="utf-8")

    # Should not trigger a wipe
    assert embeddings_cache.CorpusMeta.load(meta_path).needs_cold_rebuild("all-MiniLM-L6-v2") is False


def test_needs_cold_rebuild_on_embedding_model_change(tmp_path):
    meta_path = tmp_path / "corpus_meta.json"
    embeddings_cache.CorpusMeta.load(meta_path).update(
        schema_version=embeddings_cache.SCHEMA_VERSION,
        embedding_model="model-a",
        chunk_count="4",
        dim="384",
    ).save(meta_path)
    meta = embeddings_cache.CorpusMeta.load(meta_path)
    assert meta.needs_cold_rebuild("model-a") is False
    assert meta.needs_cold_rebuild("model-b") is True
    assert meta.query_blocked_for_model("model-b") is True
    assert meta.query_blocked_for_model("model-a") is False


def test_query_blocked_missing_meta_is_not_a_model_mismatch(tmp_path):
    meta_path = tmp_path / "missing.json"
    meta = embeddings_cache.CorpusMeta.load(meta_path)
    assert meta.needs_cold_rebuild("model-a") is True
    assert meta.query_blocked_for_model("model-a") is False


def test_non_dict_corpus_meta_does_not_look_empty_or_cold(tmp_path):
    meta_path = tmp_path / "corpus_meta.json"
    db_path = tmp_path / "corpus.db"
    db_path.write_text("sqlite", encoding="utf-8")
    meta_path.write_text('["not", "a", "dict"]', encoding="utf-8")

    meta = embeddings_cache.CorpusMeta.load(meta_path)
    assert meta.data == {}
    assert embeddings_cache.index_is_empty(meta_path, db_path) is False
    assert meta.needs_cold_rebuild("model-a") is False
    assert meta.query_blocked_for_model("model-a") is False

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
    assert isinstance(err, str) and err


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
    assert not (Path(listing) / "writeragent_embeddings").exists()


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
    assert not (Path(my_docs) / "writeragent_embeddings").exists()


def test_model_matches_index(tmp_path):
    meta_path = tmp_path / "corpus_meta.json"
    # When stored model is specified
    embeddings_cache.CorpusMeta.load(meta_path).update(embedding_model="model-a").save(meta_path)
    assert embeddings_cache.CorpusMeta.load(meta_path).model_matches_index("model-a") is True
    assert embeddings_cache.CorpusMeta.load(meta_path).model_matches_index("model-b") is False

    # When stored model is empty
    embeddings_cache.CorpusMeta.load(meta_path).update(embedding_model="").save(meta_path)
    assert embeddings_cache.CorpusMeta.load(meta_path).model_matches_index("model-a") is False
    assert embeddings_cache.CorpusMeta.load(meta_path).model_matches_index("") is True


def test_remove_stale_corpus_stores_db(tmp_path):
    listing = str(tmp_path / "project")
    Path(listing).mkdir()
    base = embeddings_cache.folder_cache_dir(listing)
    legacy = base / "index.db"
    legacy.write_text("sqlite", encoding="utf-8")
    assert embeddings_cache.remove_stale_corpus_stores(listing) is True
    assert not legacy.is_file()


def test_remove_path_directory_failure_returns_false(tmp_path):
    target_dir = tmp_path / "subdir"
    target_dir.mkdir()

    with patch("plugin.embeddings.embeddings_cache.shutil.rmtree", side_effect=OSError("rmtree error")):
        assert embeddings_cache._remove_path(target_dir) is False
    assert target_dir.exists()


def test_remove_path_directory_success_returns_true(tmp_path):
    target_dir = tmp_path / "subdir"
    target_dir.mkdir()

    assert embeddings_cache._remove_path(target_dir) is True
    assert not target_dir.exists()


def test_file_index_state_and_diff(tmp_path):
    from plugin.embeddings.venv.embeddings_sqlite import connect_corpus_db, diff_chunk_rows_in_db, ensure_schema, upsert_chunk_with_vector

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

    conn = connect_corpus_db(db_path)
    try:
        to_index, to_delete = diff_chunk_rows_in_db(conn, "file:///a.odt", [chunk])
    finally:
        conn.close()
    assert len(to_index) == 1
    assert to_delete == [
        {
            "doc_url": "file:///a.odt",
            "para_index": 2,
            "char_start": 0,
            "char_end": 4,
        }
    ]


def test_dim_from_meta_path_ignores_non_dict(tmp_path):
    meta = tmp_path / "corpus_meta.json"
    meta.write_text("[]", encoding="utf-8")
    m = embeddings_cache.CorpusMeta.load(str(meta))
    assert (m.dim if (m.is_valid and m.dim > 0) else None) is None
    meta.write_text('{"dim": "384"}', encoding="utf-8")
    m = embeddings_cache.CorpusMeta.load(str(meta))
    assert (m.dim if (m.is_valid and m.dim > 0) else None) == 384


def test_corpus_meta_load_update_save_and_predicates(tmp_path):
    meta_path = tmp_path / "corpus_meta.json"
    meta = embeddings_cache.CorpusMeta.load(meta_path)
    assert not meta.is_valid
    assert meta.chunk_count == 0

    meta.update(
        schema_version="6",
        embedding_model="model-x",
        dim=256,
        chunk_count=10,
        storage_backend="sqlite_vec",
        host_flag="yes",
    ).save(meta_path)

    loaded = embeddings_cache.CorpusMeta.load(meta_path)
    assert loaded.is_valid
    assert loaded.schema_version == "6"
    assert loaded.embedding_model == "model-x"
    assert loaded.dim == 256
    assert loaded.chunk_count == 10
    assert loaded.storage_backend == "sqlite_vec"
    assert loaded.data.get("host_flag") == "yes"

    assert loaded.schema_matches()
    assert loaded.model_matches_index("model-x")
    assert not loaded.model_matches_index("other-model")
    assert not loaded.needs_cold_rebuild("model-x")
    assert loaded.needs_cold_rebuild("other-model")

    # Second update preserves host_flag
    loaded.update(chunk_count=20).save()
    reloaded = embeddings_cache.CorpusMeta.load(meta_path)
    assert reloaded.chunk_count == 20
    assert reloaded.data.get("host_flag") == "yes"
    assert reloaded.embedding_model == "model-x"


def test_corpus_meta_corrupt_file_handling(tmp_path):
    meta_path = tmp_path / "corpus_meta.json"
    meta_path.write_text("{ corrupt", encoding="utf-8")
    meta = embeddings_cache.CorpusMeta.load(meta_path)
    assert not meta.is_valid
    assert not meta.schema_matches()
    # Corrupt meta should not trigger cold rebuild (which would wipe live DB)
    assert not meta.needs_cold_rebuild("any-model")


def test_folder_store_zvec_lists_collection_once(tmp_path):
    from unittest.mock import patch

    listing = str(tmp_path / "docs")
    meta_path = embeddings_cache.corpus_meta_path(listing)
    db_path = embeddings_cache.corpus_db_path(listing, create_parent=False)
    embeddings_cache.CorpusMeta.load(meta_path).update(chunk_count="3", embedding_model="m").save(meta_path)
    store = embeddings_cache.zvec_collection_path(listing, create_parent=False)
    store.mkdir()
    (store / "segment").write_text("rows", encoding="utf-8")

    with patch.object(embeddings_cache, "zvec_collection_looks_populated", wraps=embeddings_cache.zvec_collection_looks_populated) as probe:
        folder = embeddings_cache.folder_store(listing, meta_path, db_path, mode="zvec")
    probe.assert_called_once()
    assert folder.is_empty is False
    assert folder.search_path is not None


def test_folder_store_empty_zvec_does_not_create_parent(tmp_path):
    listing = str(tmp_path / "docs")
    meta_path = embeddings_cache.corpus_meta_path(listing, create_parent=False)
    db_path = embeddings_cache.corpus_db_path(listing, create_parent=False)
    cache_dir = tmp_path / "docs" / "writeragent_embeddings"

    store = embeddings_cache.folder_store(listing, meta_path, db_path, mode="zvec")
    assert store.is_empty is True
    assert store.search_path is None
    assert not cache_dir.exists()


@pytest.mark.parametrize(
    ("mode", "listing_root", "expected", "create_parent_calls"),
    [
        ("zvec", None, None, []),
        ("zvec", "/root", "/ready", [True]),
        ("lancedb", None, None, []),
        ("lancedb", "/root", "/ready", [True]),
        ("hybrid", "/root", "/db", []),
    ],
)
def test_search_path_after_populated_probe(mode, listing_root, expected, create_parent_calls):
    """Path selection once index_is_empty is false (no second collection probe)."""
    path_name = "zvec_collection_path" if mode == "zvec" else "lancedb_collection_path"

    def _path(root, *, create_parent):
        del root
        return Path("/ready") if create_parent else Path("/probe")

    with patch(f"plugin.embeddings.embeddings_cache.{path_name}", side_effect=_path) as path:
        got = embeddings_cache._search_path(mode, listing_root, Path("/db"))

    if expected is None:
        assert got is None
    else:
        assert got is not None
        assert Path(got) == Path(expected)
    assert [call.kwargs.get("create_parent") for call in path.call_args_list] == create_parent_calls


def test_folder_store_zvec_without_listing_root_has_no_search_path(tmp_path):
    from unittest.mock import patch

    meta_path = tmp_path / "corpus_meta.json"
    db_path = tmp_path / "corpus.db"
    with patch.object(embeddings_cache, "index_is_empty", return_value=False):
        store = embeddings_cache.folder_store(None, meta_path, db_path, mode="zvec")
    assert store.search_path is None


def test_folder_store_lancedb_lists_collection_once(tmp_path):
    from unittest.mock import patch

    listing = str(tmp_path / "docs")
    meta_path = embeddings_cache.corpus_meta_path(listing)
    db_path = embeddings_cache.corpus_db_path(listing, create_parent=False)
    embeddings_cache.CorpusMeta.load(meta_path).update(chunk_count="2", embedding_model="m").save(meta_path)
    store = embeddings_cache.lancedb_collection_path(listing, create_parent=False)
    store.mkdir()
    (store / "data.lance").write_text("rows", encoding="utf-8")

    with patch.object(
        embeddings_cache,
        "lancedb_collection_looks_populated",
        wraps=embeddings_cache.lancedb_collection_looks_populated,
    ) as probe:
        folder = embeddings_cache.folder_store(listing, meta_path, db_path, mode="lancedb")
    probe.assert_called_once()
    assert folder.is_empty is False
    assert folder.search_path is not None


def test_resolve_index_context_does_not_delete_stale_schema(tmp_path):
    listing = str(tmp_path / "folder")
    cache_dir = Path(listing) / embeddings_cache.EMBEDDINGS_CACHE_DIRNAME
    cache_dir.mkdir(parents=True)
    meta_path = cache_dir / embeddings_cache.CORPUS_META_FILENAME
    meta_path.write_text('{"schema_version": "0"}', encoding="utf-8")
    db_path = cache_dir / embeddings_cache.CORPUS_DB_FILENAME
    db_path.write_text("data", encoding="utf-8")
    zvec_dir = cache_dir / "zvec"
    zvec_dir.mkdir()
    (zvec_dir / "entry").write_text("data", encoding="utf-8")

    with patch("plugin.embeddings.embeddings_cache.shutil.rmtree") as rmtree_mock:
        folder_key, resolved_db, resolved_meta, resolved_listing = embeddings_cache.resolve_index_context(listing_root=listing)

    rmtree_mock.assert_not_called()
    assert zvec_dir.exists()
    assert db_path.exists()
    assert meta_path.exists()
    assert folder_key == embeddings_cache.folder_corpus_key(listing)
    assert resolved_db == db_path
    assert resolved_meta == meta_path
    assert resolved_listing == listing


def test_folder_store_foreign_schema_is_empty_without_delete(tmp_path):
    listing = str(tmp_path / "folder")
    cache_dir = Path(listing) / embeddings_cache.EMBEDDINGS_CACHE_DIRNAME
    cache_dir.mkdir(parents=True)
    meta_path = cache_dir / embeddings_cache.CORPUS_META_FILENAME
    meta_path.write_text('{"schema_version": "5", "chunk_count": "3"}', encoding="utf-8")
    db_path = cache_dir / embeddings_cache.CORPUS_DB_FILENAME
    db_path.write_text("sqlite", encoding="utf-8")

    store = embeddings_cache.folder_store(listing, meta_path, db_path, mode="fts")
    assert store.is_empty is True
    assert store.search_path is None
    assert db_path.exists()
    assert meta_path.exists()

    meta_path.write_text(f'{{"schema_version": "{embeddings_cache.SCHEMA_VERSION}", "chunk_count": "3"}}', encoding="utf-8")
    store_matching = embeddings_cache.folder_store(listing, meta_path, db_path, mode="fts")
    assert store_matching.is_empty is False
    assert store_matching.search_path == str(db_path)


def test_corpus_meta_has_foreign_schema(tmp_path):
    missing_path = tmp_path / "missing.json"
    assert embeddings_cache.CorpusMeta.load(missing_path).has_foreign_schema is False

    corrupt_path = tmp_path / "corrupt.json"
    corrupt_path.write_text("not json", encoding="utf-8")
    assert embeddings_cache.CorpusMeta.load(corrupt_path).has_foreign_schema is False

    empty_ver_path = tmp_path / "empty_ver.json"
    empty_ver_path.write_text('{"schema_version": ""}', encoding="utf-8")
    assert embeddings_cache.CorpusMeta.load(empty_ver_path).has_foreign_schema is False

    current_path = tmp_path / "current.json"
    current_path.write_text(f'{{"schema_version": "{embeddings_cache.SCHEMA_VERSION}"}}', encoding="utf-8")
    assert embeddings_cache.CorpusMeta.load(current_path).has_foreign_schema is False

    foreign_path = tmp_path / "foreign.json"
    foreign_path.write_text('{"schema_version": "0"}', encoding="utf-8")
    assert embeddings_cache.CorpusMeta.load(foreign_path).has_foreign_schema is True


def test_clear_folder_cache_removes_meta_last_hiding_store(tmp_path):
    listing = str(tmp_path / "folder")
    cache_dir = Path(listing) / embeddings_cache.EMBEDDINGS_CACHE_DIRNAME
    cache_dir.mkdir(parents=True)
    meta_path = cache_dir / embeddings_cache.CORPUS_META_FILENAME
    meta_path.write_text('{"schema_version": "0", "chunk_count": "5"}', encoding="utf-8")
    db_path = cache_dir / embeddings_cache.CORPUS_DB_FILENAME
    db_path.write_text("data", encoding="utf-8")
    zvec_dir = cache_dir / "zvec"
    zvec_dir.mkdir()
    (zvec_dir / "entry").write_text("vector_data", encoding="utf-8")

    zvec_checked = False
    real_remove_path = embeddings_cache._remove_path

    def spy_remove_path(path: Path) -> bool:
        nonlocal zvec_checked
        if path == zvec_dir:
            # While zvec/ is being removed, corpus_meta.json must still be present with foreign
            # schema, ensuring folder_store continues to hide the store (is_empty is True).
            store = embeddings_cache.folder_store(listing, meta_path, db_path, mode="zvec")
            assert store.is_empty is True
            assert store.search_path is None
            assert meta_path.exists()
            zvec_checked = True
        return real_remove_path(path)

    with patch.object(embeddings_cache, "_remove_path", side_effect=spy_remove_path):
        embeddings_cache.clear_folder_cache(listing)

    assert zvec_checked is True
    assert not zvec_dir.exists()
    assert not meta_path.exists()
    assert not db_path.exists()


def test_folder_cache_lock_held_during_clear_and_upgrade(tmp_path):
    import threading

    listing = str(tmp_path / "folder")
    cache_dir = Path(listing) / embeddings_cache.EMBEDDINGS_CACHE_DIRNAME
    cache_dir.mkdir(parents=True)
    meta_path = cache_dir / embeddings_cache.CORPUS_META_FILENAME
    meta_path.write_text('{"schema_version": "0"}', encoding="utf-8")

    locked_during_clear = False
    locked_during_upgrade = False

    def probe_lock() -> bool:
        # Try to acquire lock from another thread; must fail if held.
        acquired: list[bool] = []

        def _try() -> None:
            got = embeddings_cache._folder_cache_lock.acquire(blocking=False)
            if got:
                embeddings_cache._folder_cache_lock.release()
            acquired.append(got)

        t = threading.Thread(target=_try)
        t.start()
        t.join()
        return not acquired[0]

    real_remove = embeddings_cache._remove_path

    def spy_remove_clear(path: Path) -> bool:
        nonlocal locked_during_clear
        if probe_lock():
            locked_during_clear = True
        return real_remove(path)

    with patch.object(embeddings_cache, "_remove_path", side_effect=spy_remove_clear):
        embeddings_cache.clear_folder_cache(listing)

    assert locked_during_clear is True

    # Re-create meta to test maybe_upgrade_legacy_index
    meta_path.write_text('{"schema_version": "0"}', encoding="utf-8")

    real_load = embeddings_cache._load_meta_object

    def spy_load_upgrade(path: Path):
        nonlocal locked_during_upgrade
        if probe_lock():
            locked_during_upgrade = True
        return real_load(path)

    with patch.object(embeddings_cache, "_load_meta_object", side_effect=spy_load_upgrade):
        embeddings_cache.maybe_upgrade_legacy_index(listing)

    assert locked_during_upgrade is True


