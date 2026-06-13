# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Reciprocal Rank Fusion for hybrid FTS + vector search."""
from __future__ import annotations

from typing import Any

# --- Vendored from liamca/sqlite-hybrid-search ---
# Source: sqlite-hybrid-search.ipynb (cell aa9b0d4c-5517-46ff-a745-9aa1726ccf51)
#         https://github.com/liamca/sqlite-hybrid-search/blob/main/sqlite-hybrid-search.ipynb
# Also documented in upstream README.md (Hybrid Search using SQLite → RRF code block).
#
# We vendor reciprocal_rank_fusion verbatim because it matches our stack (sqlite-vec + FTS5)
# and avoids a new pip dependency. WriterAgent adapts corpus.db hit dicts into the tuple
# shapes this function expects; only the function below is copied from upstream.
# --- end vendoring notice ---


def reciprocal_rank_fusion(fts_results, vec_results, k=60):
    rank_dict = {}

    # Process FTS results
    for rank, (id,) in enumerate(fts_results):
        if id not in rank_dict:
            rank_dict[id] = 0
        rank_dict[id] += 1 / (k + rank + 1)

    # Process vector results
    for rank, (rowid, distance) in enumerate(vec_results):
        if rowid not in rank_dict:
            rank_dict[rowid] = 0
        rank_dict[rowid] += 1 / (k + rank + 1)

    # Sort by RRF score
    sorted_results = sorted(rank_dict.items(), key=lambda x: x[1], reverse=True)
    return sorted_results


# WriterAgent — not from upstream


def _fts_results_for_rrf(fts_hits: list[dict[str, Any]]) -> list[tuple[int]]:
    rows: list[tuple[int]] = []
    for hit in fts_hits:
        chunk_id = hit.get("chunk_id")
        if chunk_id is None:
            continue
        rows.append((int(chunk_id),))
    return rows


def _vec_results_for_rrf(vec_hits: list[dict[str, Any]]) -> list[tuple[int, float]]:
    rows: list[tuple[int, float]] = []
    for hit in vec_hits:
        chunk_id = hit.get("chunk_id")
        if chunk_id is None:
            continue
        raw_distance = hit.get("distance")
        if raw_distance is None:
            raw_distance = hit.get("score", 0.0)
        distance = float(raw_distance)
        rows.append((int(chunk_id), distance))
    return rows


def _matched_by(chunk_id: int, fts_ids: set[int], vec_ids: set[int]) -> list[str]:
    matched: list[str] = []
    if chunk_id in fts_ids:
        matched.append("fts")
    if chunk_id in vec_ids:
        matched.append("vec")
    return matched


def merge_hybrid_hits(
    fts_hits: list[dict[str, Any]],
    vec_hits: list[dict[str, Any]],
    *,
    k: int,
    rrf_k: int = 60,
) -> list[dict[str, Any]]:
    """Fuse FTS and vector hit lists on chunk_id; returns top *k* with RRF scores."""
    by_id: dict[int, dict[str, Any]] = {}
    for hit in fts_hits + vec_hits:
        cid = hit.get("chunk_id")
        if cid is None:
            continue
        chunk_id = int(cid)
        if chunk_id not in by_id:
            by_id[chunk_id] = dict(hit)
        else:
            existing = by_id[chunk_id]
            for key, value in hit.items():
                if key == "snippet" and not str(existing.get("snippet") or "").strip():
                    existing[key] = value
                elif key not in existing or existing.get(key) in (None, "", 0):
                    existing[key] = value

    fts_ids = {int(h["chunk_id"]) for h in fts_hits if h.get("chunk_id") is not None}
    vec_ids = {int(h["chunk_id"]) for h in vec_hits if h.get("chunk_id") is not None}

    fused = reciprocal_rank_fusion(
        _fts_results_for_rrf(fts_hits),
        _vec_results_for_rrf(vec_hits),
        k=rrf_k,
    )

    hits: list[dict[str, Any]] = []
    for chunk_id, rrf_score in fused[: max(1, int(k))]:
        base = by_id.get(int(chunk_id))
        if base is None:
            continue
        out = dict(base)
        out["score"] = float(rrf_score)
        out["matched_by"] = _matched_by(int(chunk_id), fts_ids, vec_ids)
        hits.append(out)
    return hits


__all__ = ["merge_hybrid_hits", "reciprocal_rank_fusion"]
