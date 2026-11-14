# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.embeddings.embeddings_indexer."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from plugin.embeddings import embeddings_indexer
from plugin.embeddings.embeddings_fs import ParagraphChunk, content_hash
from plugin.embeddings.venv import embeddings_sqlite


def test_file_is_stale_when_no_rows(tmp_path):
    db_path = tmp_path / "corpus.db"
    conn = embeddings_sqlite.connect_corpus_db(db_path)
    try:
        embeddings_sqlite.ensure_schema(conn, with_fts=False, with_vec=False)
        assert embeddings_sqlite.file_is_stale_in_db(conn, "file:///a.odt", 100.0) is True
    finally:
        conn.close()


def test_file_is_stale_when_mtime_newer(tmp_path):
    from plugin.embeddings.venv.embeddings_sqlite import connect_corpus_db, ensure_schema, upsert_chunk_with_vector

    db_path = tmp_path / "corpus.db"
    conn = connect_corpus_db(db_path)
    try:
        ensure_schema(conn, with_fts=False, with_vec=False)
        upsert_chunk_with_vector(
            conn,
            {
                "doc_url": "file:///a.odt",
                "para_index": 0,
                "char_start": 0,
                "char_end": 1,
                "content_hash": content_hash("h"),
                "text": "h",
                "file_mtime": 50.0,
            },
            [],
            model="",
            with_fts=False,
            with_vec=False,
        )
        conn.commit()
        embeddings_sqlite.mark_file_indexed_in_db(conn, "file:///a.odt", 50.0, indexed_at=50.0)
        assert embeddings_sqlite.file_is_stale_in_db(conn, "file:///a.odt", 100.0) is True
        assert embeddings_sqlite.file_is_stale_in_db(conn, "file:///a.odt", 40.0) is False
    finally:
        conn.close()


def test_file_is_stale_uses_stored_file_mtime_not_wall_clock(tmp_path):
    """Staleness follows the stored file mtime, not wall-clock last_indexed_at."""
    from plugin.embeddings.venv.embeddings_sqlite import connect_corpus_db, ensure_schema, upsert_chunk_with_vector

    db_path = tmp_path / "corpus.db"
    conn = connect_corpus_db(db_path)
    try:
        ensure_schema(conn, with_fts=False, with_vec=False)
        upsert_chunk_with_vector(
            conn,
            {
                "doc_url": "file:///a.odt",
                "para_index": 0,
                "char_start": 0,
                "char_end": 1,
                "content_hash": content_hash("h"),
                "text": "h",
                "file_mtime": 50.0,
            },
            [],
            model="",
            with_fts=False,
            with_vec=False,
        )
        conn.commit()
        embeddings_sqlite.mark_file_indexed_in_db(conn, "file:///a.odt", 50.0, indexed_at=1_000_000.0)
        assert embeddings_sqlite.file_is_stale_in_db(conn, "file:///a.odt", 80.0) is True
        assert embeddings_sqlite.file_is_stale_in_db(conn, "file:///a.odt", 50.0) is False
        embeddings_sqlite.mark_file_indexed_in_db(conn, "file:///a.odt", 200.0, indexed_at=10.0)
        assert embeddings_sqlite.file_is_stale_in_db(conn, "file:///a.odt", 200.0) is False
    finally:
        conn.close()


