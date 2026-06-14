# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for cross_encoder_rerank_candidates."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from plugin.embeddings.venv.embeddings_cross_encoder_rerank import cross_encoder_rerank_candidates


def test_cross_encoder_rerank_reorders_by_score() -> None:
    candidates = [
        {"doc_url": "file:///a.odt", "para_index": 0, "snippet": "alpha", "score": 0.1},
        {"doc_url": "file:///b.odt", "para_index": 1, "snippet": "beta", "score": 0.9},
    ]
    mock_encoder = MagicMock()
    mock_encoder.predict.return_value = [0.2, 0.8]

    with patch(
        "plugin.embeddings.venv.embeddings_cross_encoder_rerank._get_cross_encoder",
        return_value=mock_encoder,
    ):
        out = cross_encoder_rerank_candidates(
            "query",
            candidates,
            model="cross-encoder/ms-marco-MiniLM-L-6-v2",
            top_n=2,
        )

    assert out[0]["doc_url"] == "file:///b.odt"
    assert out[0]["score"] == 0.8
    assert out[1]["doc_url"] == "file:///a.odt"


def test_cross_encoder_rerank_falls_back_on_failure() -> None:
    candidates = [
        {"doc_url": "file:///a.odt", "para_index": 0, "snippet": "alpha", "score": 0.1},
        {"doc_url": "file:///b.odt", "para_index": 1, "snippet": "beta", "score": 0.9},
    ]

    with patch(
        "plugin.embeddings.venv.embeddings_cross_encoder_rerank._get_cross_encoder",
        side_effect=RuntimeError("boom"),
    ):
        out = cross_encoder_rerank_candidates(
            "query",
            candidates,
            model="cross-encoder/ms-marco-MiniLM-L-6-v2",
            top_n=2,
        )

    assert out == candidates[:2]
