# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.doc.embeddings_cache."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from plugin.doc import embeddings_cache
from plugin.doc.embeddings_chunker import ParagraphChunk, content_hash


def test_folder_corpus_key_stable_and_normalized():
    a = embeddings_cache.folder_corpus_key("/tmp/foo/bar")
    b = embeddings_cache.folder_corpus_key("/tmp/foo/bar/")
    c = embeddings_cache.folder_corpus_key("/tmp/foo/../foo/bar")
    assert a == b == c
    assert len(a) == 64


def test_chroma_persist_dir_under_profile(tmp_path):
    ctx = MagicMock()
    with patch("plugin.doc.embeddings_cache.user_config_dir", return_value=str(tmp_path)):
        path = embeddings_cache.chroma_persist_dir(ctx, "abc123")
    assert path == tmp_path / "writeragent_embeddings" / "abc123" / "chroma"
    assert path.is_dir()


def test_ensure_corpus_meta_writes_json(tmp_path):
    meta_path = tmp_path / "corpus_meta.json"
    embeddings_cache.ensure_corpus_meta(meta_path, embedding_model="all-MiniLM-L6-v2", dim=384, chunk_count=10)
    meta = embeddings_cache.read_corpus_meta(meta_path)
    assert meta["schema_version"] == embeddings_cache.SCHEMA_VERSION
    assert meta["embedding_model"] == "all-MiniLM-L6-v2"
    assert meta["dim"] == "384"
    assert meta["chunk_count"] == "10"
    assert meta["storage_backend"] == embeddings_cache.STORAGE_BACKEND


def test_index_is_empty_missing_and_populated(tmp_path):
    meta_path = tmp_path / "corpus_meta.json"
    persist = tmp_path / "chroma"
    assert embeddings_cache.index_is_empty(meta_path, persist) is True

    embeddings_cache.write_corpus_meta(meta_path, chunk_count="0")
    assert embeddings_cache.index_is_empty(meta_path, persist) is True

    embeddings_cache.write_corpus_meta(meta_path, chunk_count="3")
    assert embeddings_cache.index_is_empty(meta_path, persist) is False


def test_resolve_index_context_no_listing_root():
    ctx = MagicMock()
    model = MagicMock()
    with patch("plugin.doc.embeddings_cache.resolve_folder_for_active_doc", return_value=None):
        key, persist, meta, err = embeddings_cache.resolve_index_context(ctx, model)
    assert key is None
    assert persist is None
    assert meta is None
    assert "Save the document" in err


def test_resolve_index_context_ok(tmp_path):
    ctx = MagicMock()
    model = MagicMock()
    listing = str(tmp_path / "project")
    Path(listing).mkdir()
    with patch("plugin.doc.embeddings_cache.resolve_folder_for_active_doc", return_value=listing):
        with patch("plugin.doc.embeddings_cache.user_config_dir", return_value=str(tmp_path / "profile")):
            key, persist, meta, root = embeddings_cache.resolve_index_context(ctx, model)
    assert root == listing
    assert key == embeddings_cache.folder_corpus_key(listing)
    assert persist.name == "chroma"
    assert meta.name == "corpus_meta.json"


def test_model_matches_index(tmp_path):
    meta_path = tmp_path / "corpus_meta.json"
    embeddings_cache.write_corpus_meta(meta_path, embedding_model="model-a")
    assert embeddings_cache.model_matches_index(meta_path, "model-a") is True
    assert embeddings_cache.model_matches_index(meta_path, "model-b") is False


def test_remove_legacy_index_db(tmp_path):
    ctx = MagicMock()
    folder_key = "abc"
    with patch("plugin.doc.embeddings_cache.user_config_dir", return_value=str(tmp_path)):
        base = embeddings_cache.folder_cache_dir(ctx, folder_key)
        legacy = base / "index.db"
        legacy.write_text("sqlite", encoding="utf-8")
        assert embeddings_cache.remove_legacy_index(ctx, folder_key) is True
        assert not legacy.is_file()


def test_file_index_state_and_diff(tmp_path):
    state_path = tmp_path / "file_index_state.json"
    chunk = ParagraphChunk(
        doc_url="file:///a.odt",
        para_index=0,
        char_start=0,
        char_end=3,
        text="new",
        content_hash=content_hash("new"),
        file_mtime=1.0,
    )
    embeddings_cache.mark_file_indexed(
        state_path,
        "file:///a.odt",
        50.0,
        paragraphs={"0": content_hash("old"), "2": content_hash("gone")},
    )
    to_index, to_delete = embeddings_cache.diff_paragraph_rows(state_path, [chunk])
    assert len(to_index) == 1
    assert to_delete == [{"doc_url": "file:///a.odt", "para_index": 2}]