def test_diff_chunk_rows_detects_change_and_delete(tmp_path):
    from plugin.embeddings.venv.embeddings_sqlite import connect_corpus_db, ensure_schema, upsert_chunk_with_vector

    db_path = tmp_path / "corpus.db"
    conn = connect_corpus_db(db_path)
    try:
        ensure_schema(conn, with_fts=False, with_vec=False)
        for para_index, text in ((0, "old"), (2, "gone")):
            upsert_chunk_with_vector(
                conn,
                {
                    "doc_url": "file:///a.odt",
                    "para_index": para_index,
                    "char_start": 0,
                    "char_end": len(text),
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

    chunks = [
        ParagraphChunk(
            doc_url="file:///a.odt",
            para_index=0,
            char_start=0,
            char_end=3,
            text="new",
            content_hash=content_hash("new"),
            file_mtime=1.0,
        ),
        ParagraphChunk(
            doc_url="file:///a.odt",
            para_index=1,
            char_start=0,
            char_end=5,
            text="added",
            content_hash=content_hash("added"),
            file_mtime=1.0,
        ),
    ]
    conn = connect_corpus_db(db_path)
    try:
        to_index, to_delete = embeddings_sqlite.diff_chunk_rows_in_db(conn, "file:///a.odt", chunks)
    finally:
        conn.close()
    assert len(to_index) == 2
    assert to_delete == [
        {"doc_url": "file:///a.odt", "para_index": 2, "char_start": 0, "char_end": 4}
    ]


def test_enqueue_skipped_when_off():
    with patch("plugin.embeddings.embeddings_indexer.folder_search_enabled", return_value=False):
        with patch("plugin.embeddings.embeddings_indexer.run_in_background") as bg_mock:
            embeddings_indexer.enqueue_folder_index(MagicMock(), MagicMock())
            bg_mock.assert_not_called()


def test_enqueue_skipped_when_inflight():
    ctx = MagicMock()
    with patch("plugin.embeddings.embeddings_indexer.folder_search_enabled", return_value=True):
        with patch(
            "plugin.embeddings.embeddings_indexer.resolve_folder_for_active_doc",
            return_value="/tmp/folder",
        ):
            with patch("plugin.embeddings.embeddings_indexer._try_enqueue", return_value=False):
                with patch(
                    "plugin.framework.queue_executor.main.call",
                    side_effect=lambda fn, *args, **kwargs: fn(*args, **kwargs),
                ):
                    with patch("plugin.embeddings.embeddings_indexer.run_in_background") as bg_mock:
                        embeddings_indexer.enqueue_folder_index(ctx, MagicMock())
                    bg_mock.assert_not_called()


def test_enqueue_marshals_listing_root_only():
    ctx = MagicMock()
    resolve_calls_during_marshal: list[bool] = []
    marshal_depth = [0]

    def mock_execute(fn, *args, **kwargs):
        marshal_depth[0] += 1
        try:
            return fn(*args, **kwargs)
        finally:
            marshal_depth[0] -= 1

    def tracking_resolve(_ctx, _model):
        resolve_calls_during_marshal.append(marshal_depth[0] > 0)
        return "/tmp/folder"

    with patch("plugin.embeddings.embeddings_indexer.folder_search_enabled", return_value=True):
        with patch("plugin.embeddings.embeddings_indexer.resolve_folder_for_active_doc", side_effect=tracking_resolve):
            with patch("plugin.embeddings.embeddings_indexer.maybe_upgrade_legacy_index") as upgrade_mock:
                with patch("shutil.rmtree") as rmtree_mock:
                    with patch("plugin.embeddings.embeddings_indexer._try_enqueue", return_value=False):
                        with patch(
                            "plugin.framework.queue_executor.main.call",
                            side_effect=mock_execute,
                        ):
                            embeddings_indexer.enqueue_folder_index(ctx, MagicMock())

    assert resolve_calls_during_marshal
    assert all(resolve_calls_during_marshal)
    upgrade_mock.assert_not_called()
    rmtree_mock.assert_not_called()


def test_stale_schema_rmtree_runs_on_corpus_index_thread(tmp_path):
    import shutil
    import threading
    from pathlib import Path
    from plugin.framework import worker_pool

    listing = tmp_path / "folder"
    listing.mkdir()
    cache_dir = listing / "writeragent_embeddings"
    cache_dir.mkdir()
    meta_path = cache_dir / "corpus_meta.json"
    meta_path.write_text('{"schema_version": "0"}', encoding="utf-8")
    db_path = cache_dir / "corpus.db"
    db_path.write_text("data", encoding="utf-8")
    zvec_dir = cache_dir / "zvec"
    zvec_dir.mkdir()
    (zvec_dir / "entry").write_text("data", encoding="utf-8")

    handles = []
    real_run = worker_pool.run_in_background

    def wrap_run(*args, **kwargs):
        handle = real_run(*args, **kwargs)
        handles.append(handle)
        return handle

    real_rmtree = shutil.rmtree
    calls_with_threads: list[tuple[str, Path]] = []

    def spy_rmtree(path, *args, **kwargs):
        calls_with_threads.append((threading.current_thread().name, Path(path)))
        return real_rmtree(path, *args, **kwargs)

    caller_thread_name = threading.current_thread().name

    ctx = MagicMock()
    model = MagicMock()

    with patch("plugin.framework.queue_executor.main.is_current", return_value=True), \
         patch("plugin.framework.queue_executor.main.call", side_effect=lambda fn, *a, **kw: fn(*a, **kw)), \
         patch("plugin.embeddings.embeddings_indexer.folder_search_enabled", return_value=True), \
         patch("plugin.embeddings.embeddings_indexer.resolve_folder_for_active_doc", return_value=str(listing)), \
         patch("plugin.embeddings.embeddings_indexer.get_embedding_model", return_value="m"), \
         patch("plugin.embeddings.embeddings_indexer.maintain_folder_index_rpc"), \
         patch("plugin.embeddings.embeddings_indexer.run_in_background", side_effect=wrap_run), \
         patch("plugin.embeddings.embeddings_cache.shutil.rmtree", side_effect=spy_rmtree):
        embeddings_indexer.enqueue_folder_index(ctx, model)
        assert not any(t_name == caller_thread_name for t_name, _ in calls_with_threads)

        assert len(handles) == 1
        handles[0].join(timeout=5.0)

        assert calls_with_threads
        assert all(t_name.startswith("corpus-index-") for t_name, _ in calls_with_threads)
        assert not zvec_dir.exists()
        assert not db_path.exists()
        assert not meta_path.exists()


def test_index_worker_calls_maintain_rpc():
    ctx = MagicMock()
    with patch("plugin.embeddings.embeddings_indexer.maybe_upgrade_legacy_index"), \
         patch("plugin.embeddings.embeddings_indexer.maintain_folder_index_rpc") as mock_maintain, \
         patch("plugin.embeddings.embeddings_indexer.get_embedding_model", return_value="m"), \
         patch("plugin.embeddings.embeddings_indexer._resolve_search_mode", return_value="llama_index"):
        embeddings_indexer._index_worker(ctx, "folderkey", "/tmp/listing")
    mock_maintain.assert_called_once()
    assert mock_maintain.call_args.kwargs["search_mode"] == "llama_index"


def test_resolve_search_mode_reads_config():
    with patch("plugin.embeddings.embeddings_indexer.configured_folder_search_mode", return_value="llama_index"):
        assert embeddings_indexer._resolve_search_mode() == "llama_index"
    with patch("plugin.embeddings.embeddings_indexer.configured_folder_search_mode", return_value="none"):
        assert embeddings_indexer._resolve_search_mode() == "hybrid"


def test_index_worker_clears_inflight_on_failure():
    ctx = MagicMock()
    embeddings_indexer._inflight.add("folderkey")
    with patch("plugin.embeddings.embeddings_indexer.get_embedding_model", return_value="m"):
        with patch("plugin.embeddings.embeddings_indexer.maintain_folder_index_rpc", side_effect=RuntimeError("fail")):
            embeddings_indexer._index_worker(ctx, "folderkey", "/tmp/listing")
    assert "folderkey" not in embeddings_indexer._inflight


def test_ensure_index_wakeup_when_cancelled():
    ctx = MagicMock()
    model = MagicMock()
    scope = MagicMock()
    scope.is_cancelled.return_value = True

    with patch("plugin.embeddings.embeddings_indexer.get_current_send_cancellation", return_value=scope):
        with patch("plugin.embeddings.embeddings_indexer.enqueue_folder_index") as enqueue_mock:
            embeddings_indexer.ensure_index_wakeup(ctx, model)
            enqueue_mock.assert_not_called()


def test_ensure_index_wakeup_when_not_cancelled():
    ctx = MagicMock()
    model = MagicMock()

    with patch("plugin.embeddings.embeddings_indexer.get_current_send_cancellation", return_value=None):
        with patch("plugin.embeddings.embeddings_indexer.enqueue_folder_index") as enqueue_mock:
            embeddings_indexer.ensure_index_wakeup(ctx, model)
            enqueue_mock.assert_called_once_with(ctx, model)
