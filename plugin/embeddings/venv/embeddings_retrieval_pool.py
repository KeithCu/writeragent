# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared (final_k, fetch_k) sizing for hybrid retrieve + rerank pipelines."""
from __future__ import annotations

_MAX_FINAL_K = 30
_MIN_FETCH_K = 20
_FETCH_MULTIPLIER = 4
_MAX_FETCH_K = 50


def hybrid_retrieval_pool(k: int) -> tuple[int, int]:
    """Return (final_k, fetch_k) for over-retrieval before fusion truncate / cross-encoder rerank."""
    final_k = max(1, min(int(k or 10), _MAX_FINAL_K))
    fetch_k = min(max(final_k * _FETCH_MULTIPLIER, _MIN_FETCH_K), _MAX_FETCH_K)
    return final_k, fetch_k


__all__ = [
    "_FETCH_MULTIPLIER",
    "_MAX_FETCH_K",
    "_MAX_FINAL_K",
    "_MIN_FETCH_K",
    "hybrid_retrieval_pool",
]
