# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Hybrid corpus search: FTS + vec0 legs fused with vendored RRF."""
from __future__ import annotations

from typing import Any

from plugin.embeddings.venv.embeddings_cross_encoder_rerank import cross_encoder_rerank_candidates
from plugin.embeddings.venv.embeddings_parent_hits import expand_candidates_to_parent_paragraphs
from plugin.embeddings.venv.embeddings_index import embed_texts
from plugin.embeddings.venv.embeddings_search_graph import (
    _public_hit_from_candidate,
)
from plugin.embeddings.venv.embeddings_sqlite import (
    connect_corpus_db,
    fts_corpus_search,
    load_embeddings_for_candidates,
    vec0_search,
)
from plugin.embeddings.venv.embeddings_retrieval_pool import hybrid_retrieval_pool
from plugin.embeddings.venv.hybrid_rrf import merge_hybrid_hits

_DEFAULT_RRF_K = 60


def hybrid_corpus_search(
    db_path: str,
    query_text: str,
    k: int,
    *,
    model_name: str,
    near_slop: int = 10,
    rrf_k: int = _DEFAULT_RRF_K,
    doc_url_filter: str | None = None,
    use_mmr: bool = True,
    rerank_model: str | None = None,
) -> dict[str, Any]:
    """Run FTS + semantic search on corpus.db; fuse with reciprocal_rank_fusion."""
    model = (model_name or "").strip()
    if not model:
        raise ValueError("embedding model name is required")
    query = str(query_text or "").strip()
    if not query:
        return {"hits": []}

    final_k, fetch_k = hybrid_retrieval_pool(k)

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
        load_embeddings_for_candidates(conn, vec_hits, model=model)

        if doc_url_filter:
            allowed = str(doc_url_filter)
            fts_hits = [h for h in fts_hits if str(h.get("doc_url") or "") == allowed]
            vec_hits = [h for h in vec_hits if str(h.get("doc_url") or "") == allowed]

        fused = merge_hybrid_hits(fts_hits, vec_hits, k=fetch_k, rrf_k=rrf_k)
        fused = expand_candidates_to_parent_paragraphs(str(db_path), fused)

        rerank_id = str(rerank_model or "").strip()
        if use_mmr and rerank_id and fused and final_k > 1:
            fused = cross_encoder_rerank_candidates(
                query,
                fused,
                model=rerank_id,
                top_n=final_k,
            )
        elif use_mmr and fused and final_k > 1:
            # MMR diversity rerank — disabled for fair cross-encoder experiment (2026-06).
            # Re-enable if we want hybrid rerank + diversity instead of LI-style rerank-only.
            '''
            load_embeddings_for_candidates(conn, fused, model=model)
            with_embeddings = [c for c in fused if c.get("embedding") is not None]
            without = [c for c in fused if c.get("embedding") is None]
            if len(with_embeddings) >= final_k:
                import numpy as np
                from plugin.embeddings.venv.embeddings_search_graph import MMR_LAMBDA, _max_marginal_relevance

                fused = _max_marginal_relevance(
                    np.asarray(query_vec, dtype=np.float32),
                    [c["embedding"] for c in with_embeddings],
                    with_embeddings,
                    final_k,
                    lambda_mult=MMR_LAMBDA,
                )
                if len(fused) < final_k:
                    for cand in without:
                        if len(fused) >= final_k:
                            break
                        if cand not in fused:
                            fused.append(cand)
            else:
                fused = fused[:final_k]
            '''
            fused = fused[:final_k]
        else:
            fused = fused[:final_k]
    finally:
        conn.close()

    hits: list[dict[str, Any]] = []
    for row in fused:
        cand = dict(row)
        hit = _public_hit_from_candidate(cand)
        if row.get("matched_by"):
            hit["matched_by"] = list(row["matched_by"])
        hits.append(hit)

    return {"hits": hits}


__all__ = ["hybrid_corpus_search"]
