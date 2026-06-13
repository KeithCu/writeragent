# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""LangGraph search pipeline: embed query → vec0 retrieve → MMR rerank (trusted venv)."""
from __future__ import annotations

import logging
from typing import Any, NotRequired, TypedDict

from plugin.embeddings.venv.embeddings_ingest_graph import CHUNK_SIZE
from plugin.embeddings.venv.embeddings_index import embed_texts
from plugin.embeddings.venv.embeddings_sqlite import (
    connect_corpus_db,
    load_embeddings_for_candidates,
    vec0_search,
)

log = logging.getLogger(__name__)

MMR_LAMBDA = 0.7
SNIPPET_MAX_CHARS = CHUNK_SIZE


def _hit_snippet(text: str, *, max_chars: int = SNIPPET_MAX_CHARS) -> str:
    """Normalize embedded chunk text for search_embeddings hits (full chunk up to CHUNK_SIZE)."""
    cleaned = " ".join(str(text or "").split())
    if not cleaned:
        return ""
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[: max_chars - 1] + "…"


def _public_hit_from_candidate(cand: dict[str, Any]) -> dict[str, Any]:
    """Tool-facing hit: file router + snippet; no char offsets (ODF-local, not LO-native)."""
    return {
        "chunk_id": cand.get("chunk_id"),
        "doc_url": cand.get("doc_url"),
        "para_index": cand.get("para_index"),
        "snippet": _hit_snippet(str(cand.get("snippet") or "")),
        "score": float(cand.get("score") or 0.0),
    }


class SearchState(TypedDict):
    db_path: str
    model: str
    query: str
    k: int
    doc_url_filter: NotRequired[str | None]
    query_vec: NotRequired[list[float]]
    candidates: NotRequired[list[dict[str, Any]]]
    hits: NotRequired[list[dict[str, Any]]]


def embed_query(state: SearchState) -> dict[str, Any]:
    model = str(state.get("model") or "")
    query = str(state.get("query") or "").strip()
    if not query:
        return {"query_vec": []}
    encoded = embed_texts(model, [query])
    vectors = encoded.get("vectors") or []
    if not vectors:
        return {"query_vec": []}
    return {"query_vec": vectors[0]}


def vec0_retrieve(state: SearchState) -> dict[str, Any]:
    query_vec = state.get("query_vec") or []
    if not query_vec:
        return {"candidates": []}

    k = max(1, min(int(state.get("k") or 5), 50))
    n_results = min(max(k * 3, k), 100)
    model = str(state.get("model") or "")
    doc_filter = state.get("doc_url_filter")

    conn = connect_corpus_db(str(state["db_path"]))
    try:
        candidates = vec0_search(
            conn,
            query_vec,
            k=n_results,
            model=model,
            doc_url_filter=doc_filter,
        )
        load_embeddings_for_candidates(conn, candidates)
    finally:
        conn.close()
    return {"candidates": candidates}


def metadata_filter(state: SearchState) -> dict[str, Any]:
    model = state.get("model")
    candidates = state.get("candidates") or []
    if model:
        filtered = [c for c in candidates if c.get("embedding_model") == model]
        return {"candidates": filtered}
    return {"candidates": list(candidates)}


