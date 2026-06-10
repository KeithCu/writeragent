# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.scripting.embeddings_search_graph."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np

from plugin.scripting import embeddings_search_graph


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


def test_search_embeddings_graph_invokes_graph():
    with patch.object(embeddings_search_graph, "_get_search_graph") as mock_graph_factory:
        mock_graph = MagicMock()
        mock_graph.invoke.return_value = {"hits": [{"doc_url": "file:///a.odt", "score": 0.9}]}
        mock_graph_factory.return_value = mock_graph
        result = embeddings_search_graph.search_embeddings_graph(
            "/chroma",
            "folder_key",
            "query",
            3,
            model_name="all-MiniLM-L6-v2",
        )
    assert len(result["hits"]) == 1
    mock_graph.invoke.assert_called_once()
