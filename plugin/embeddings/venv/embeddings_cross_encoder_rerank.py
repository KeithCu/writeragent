# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Cross-encoder rerank for hybrid candidate dicts (no LlamaIndex dependency)."""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

_DEFAULT_RERANK_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"
_CROSS_ENCODER_CACHE: dict[str, Any] = {}


def _resolve_rerank_model(rerank_model: str | None) -> str:
    model = str(rerank_model or "").strip()
    return model or _DEFAULT_RERANK_MODEL


def _get_cross_encoder(model_name: str) -> Any:
    cached = _CROSS_ENCODER_CACHE.get(model_name)
    if cached is not None:
        return cached
    import importlib

    from plugin.embeddings.venv.embeddings_index import EMBEDDINGS_VENV_PIP_INSTALL

    try:
        st_mod = importlib.import_module("sentence_transformers")
    except ImportError as exc:
        raise ImportError(
            f"sentence-transformers is not installed in the configured Python venv. "
            f"Install with: {EMBEDDINGS_VENV_PIP_INSTALL}"
        ) from exc
    encoder = st_mod.CrossEncoder(model_name)
    _CROSS_ENCODER_CACHE[model_name] = encoder
    return encoder


def cross_encoder_rerank_candidates(
    query: str,
    candidates: list[dict[str, Any]],
    *,
    model: str,
    top_n: int,
) -> list[dict[str, Any]]:
    """Score (query, snippet) pairs with a cross-encoder; return top_n by score."""
    if not candidates:
        return []
    final_n = max(1, int(top_n or 1))
    if final_n <= 1 or len(candidates) <= 1:
        return candidates[:final_n]

    query_text = str(query or "").strip()
    if not query_text:
        return candidates[:final_n]

    model_name = _resolve_rerank_model(model)
    pairs: list[tuple[str, str]] = []
    indexed: list[dict[str, Any]] = []
    for cand in candidates:
        snippet = str(cand.get("snippet") or "").strip()
        if not snippet:
            continue
        pairs.append((query_text, snippet))
        indexed.append(cand)

    if not indexed:
        return candidates[:final_n]

    try:
        encoder = _get_cross_encoder(model_name)
        scores = encoder.predict(pairs)
    except Exception:
        log.exception("CrossEncoder rerank failed; using fused top-k")
        return candidates[:final_n]

    reranked: list[dict[str, Any]] = []
    for cand, raw_score in zip(indexed, scores, strict=False):
        merged = dict(cand)
        merged["score"] = float(raw_score)
        reranked.append(merged)
    reranked.sort(key=lambda row: float(row.get("score") or 0.0), reverse=True)
    return reranked[:final_n]


__all__ = ["cross_encoder_rerank_candidates"]
