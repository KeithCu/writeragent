# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.doc.embeddings_indexer."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from plugin.doc import embeddings_cache, embeddings_indexer
from plugin.doc.embeddings_chunker import ParagraphChunk, content_hash


def test_file_is_stale_when_no_rows(tmp_path):
    state_path = tmp_path / "file_index_state.json"
    assert embeddings_indexer.file_is_stale(state_path, "file:///a.odt", 100.0) is True


def test_file_is_stale_when_mtime_newer(tmp_path):
    state_path = tmp_path / "file_index_state.json"
    embeddings_cache.mark_file_indexed(
        state_path,
        "file:///a.odt",
        50.0,
        indexed_at=50.0,
        paragraphs={"0": "h"},
    )
    assert embeddings_indexer.file_is_stale(state_path, "file:///a.odt", 100.0) is True
    assert embeddings_indexer.file_is_stale(state_path, "file:///a.odt", 40.0) is False


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


def test_needs_cold_rebuild_on_model_change(tmp_path):
    meta_path = tmp_path / "corpus_meta.json"
    embeddings_cache.ensure_corpus_meta(meta_path, embedding_model="model-a", chunk_count=1)
    assert embeddings_cache.needs_cold_rebuild(meta_path, "model-b") is True
    assert embeddings_cache.needs_cold_rebuild(meta_path, "model-a") is False


def test_enqueue_skipped_in_grep_mode():
    with patch("plugin.doc.embeddings_indexer.document_research_uses_embeddings", return_value=False):
        embeddings_indexer.enqueue_folder_index(MagicMock(), MagicMock(), MagicMock())


def test_mark_file_indexed_clears_stale_mtime(tmp_path):
    state_path = tmp_path / "file_index_state.json"
    doc_url = "file:///a.odt"
    embeddings_cache.mark_file_indexed(
        state_path,
        doc_url,
        90.0,
        indexed_at=50.0,
        paragraphs={"0": "h"},
    )
    assert embeddings_indexer.file_is_stale(state_path, doc_url, 100.0) is True
    embeddings_cache.mark_file_indexed(state_path, doc_url, 100.0, indexed_at=150.0, paragraphs={"0": "h"})
    state = embeddings_cache.get_file_index_state(state_path, doc_url)
    assert state["file_mtime"] == 100.0
    assert state["last_indexed_at"] == 150.0
    assert embeddings_indexer.file_is_stale(state_path, doc_url, 100.0) is False


def test_refresh_marks_unchanged_file_after_mtime_bump(tmp_path):
    ctx = MagicMock()
    services = MagicMock()
    model = MagicMock()
    folder_key = "abc123"
    doc_url = "file:///a.odt"
    entry = {"url": doc_url, "path": "/tmp/a.odt", "modified": 200.0, "doc_type_guess": "writer"}
    chunk = ParagraphChunk(
        doc_url=doc_url,
        para_index=0,
        char_start=0,
        char_end=3,
        text="same",
        content_hash=content_hash("same"),
        file_mtime=200.0,
    )
    meta_path = tmp_path / "corpus_meta.json"
    state_path = tmp_path / "file_index_state.json"
    persist_dir = tmp_path / "chroma"

    embeddings_cache.ensure_corpus_meta(meta_path, embedding_model="m", chunk_count=1)
    embeddings_cache.mark_file_indexed(
        state_path,
        doc_url,
        100.0,
        indexed_at=50.0,
        paragraphs={"0": content_hash("same")},
    )

    with patch("plugin.doc.embeddings_indexer.get_embedding_model", return_value="m"):
        with patch("plugin.doc.embeddings_indexer.corpus_meta_path", return_value=meta_path):
            with patch("plugin.doc.embeddings_indexer.file_index_state_path", return_value=state_path):
                with patch("plugin.doc.embeddings_indexer.chroma_persist_dir", return_value=persist_dir):
                    with patch("plugin.doc.embeddings_indexer.needs_cold_rebuild", return_value=False):
                        with patch("plugin.doc.embeddings_indexer.list_indexable_sibling_files", return_value=([entry], None)):
                            with patch("plugin.doc.embeddings_indexer.extract_paragraph_chunks_from_file", return_value=[chunk]):
                                with patch("plugin.doc.embeddings_indexer.delete_paragraphs") as del_mock:
                                    with patch("plugin.doc.embeddings_indexer.index_paragraphs") as idx_mock:
                                        embeddings_indexer.refresh_folder_index_incremental(
                                            ctx, services, model, folder_key=folder_key
                                        )
                                        del_mock.assert_not_called()
                                        idx_mock.assert_not_called()

    assert embeddings_indexer.file_is_stale(state_path, doc_url, 200.0) is False
