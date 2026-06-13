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
    state_path = tmp_path / "file_index_state.json"
    assert embeddings_cache.file_is_stale(state_path, "file:///a.odt", 100.0) is True


def test_file_is_stale_when_mtime_newer(tmp_path):
    state_path = tmp_path / "file_index_state.json"
    embeddings_cache.mark_file_indexed(
        state_path,
        "file:///a.odt",
        50.0,
        indexed_at=50.0,
        paragraphs={"0": "h"},
    )
    assert embeddings_cache.file_is_stale(state_path, "file:///a.odt", 100.0) is True
    assert embeddings_cache.file_is_stale(state_path, "file:///a.odt", 40.0) is False


def test_diff_paragraph_rows_detects_change_and_delete(tmp_path):
    state_path = tmp_path / "file_index_state.json"
    embeddings_cache.mark_file_indexed(
        state_path,
        "file:///a.odt",
        1.0,
        paragraphs={
            "0": content_hash("old"),
            "2": content_hash("gone"),
        },
    )

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
            char_end=3,
            text="added",
            content_hash=content_hash("added"),
            file_mtime=1.0,
        ),
    ]
    to_index, to_delete = embeddings_cache.diff_paragraph_rows(state_path, chunks)
    assert len(to_index) == 2
    assert to_delete == [{"doc_url": "file:///a.odt", "para_index": 2}]


def test_enqueue_skipped_in_none_mode():
    with patch("plugin.embeddings.embeddings_indexer.get_folder_search_mode", return_value="none"):
        with patch("plugin.embeddings.embeddings_indexer.run_in_background") as bg_mock:
            embeddings_indexer.enqueue_folder_index(MagicMock(), MagicMock(), MagicMock())
            bg_mock.assert_not_called()


def test_enqueue_skipped_when_inflight():
    ctx = MagicMock()
    with patch("plugin.embeddings.embeddings_indexer.get_folder_search_mode", return_value="embeddings"):
        with patch(
            "plugin.embeddings.embeddings_indexer.resolve_index_context",
            return_value=("key", MagicMock(), MagicMock(), "/tmp/folder"),
        ):
            with patch("plugin.embeddings.embeddings_indexer._try_enqueue", return_value=False):
                with patch("plugin.embeddings.embeddings_indexer.run_in_background") as bg_mock:
                    embeddings_indexer.enqueue_folder_index(ctx, MagicMock(), MagicMock())
                    bg_mock.assert_not_called()


def test_index_worker_calls_maintain_rpc():
    ctx = MagicMock()
    with patch("plugin.embeddings.embeddings_indexer.get_embedding_model", return_value="m"):
        with patch("plugin.embeddings.embeddings_indexer.maintain_folder_index_rpc") as maintain_mock:
            embeddings_indexer._index_worker(ctx, "folderkey", "/tmp/listing", "hybrid")
            maintain_mock.assert_called_once_with(
                ctx,
                "/tmp/listing",
                model="m",
                mode="auto",
                search_mode="hybrid",
            )


def test_index_worker_clears_inflight_on_failure():
    ctx = MagicMock()
    embeddings_indexer._inflight.add("folderkey")
    with patch("plugin.embeddings.embeddings_indexer.get_embedding_model", return_value="m"):
        with patch("plugin.embeddings.embeddings_indexer.maintain_folder_index_rpc", side_effect=RuntimeError("fail")):
            embeddings_indexer._index_worker(ctx, "folderkey", "/tmp/listing", "fts")
    assert "folderkey" not in embeddings_indexer._inflight
