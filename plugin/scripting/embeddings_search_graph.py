# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""LangGraph search pipeline: embed query → Chroma retrieve → MMR rerank (trusted venv)."""
from __future__ import annotations

import logging
from typing import Any, TypedDict

from plugin.scripting.embeddings_chroma import get_collection
from plugin.scripting.embeddings_index import embed_texts

log = logging.getLogger(__name__)

MMR_LAMBDA = 0.7


class SearchState(TypedDict, total=False):
    persist_dir: str
    collection_name: str
    model: str
    query: str
    k: int
    doc_url_filter: str | None
    query_vec: list[float]
    candidates: list[dict[str, Any]]
    hits: list[dict[str, Any]]


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


def chroma_retrieve(state: SearchState) -> dict[str, Any]:
    query_vec = state.get("query_vec") or []
    if not query_vec:
        return {"candidates": []}

    k = max(1, min(int(state.get("k") or 5), 50))
    n_results = min(max(k * 3, k), 100)
    collection = get_collection(str(state["persist_dir"]), str(state["collection_name"]))
    try:
        count = int(collection.count())
    except Exception:
        count = 0
    if count == 0:
        return {"candidates": []}
    n_results = min(n_results, count)

    result = collection.query(
        query_embeddings=[query_vec],
        n_results=n_results,
        include=["metadatas", "distances", "embeddings"],
    )
    ids = (result.get("ids") or [[]])[0]
    metadatas = (result.get("metadatas") or [[]])[0]
    distances = (result.get("distances") or [[]])[0]
    embeddings = (result.get("embeddings") or [[]])[0]

    candidates: list[dict[str, Any]] = []
    for i, cid in enumerate(ids):
        meta = metadatas[i] if i < len(metadatas) else {}
        dist = float(distances[i]) if i < len(distances) else 1.0
        emb = embeddings[i] if i < len(embeddings) else None
        score = max(0.0, 1.0 - dist)
        candidates.append(
            {
                "chunk_id": str(cid),
                "doc_url": str((meta or {}).get("doc_url") or ""),
                "para_index": int((meta or {}).get("para_index") or 0),
                "char_start": int((meta or {}).get("char_start") or 0),
                "char_end": int((meta or {}).get("char_end") or 0),
                "embedding_model": str((meta or {}).get("embedding_model") or ""),
                "score": score,
                "embedding": emb,
            }
        )
    return {"candidates": candidates}


def metadata_filter(state: SearchState) -> dict[str, Any]:
    model = str(state.get("model") or "")
    doc_filter = state.get("doc_url_filter")
    filtered: list[dict[str, Any]] = []
    for cand in state.get("candidates") or []:
        if doc_filter and cand.get("doc_url") != doc_filter:
            continue
        emb_model = str(cand.get("embedding_model") or "")
        if emb_model and emb_model != model:
            continue
        filtered.append(cand)
    return {"candidates": filtered}


def _max_marginal_relevance(
    query_vec: Any,
    candidate_embeddings: list[Any],
    candidates: list[dict[str, Any]],
    k: int,
    lambda_mult: float = MMR_LAMBDA,
) -> list[dict[str, Any]]:
    """Pure NumPy MMR — reduce redundant overlapping sub-chunks."""
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
        hits.append(
            {
                "chunk_id": cand.get("chunk_id"),
                "doc_url": cand.get("doc_url"),
                "para_index": cand.get("para_index"),
                "char_start": cand.get("char_start"),
                "char_end": cand.get("char_end"),
                "score": float(cand.get("score") or 0.0),
            }
        )
    if len(hits) < k and without:
        for cand in without:
            if len(hits) >= k:
                break
            hits.append(
                {
                    "chunk_id": cand.get("chunk_id"),
                    "doc_url": cand.get("doc_url"),
                    "para_index": cand.get("para_index"),
                    "char_start": cand.get("char_start"),
                    "char_end": cand.get("char_end"),
                    "score": float(cand.get("score") or 0.0),
                }
            )
    return {"hits": hits}


def format_hits(state: SearchState) -> dict[str, Any]:
    return {"hits": list(state.get("hits") or [])}


def _build_search_graph() -> Any:
    import importlib

    lg = importlib.import_module("langgraph.graph")
    graph = lg.StateGraph(SearchState)
    graph.add_node("embed_query", embed_query)
    graph.add_node("chroma_retrieve", chroma_retrieve)
    graph.add_node("metadata_filter", metadata_filter)
    graph.add_node("rerank", rerank)
    graph.add_node("format_hits", format_hits)
    graph.add_edge(lg.START, "embed_query")
    graph.add_edge("embed_query", "chroma_retrieve")
    graph.add_edge("chroma_retrieve", "metadata_filter")
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
    persist_dir: str,
    collection_name: str,
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
        "persist_dir": str(persist_dir),
        "collection_name": str(collection_name),
        "model": model,
        "query": query,
        "k": int(k or 5),
        "doc_url_filter": doc_url_filter,
    }
    final = _get_search_graph().invoke(initial)
    return {"hits": list(final.get("hits") or [])}


__all__ = ["search_embeddings_graph"]
