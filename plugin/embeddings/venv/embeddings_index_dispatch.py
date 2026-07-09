# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Trusted venv dispatch for embeddings index maintain/search/index RPC."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def dispatch_trusted(data: dict[str, Any], *, heartbeat_fn: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    """Route embeddings_index trusted actions to embeddings_index compute functions."""
    helper = str(data.get("helper") or "")
    params = data.get("params") or {}
    if not isinstance(params, dict):
        params = {}

    if helper == "index_paragraphs":
        from plugin.embeddings.venv.embeddings_index import index_paragraphs

        return index_paragraphs(
            str(params["db_path"]),
            str(params["meta_path"]),
            str(params["model"]),
            list(params.get("rows") or []),
            build_fts=bool(params.get("build_fts", False)),
            build_vectors=bool(params.get("build_vectors", True)),
        )
    if helper == "delete_paragraphs":
        from plugin.embeddings.venv.embeddings_index import delete_paragraphs

        return delete_paragraphs(
            str(params["db_path"]),
            str(params["meta_path"]),
            list(params.get("keys") or []),
            model_name=str(params.get("model") or ""),
            build_fts=bool(params.get("build_fts", False)),
            build_vectors=bool(params.get("build_vectors", True)),
        )
    if helper == "hybrid_search":
        from plugin.embeddings.venv.embeddings_index import hybrid_search

        return hybrid_search(
            str(params["db_path"]),
            str(params.get("query") or ""),
            int(params.get("k") or 10),
            model_name=str(params["model"]),
            near_slop=int(params.get("near_slop", 10)),
            doc_url_filter=params.get("doc_url_filter"),
            use_mmr=bool(params.get("use_mmr", True)),
            rerank_model=params.get("rerank_model"),
            search_mode=str(params.get("search_mode", "hybrid")),
        )
    if helper == "knn_search":
        from plugin.embeddings.venv.embeddings_index import knn_search

        return knn_search(
            str(params["db_path"]),
            str(params.get("query") or ""),
            int(params.get("k") or 5),
            model_name=str(params["model"]),
            doc_url_filter=params.get("doc_url_filter"),
            use_mmr=bool(params.get("use_mmr", True)),
            rerank_model=params.get("rerank_model"),
            search_mode=str(params.get("search_mode", "hybrid")),
        )
    if helper == "collection_stats":
        from plugin.embeddings.venv.embeddings_index import collection_stats

        return collection_stats(
            str(params["db_path"]),
            str(params["meta_path"]),
            model_name=str(params.get("model") or ""),
        )
    if helper == "maintain_folder_index":
        from plugin.embeddings.venv.embeddings_index import maintain_folder_index

        return maintain_folder_index(
            str(params.get("listing_root") or ""),
            str(params.get("model") or ""),
            str(params.get("mode") or "auto"),
            search_mode=str(params.get("search_mode") or "hybrid"),
            heartbeat_fn=heartbeat_fn,
        )

    raise ValueError(f"Unknown embeddings_index helper: {helper}")
