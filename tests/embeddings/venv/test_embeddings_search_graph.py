# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.embeddings.venv.embeddings_search_graph."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np

from plugin.embeddings.venv import embeddings_search_graph


def test_metadata_filter_drops_wrong_model():
    state = {
        "model": "model-a",
        "candidates": [
            {"doc_url": "file:///a.odt", "embedding_model": "model-a", "score": 0.9},
            {"doc_url": "file:///b.odt", "embedding_model": "model-b", "score": 0.8},
        ],
    }
    out = embeddings_search_graph.metadata_filter(state)
    assert len(out["candidates"]) == 1
    assert out["candidates"][0]["doc_url"] == "file:///a.odt"


def test_mmr_reduces_redundant_candidates():
    q = np.array([1.0, 0.0], dtype=np.float32)
    emb_a = np.array([1.0, 0.0], dtype=np.float32)
    emb_b = np.array([0.99, 0.01], dtype=np.float32)
    emb_c = np.array([0.0, 1.0], dtype=np.float32)
    candidates = [
        {"chunk_id": "a", "score": 0.99, "embedding": emb_a},
        {"chunk_id": "b", "score": 0.98, "embedding": emb_b},
        {"chunk_id": "c", "score": 0.5, "embedding": emb_c},
    ]
    picked = embeddings_search_graph._max_marginal_relevance(q, [emb_a, emb_b, emb_c], candidates, 2)
    assert len(picked) == 2
    assert picked[0]["chunk_id"] == "a"


def test_hit_snippet_returns_full_chunk_within_limit():
    from plugin.embeddings.venv.embeddings_ingest_graph import CHUNK_SIZE

    text = "alpha " * 60
    cleaned = " ".join(text.split())
    snippet = embeddings_search_graph._hit_snippet(text)
    assert snippet == cleaned
    assert len(snippet) <= CHUNK_SIZE


def test_hit_snippet_truncates_beyond_chunk_size():
    from plugin.embeddings.venv.embeddings_ingest_graph import CHUNK_SIZE

    text = "word " * 200
    snippet = embeddings_search_graph._hit_snippet(text)
    assert snippet.endswith("…")
    assert len(snippet) == CHUNK_SIZE


def test_hit_snippet_collapses_whitespace():
    assert embeddings_search_graph._hit_snippet("  hello\n\nworld  ") == "hello world"


def test_public_hit_omits_char_offsets():
    hit = embeddings_search_graph._public_hit_from_candidate(
        {
            "chunk_id": "abc",
            "doc_url": "file:///a.odt",
            "para_index": 3,
            "snippet": "preview text",
            "score": 0.91,
            "char_start": 10,
            "char_end": 20,
        }
    )
    assert hit == {
        "chunk_id": "abc",
        "doc_url": "file:///a.odt",
        "para_index": 3,
        "snippet": "preview text",
        "score": 0.91,
    }
    assert "char_start" not in hit
    assert "char_end" not in hit


def test_rerank_returns_snippet_hits():
    state = {
        "k": 2,
        "query_vec": [1.0, 0.0],
        "candidates": [
            {
                "chunk_id": "a",
                "doc_url": "file:///a.odt",
                "para_index": 1,
                "snippet": "first hit",
                "score": 0.9,
                "embedding": None,
            },
            {
                "chunk_id": "b",
                "doc_url": "file:///b.odt",
                "para_index": 0,
                "snippet": "second hit",
                "score": 0.8,
                "embedding": None,
            },
        ],
    }
    out = embeddings_search_graph.rerank(state)
    assert len(out["hits"]) == 2
    assert out["hits"][0]["snippet"] == "first hit"
    assert "char_start" not in out["hits"][0]


def test_search_embeddings_graph_invokes_graph():
    with patch.object(embeddings_search_graph, "_get_search_graph") as mock_graph_factory:
        mock_graph = MagicMock()
        mock_graph.invoke.return_value = {"hits": [{"doc_url": "file:///a.odt", "score": 0.9}]}
        mock_graph_factory.return_value = mock_graph
        result = embeddings_search_graph.search_embeddings_graph(
            "/chroma/corpus.db",
            "query",
            3,
            model_name="all-MiniLM-L6-v2",
        )
    assert len(result["hits"]) == 1
    mock_graph.invoke.assert_called_once()
