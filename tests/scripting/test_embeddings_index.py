# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.scripting.embeddings_index."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from plugin.scripting import embeddings_index


@pytest.fixture(autouse=True)
def clear_model_cache():
    embeddings_index._MODEL_CACHE.clear()
    yield
    embeddings_index._MODEL_CACHE.clear()


def test_embed_texts_empty_input():
    result = embeddings_index.embed_texts("all-MiniLM-L6-v2", [])
    assert result == {"model": "all-MiniLM-L6-v2", "dim": 0, "vectors": [], "indices": []}


def test_embed_texts_skips_blank_strings():
    mock_embedder = MagicMock()
    mock_embedder.encode.return_value = [np.array([1.0, 0.0], dtype=np.float32), np.array([0.0, 1.0], dtype=np.float32)]

    with patch.object(embeddings_index, "_get_embedder", return_value=mock_embedder):
        result = embeddings_index.embed_texts("all-MiniLM-L6-v2", ["  hello  ", "", "world", "   "])

    assert result["indices"] == [0, 2]
    assert result["dim"] == 2
    assert len(result["vectors"]) == 2
    mock_embedder.encode.assert_called_once_with(["hello", "world"], convert_to_tensor=False, show_progress_bar=False)


def test_embed_texts_normalizes_vectors():
    mock_embedder = MagicMock()
    mock_embedder.encode.return_value = [np.array([3.0, 4.0], dtype=np.float32)]

    with patch.object(embeddings_index, "_get_embedder", return_value=mock_embedder):
        result = embeddings_index.embed_texts("all-MiniLM-L6-v2", ["text"], normalize=True)

    vec = np.array(result["vectors"][0], dtype=np.float32)
    assert pytest.approx(float(np.linalg.norm(vec)), rel=1e-5) == 1.0


def test_embed_texts_reuses_cached_model():
    mock_embedder = MagicMock()
    mock_embedder.encode.return_value = [np.array([1.0, 0.0], dtype=np.float32)]
    mock_ctor = MagicMock(return_value=mock_embedder)
    fake_mod = MagicMock()
    fake_mod.SentenceTransformer = mock_ctor

    with patch.dict(sys.modules, {"sentence_transformers": fake_mod}):
        embeddings_index.embed_texts("all-MiniLM-L6-v2", ["a"])
        embeddings_index.embed_texts("all-MiniLM-L6-v2", ["b"])

    mock_ctor.assert_called_once_with("all-MiniLM-L6-v2")
    assert mock_embedder.encode.call_count == 2


def test_embed_texts_requires_model_name():
    with pytest.raises(ValueError, match="model name"):
        embeddings_index.embed_texts("", ["text"])


def test_index_paragraphs_delegates_to_ingest_graph():
    with patch("plugin.scripting.embeddings_ingest_graph.ingest_paragraphs", return_value={"indexed": 2, "dim": 384, "storage_backend": "chroma"}) as mock_ingest:
        result = embeddings_index.index_paragraphs("/chroma", "folder_key", "/meta.json", "all-MiniLM-L6-v2", [{"text": "hi"}])
    assert result["indexed"] == 2
    mock_ingest.assert_called_once()


def test_knn_search_delegates_to_search_graph():
    hits = [{"doc_url": "file:///a.odt", "para_index": 0, "score": 0.9}]
    with patch("plugin.scripting.embeddings_search_graph.search_embeddings_graph", return_value={"hits": hits}) as mock_search:
        result = embeddings_index.knn_search("/chroma", "folder_key", "query", 5, model_name="all-MiniLM-L6-v2")
    assert result["hits"] == hits
    mock_search.assert_called_once()


def test_delete_paragraphs_removes_from_chroma(tmp_path):
    mock_collection = MagicMock()
    mock_collection.count.return_value = 0
    meta_path = tmp_path / "corpus_meta.json"
    meta_path.write_text('{"chunk_count": "1"}', encoding="utf-8")

    with patch("plugin.scripting.embeddings_chroma.get_collection", return_value=mock_collection):
        with patch("plugin.scripting.embeddings_chroma.delete_paragraph_keys", return_value=1):
            result = embeddings_index.delete_paragraphs(
                str(tmp_path),
                "folder_key",
                str(meta_path),
                [{"doc_url": "file:///a.odt", "para_index": 0}],
            )
    assert result["deleted"] == 1
