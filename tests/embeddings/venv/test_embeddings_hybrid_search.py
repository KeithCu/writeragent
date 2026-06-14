# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for hybrid_corpus_search cross-encoder rerank after RRF."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np

from plugin.embeddings.venv.embeddings_hybrid_search import hybrid_corpus_search  # noqa: E402


def _fused_candidates() -> list[dict]:
    return [
        {
            "chunk_id": 1,
            "doc_url": "file:///a.odt",
            "para_index": 0,
            "snippet": "streaming sidebar alpha",
            "score": 0.03,
            "matched_by": ["fts", "vec"],
            "embedding": np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32),
        },
        {
            "chunk_id": 2,
            "doc_url": "file:///b.odt",
            "para_index": 1,
            "snippet": "streaming sidebar beta",
            "score": 0.025,
            "matched_by": ["vec"],
            "embedding": np.array([0.99, 0.01, 0.0, 0.0], dtype=np.float32),
        },
        {
            "chunk_id": 3,
            "doc_url": "file:///c.odt",
            "para_index": 2,
            "snippet": "budget figures",
            "score": 0.02,
            "matched_by": ["fts"],
            "embedding": np.array([0.0, 1.0, 0.0, 0.0], dtype=np.float32),
        },
    ]


def test_hybrid_calls_cross_encoder_when_rerank_model_set() -> None:
    fused = _fused_candidates()
    reranked = [fused[2], fused[0]]

    conn = MagicMock()
    with (
        patch("plugin.embeddings.venv.embeddings_hybrid_search.connect_corpus_db", return_value=conn),
        patch("plugin.embeddings.venv.embeddings_hybrid_search.embed_texts", return_value={"vectors": [[1.0, 0.0, 0.0, 0.0]]}),
        patch("plugin.embeddings.venv.embeddings_hybrid_search.fts_corpus_search", return_value=[]),
        patch("plugin.embeddings.venv.embeddings_hybrid_search.vec0_search", return_value=[]),
        patch("plugin.embeddings.venv.embeddings_hybrid_search.load_embeddings_for_candidates"),
        patch("plugin.embeddings.venv.embeddings_hybrid_search.merge_hybrid_hits", return_value=list(fused)),
        patch(
            "plugin.embeddings.venv.embeddings_hybrid_search.expand_candidates_to_parent_paragraphs",
            side_effect=lambda _db, rows: rows,
        ),
        patch(
            "plugin.embeddings.venv.embeddings_hybrid_search.cross_encoder_rerank_candidates",
            return_value=reranked,
        ) as rerank_mock,
    ):
        result = hybrid_corpus_search(
            "/tmp/corpus.db",
            "streaming sidebar",
            2,
            model_name="test-model",
            use_mmr=True,
            rerank_model="cross-encoder/ms-marco-MiniLM-L-6-v2",
        )

    rerank_mock.assert_called_once()
    assert len(result["hits"]) == 2
    assert result["hits"][0]["doc_url"] == "file:///c.odt"


def test_hybrid_skips_rerank_when_k_is_one() -> None:
    fused = _fused_candidates()

    conn = MagicMock()
    with (
        patch("plugin.embeddings.venv.embeddings_hybrid_search.connect_corpus_db", return_value=conn),
        patch("plugin.embeddings.venv.embeddings_hybrid_search.embed_texts", return_value={"vectors": [[1.0, 0.0, 0.0, 0.0]]}),
        patch("plugin.embeddings.venv.embeddings_hybrid_search.fts_corpus_search", return_value=[]),
        patch("plugin.embeddings.venv.embeddings_hybrid_search.vec0_search", return_value=[]),
        patch("plugin.embeddings.venv.embeddings_hybrid_search.load_embeddings_for_candidates"),
        patch("plugin.embeddings.venv.embeddings_hybrid_search.merge_hybrid_hits", return_value=list(fused)),
        patch(
            "plugin.embeddings.venv.embeddings_hybrid_search.expand_candidates_to_parent_paragraphs",
            side_effect=lambda _db, rows: rows,
        ),
        patch("plugin.embeddings.venv.embeddings_hybrid_search.cross_encoder_rerank_candidates") as rerank_mock,
    ):
        hybrid_corpus_search(
            "/tmp/corpus.db",
            "streaming sidebar",
            1,
            model_name="test-model",
            use_mmr=True,
            rerank_model="cross-encoder/ms-marco-MiniLM-L-6-v2",
        )

    rerank_mock.assert_not_called()


def test_hybrid_skips_rerank_when_disabled() -> None:
    fused = _fused_candidates()[:2]

    conn = MagicMock()
    with (
        patch("plugin.embeddings.venv.embeddings_hybrid_search.connect_corpus_db", return_value=conn),
        patch("plugin.embeddings.venv.embeddings_hybrid_search.embed_texts", return_value={"vectors": [[1.0, 0.0, 0.0, 0.0]]}),
        patch("plugin.embeddings.venv.embeddings_hybrid_search.fts_corpus_search", return_value=[]),
        patch("plugin.embeddings.venv.embeddings_hybrid_search.vec0_search", return_value=[]),
        patch("plugin.embeddings.venv.embeddings_hybrid_search.load_embeddings_for_candidates"),
        patch("plugin.embeddings.venv.embeddings_hybrid_search.merge_hybrid_hits", return_value=list(fused)),
        patch(
            "plugin.embeddings.venv.embeddings_hybrid_search.expand_candidates_to_parent_paragraphs",
            side_effect=lambda _db, rows: rows,
        ),
        patch("plugin.embeddings.venv.embeddings_hybrid_search.cross_encoder_rerank_candidates") as rerank_mock,
    ):
        result = hybrid_corpus_search(
            "/tmp/corpus.db",
            "streaming sidebar",
            2,
            model_name="test-model",
            use_mmr=False,
        )

    rerank_mock.assert_not_called()
    assert len(result["hits"]) == 2
