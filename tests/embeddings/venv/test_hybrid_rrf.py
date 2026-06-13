# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for vendored reciprocal_rank_fusion in hybrid_rrf."""

from __future__ import annotations

from plugin.embeddings.venv.hybrid_rrf import merge_hybrid_hits, reciprocal_rank_fusion


def test_reciprocal_rank_fusion_boosts_overlap():
    fts_results = [(1,), (2,)]
    vec_results = [(2, 0.1), (3, 0.2)]
    fused = reciprocal_rank_fusion(fts_results, vec_results, k=60)
    scores = dict(fused)
    assert scores[2] > scores[1]
    assert scores[2] > scores[3]


def test_merge_hybrid_hits_returns_rrf_scores():
    fts_hits = [
        {"chunk_id": 1, "doc_url": "file:///a.odt", "para_index": 0, "snippet": "alpha", "score": -1.0},
        {"chunk_id": 2, "doc_url": "file:///b.odt", "para_index": 1, "snippet": "beta", "score": -2.0},
    ]
    vec_hits = [
        {"chunk_id": 2, "doc_url": "file:///b.odt", "para_index": 1, "snippet": "beta", "distance": 0.2, "score": 0.8},
        {"chunk_id": 3, "doc_url": "file:///c.odt", "para_index": 0, "snippet": "gamma", "distance": 0.3, "score": 0.7},
    ]
    hits = merge_hybrid_hits(fts_hits, vec_hits, k=2, rrf_k=60)
    assert len(hits) == 2
    assert hits[0]["chunk_id"] == 2
    assert hits[0]["matched_by"] == ["fts", "vec"]
    assert hits[0]["score"] > hits[1]["score"]
