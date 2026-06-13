# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Hybrid corpus search: FTS + vec0 legs fused with vendored RRF."""
from __future__ import annotations

from typing import Any

from plugin.embeddings.venv.embeddings_index import embed_texts
from plugin.embeddings.venv.embeddings_search_graph import _hit_snippet, _public_hit_from_candidate
from plugin.embeddings.venv.embeddings_sqlite import (
    connect_corpus_db,
    fts_corpus_search,
    load_embeddings_for_candidates,
    vec0_search,
)
from plugin.embeddings.venv.hybrid_rrf import merge_hybrid_hits

_DEFAULT_POOL_K = 30
_MAX_POOL_K = 50
_DEFAULT_RRF_K = 60


def hybrid_corpus_search(
    db_path: str,
    query_text: str,
    k: int,
    *,
    model_name: str,
    near_slop: int = 10,
    pool_k: int = _DEFAULT_POOL_K,
    rrf_k: int = _DEFAULT_RRF_K,
    doc_url_filter: str | None = None,
) -> dict[str, Any]:
    """Run FTS + semantic search on corpus.db; fuse with reciprocal_rank_fusion."""
    model = (model_name or "").strip()
    if not model:
        raise ValueError("embedding model name is required")
    query = str(query_text or "").strip()
    if not query:
        return {"hits": []}

    final_k = max(1, min(int(k or 10), 30))
    fetch_k = max(final_k, min(int(pool_k or _DEFAULT_POOL_K), _MAX_POOL_K))

    encoded = embed_texts(model, [query])
    vectors = encoded.get("vectors") or []
    if not vectors:
        return {"hits": []}
    query_vec = vectors[0]

    conn = connect_corpus_db(str(db_path))
    try:
        fts_hits = fts_corpus_search(conn, query, k=fetch_k, near_slop=near_slop)
        vec_hits = vec0_search(
            conn,
            query_vec,
            k=fetch_k,
            model=model,
            doc_url_filter=doc_url_filter,
        )
        load_embeddings_for_candidates(conn, vec_hits)
    finally:
        conn.close()

    if doc_url_filter:
        allowed = str(doc_url_filter)
        fts_hits = [h for h in fts_hits if str(h.get("doc_url") or "") == allowed]
        vec_hits = [h for h in vec_hits if str(h.get("doc_url") or "") == allowed]

    fused = merge_hybrid_hits(fts_hits, vec_hits, k=final_k, rrf_k=rrf_k)

    hits: list[dict[str, Any]] = []
    for row in fused:
        cand = dict(row)
        cand["snippet"] = _hit_snippet(str(cand.get("snippet") or ""))
        hit = _public_hit_from_candidate(cand)
        if row.get("matched_by"):
            hit["matched_by"] = list(row["matched_by"])
        hits.append(hit)

    return {"hits": hits}


__all__ = ["hybrid_corpus_search"]
