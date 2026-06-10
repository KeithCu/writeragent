# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.scripting.embeddings_ingest_graph."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from plugin.scripting import embeddings_ingest_graph


def test_split_paragraph_text_long_produces_multiple_chunks():
    text = "word " * 200
    base_meta = {
        "doc_url": "file:///a.odt",
        "para_index": 0,
        "content_hash": "h",
        "file_mtime": 1.0,
        "embedding_model": "m",
    }
    mock_splitter = MagicMock()
    mock_splitter.split_text.return_value = [text[:300], text[300:600], text[600:]]
    with patch.object(embeddings_ingest_graph, "_import_splitter", return_value=mock_splitter):
        chunks = embeddings_ingest_graph._split_paragraph_text(text.strip(), base_meta)
    assert len(chunks) == 3
    assert all(c["char_end"] > c["char_start"] for c in chunks)


def test_split_chunks_short_paragraph_single_chunk():
    fake_doc = MagicMock()
    fake_doc.page_content = "short paragraph"
    fake_doc.metadata = {
        "doc_url": "file:///a.odt",
        "para_index": 0,
        "content_hash": "h",
        "file_mtime": 1.0,
        "embedding_model": "m",
    }
    state = {"documents": [fake_doc]}
    out = embeddings_ingest_graph.split_chunks(state)
    assert len(out["chunks"]) == 1
    assert out["chunks"][0]["char_start"] == 0


def test_ingest_paragraphs_invokes_graph(tmp_path):
    meta_path = tmp_path / "corpus_meta.json"
    with patch.object(embeddings_ingest_graph, "_get_ingest_graph") as mock_graph_factory:
        mock_graph = MagicMock()
        mock_graph.invoke.return_value = {"upserted": 1, "dim": 384}
        mock_graph_factory.return_value = mock_graph
        result = embeddings_ingest_graph.ingest_paragraphs(
            str(tmp_path / "chroma"),
            "folder_key",
            str(meta_path),
            "all-MiniLM-L6-v2",
            [{"text": "hello", "doc_url": "file:///a.odt", "para_index": 0, "content_hash": "h", "file_mtime": 1.0}],
        )
    assert result["indexed"] == 1
    assert result["storage_backend"] == "chroma"
    mock_graph.invoke.assert_called_once()
