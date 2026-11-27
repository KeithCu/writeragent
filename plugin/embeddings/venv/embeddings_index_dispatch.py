# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Trusted venv dispatch for embeddings index maintain/search/index RPC."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable


def dispatch_trusted(data: dict[str, Any], *, heartbeat_fn: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    """Route embeddings_index trusted actions to embeddings_index compute functions."""
    helper = str(data.get("helper") or "")
    params = data.get("params") or {}
    if not isinstance(params, dict):
        params = {}

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
    if helper == "maintain_folder_index":
        from typing import cast, get_args

        from plugin.embeddings.venv.embeddings_folder_maintain import MaintainMode, maintain_folder_index

        mode = str(params.get("mode") or "auto")
        resolved_mode = cast("MaintainMode", mode if mode in get_args(MaintainMode) else "auto")
        return maintain_folder_index(
            str(params.get("listing_root") or ""),
            embedding_model=str(params.get("model") or ""),
            mode=resolved_mode,
            search_mode=str(params.get("search_mode") or "hybrid"),
            heartbeat_fn=heartbeat_fn,
        )
    if helper == "warm_embedder":
        from plugin.embeddings.venv.embeddings_index import get_embedder

        model = str(params.get("model") or "").strip()
        if not model:
            raise ValueError("model is required")
        get_embedder(model)
        return {"status": "warmed", "model": model}

    raise ValueError(f"Unknown embeddings_index helper: {helper}")
