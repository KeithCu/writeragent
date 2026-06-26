# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.embeddings.embeddings_indexer."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from plugin.embeddings import embeddings_cache, embeddings_indexer
from plugin.embeddings.embeddings_fs import ParagraphChunk, content_hash


def test_file_is_stale_when_no_rows(tmp_path):
    db_path = tmp_path / "corpus.db"
    assert embeddings_cache.file_is_stale(db_path, "file:///a.odt", 100.0) is True


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
    finally:
        conn.close()
    embeddings_cache.mark_file_indexed(
        db_path,
        "file:///a.odt",
        50.0,
        indexed_at=50.0,
    )
    assert embeddings_cache.file_is_stale(db_path, "file:///a.odt", 100.0) is True
    assert embeddings_cache.file_is_stale(db_path, "file:///a.odt", 40.0) is False


def test_diff_paragraph_rows_detects_change_and_delete(tmp_path):
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
    to_index, to_delete = embeddings_cache.diff_paragraph_rows(db_path, chunks)
    assert len(to_index) == 2
    assert to_delete == [
        {"doc_url": "file:///a.odt", "para_index": 2, "char_start": 0, "char_end": 4}
    ]


def test_enqueue_skipped_when_off():
    with patch("plugin.embeddings.embeddings_indexer.folder_search_enabled", return_value=False):
        with patch("plugin.embeddings.embeddings_indexer.run_in_background") as bg_mock:
            embeddings_indexer.enqueue_folder_index(MagicMock(), MagicMock(), MagicMock())
            bg_mock.assert_not_called()


def test_enqueue_skipped_when_inflight():
    ctx = MagicMock()
    with patch("plugin.embeddings.embeddings_indexer.folder_search_enabled", return_value=True):
        with patch(
            "plugin.embeddings.embeddings_indexer.resolve_index_context",
            return_value=("key", MagicMock(), MagicMock(), "/tmp/folder"),
        ):
            with patch("plugin.embeddings.embeddings_indexer._try_enqueue", return_value=False):
                with patch(
                    "plugin.framework.queue_executor.execute_on_main_thread",
                    side_effect=lambda fn, *args, **kwargs: fn(*args, **kwargs),
                ):
                    with patch("plugin.embeddings.embeddings_indexer.run_in_background") as bg_mock:
                        embeddings_indexer.enqueue_folder_index(ctx, MagicMock(), MagicMock())
                    bg_mock.assert_not_called()


def test_enqueue_marshals_resolve_index_context():
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
        return ("key", MagicMock(), MagicMock(), "/tmp/folder")

    with patch("plugin.embeddings.embeddings_indexer.folder_search_enabled", return_value=True):
        with patch("plugin.embeddings.embeddings_indexer.resolve_index_context", side_effect=tracking_resolve):
            with patch("plugin.embeddings.embeddings_indexer._try_enqueue", return_value=False):
                with patch(
                    "plugin.framework.queue_executor.execute_on_main_thread",
                    side_effect=mock_execute,
                ):
                    embeddings_indexer.enqueue_folder_index(ctx, MagicMock(), MagicMock())

    assert resolve_calls_during_marshal
    assert all(resolve_calls_during_marshal)


def test_index_worker_calls_maintain_rpc():
    ctx = MagicMock()
    with patch("plugin.embeddings.embeddings_indexer.get_embedding_model", return_value="m"):
        with patch("plugin.embeddings.embeddings_indexer._resolve_search_mode", return_value="llama_index"):
            with patch("plugin.embeddings.embeddings_indexer.maintain_folder_index_rpc") as maintain_mock:
                embeddings_indexer._index_worker(ctx, "folderkey", "/tmp/listing")
                maintain_mock.assert_called_once_with(
                    ctx,
                    "/tmp/listing",
                    model="m",
                    mode="auto",
                    search_mode="llama_index",
                )


def test_resolve_search_mode_reads_config():
    ctx = MagicMock()
    with patch("plugin.embeddings.embeddings_indexer.get_config", return_value="llama_index"):
        assert embeddings_indexer._resolve_search_mode(ctx) == "llama_index"
    with patch("plugin.embeddings.embeddings_indexer.get_config", return_value="none"):
        assert embeddings_indexer._resolve_search_mode(ctx) == "hybrid"


def test_index_worker_clears_inflight_on_failure():
    ctx = MagicMock()
    embeddings_indexer._inflight.add("folderkey")
    with patch("plugin.embeddings.embeddings_indexer.get_embedding_model", return_value="m"):
        with patch("plugin.embeddings.embeddings_indexer.maintain_folder_index_rpc", side_effect=RuntimeError("fail")):
            embeddings_indexer._index_worker(ctx, "folderkey", "/tmp/listing")
    assert "folderkey" not in embeddings_indexer._inflight
