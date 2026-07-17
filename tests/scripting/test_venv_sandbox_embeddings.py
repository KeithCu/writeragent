# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for embeddings_index trusted-action dispatch (replaces sandbox stub path)."""

from __future__ import annotations

from unittest.mock import patch

from plugin.embeddings.venv.embeddings_index_dispatch import dispatch_trusted


def test_hybrid_search_trusted_action_dispatches():
    fake_hits = {"hits": [{"doc_url": "file:///a.odt", "score": 0.5, "snippet": "dspy"}]}
    with patch("plugin.embeddings.venv.embeddings_index.hybrid_search", return_value=fake_hits) as mock_search:
        result = dispatch_trusted(
            {
                "helper": "hybrid_search",
                "params": {
                    "db_path": "/tmp/corpus.db",
                    "query": "dspy",
                    "k": 20,
                    "model": "all-MiniLM-L6-v2",
                    "near_slop": 10,
                },
            }
        )

    assert result["hits"][0]["doc_url"] == "file:///a.odt"
    mock_search.assert_called_once_with(
        "/tmp/corpus.db",
        "dspy",
        20,
        model_name="all-MiniLM-L6-v2",
        near_slop=10,
        doc_url_filter=None,
        use_mmr=True,
        rerank_model=None,
        search_mode="hybrid",
    )


def test_warm_embedder_trusted_action():
    with patch("plugin.embeddings.venv.embeddings_index._get_embedder") as mock_get:
        result = dispatch_trusted(
            {
                "helper": "warm_embedder",
                "params": {"model": "all-MiniLM-L6-v2"},
            }
        )
    assert result == {"status": "warmed", "model": "all-MiniLM-L6-v2"}
    mock_get.assert_called_once_with("all-MiniLM-L6-v2")