# Maximal Marginal Relevance (MMR): after RRF (or vec retrieve) we often have several
# chunks that score high but say almost the same thing (e.g. blog_draft_* vs partN.odt
# siblings). MMR re-ranks the candidate pool greedily: each pick maximizes
#   λ * sim(query, chunk) − (1−λ) * max sim(chunk, already_selected)
# so later hits stay on-topic but are not near-duplicates of earlier ones.
# λ=MMR_LAMBDA (0.7) favors relevance over diversity. Skipped when k=1 (top-1 routing
# should stay the best RRF hit, not a diversity pick) — see hybrid_corpus_search.
def _max_marginal_relevance(
    query_vec: Any,
    candidate_embeddings: list[Any],
    candidates: list[dict[str, Any]],
    k: int,
    lambda_mult: float = MMR_LAMBDA,
) -> list[dict[str, Any]]:
    """Greedy MMR rerank over *candidates* using pre-loaded embeddings."""
    import numpy as np

    if not candidates:
        return []
    if len(candidates) <= k:
        return list(candidates)

    q = np.asarray(query_vec, dtype=np.float32)
    matrix = np.stack([np.asarray(e, dtype=np.float32) for e in candidate_embeddings])
    query_sim = np.clip(matrix @ q, -1.0, 1.0)

    selected: list[int] = []
    remaining = list(range(len(candidates)))
    while remaining and len(selected) < k:
        if not selected:
            best = int(remaining[int(np.argmax(query_sim[remaining]))])
            selected.append(best)
            remaining.remove(best)
            continue
        mmr_scores: list[float] = []
        for idx in remaining:
            redundancy = max(float(matrix[idx] @ matrix[s]) for s in selected)
            mmr = lambda_mult * float(query_sim[idx]) - (1.0 - lambda_mult) * redundancy
            mmr_scores.append(mmr)
        pick = remaining[int(np.argmax(mmr_scores))]
        selected.append(pick)
        remaining.remove(pick)

    return [candidates[i] for i in selected]


def rerank(state: SearchState) -> dict[str, Any]:
    candidates = list(state.get("candidates") or [])
    k = max(1, min(int(state.get("k") or 5), 50))
    query_vec = state.get("query_vec") or []
    if not candidates or not query_vec:
        return {"hits": candidates[:k]}

    with_embeddings = [c for c in candidates if c.get("embedding") is not None]
    without = [c for c in candidates if c.get("embedding") is None]
    if len(with_embeddings) >= k:
        import numpy as np

        reranked = _max_marginal_relevance(
            np.asarray(query_vec, dtype=np.float32),
            [c["embedding"] for c in with_embeddings],
            with_embeddings,
            k,
        )
    else:
        reranked = candidates[:k]

    hits: list[dict[str, Any]] = []
    for cand in reranked[:k]:
        hits.append(_public_hit_from_candidate(cand))
    if len(hits) < k and without:
        for cand in without:
            if len(hits) >= k:
                break
            hits.append(_public_hit_from_candidate(cand))
    return {"hits": hits}


def format_hits(state: SearchState) -> dict[str, Any]:
    return {"hits": list(state.get("hits") or [])}


def _build_search_graph() -> Any:
    import importlib
    from typing import cast

    lg = importlib.import_module("langgraph.graph")
    graph = lg.StateGraph(cast("Any", SearchState))
    graph.add_node("embed_query", embed_query)
    graph.add_node("vec0_retrieve", vec0_retrieve)
    graph.add_node("metadata_filter", metadata_filter)
    graph.add_node("rerank", rerank)
    graph.add_node("format_hits", format_hits)
    graph.add_edge(lg.START, "embed_query")
    graph.add_edge("embed_query", "vec0_retrieve")
    graph.add_edge("vec0_retrieve", "metadata_filter")
    graph.add_edge("metadata_filter", "rerank")
    graph.add_edge("rerank", "format_hits")
    graph.add_edge("format_hits", lg.END)
    return graph.compile()


_SEARCH_GRAPH: Any | None = None


def _get_search_graph() -> Any:
    global _SEARCH_GRAPH
    if _SEARCH_GRAPH is None:
        _SEARCH_GRAPH = _build_search_graph()
    return _SEARCH_GRAPH


def search_embeddings_graph(
    db_path: str,
    query_text: str,
    k: int,
    *,
    model_name: str,
    doc_url_filter: str | None = None,
) -> dict[str, Any]:
    """Run the LangGraph search pipeline; returns tool-compatible hit dicts."""
    model = (model_name or "").strip()
    if not model:
        raise ValueError("embedding model name is required")
    query = str(query_text or "").strip()
    if not query:
        return {"hits": []}

    initial: SearchState = {
        "db_path": str(db_path),
        "model": model,
        "query": query,
        "k": int(k or 5),
        "doc_url_filter": doc_url_filter,
    }
    final = _get_search_graph().invoke(initial)
    return {"hits": list(final.get("hits") or [])}


__all__ = ["search_embeddings_graph"]
