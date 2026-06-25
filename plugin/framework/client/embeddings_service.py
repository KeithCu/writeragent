# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Host-side embeddings index RPC — sqlite-vec + LangGraph in the venv worker."""
from __future__ import annotations

import logging
from typing import Any, Callable

from plugin.framework.client.embedding_client import _embedding_session_id
from plugin.framework.constants import EMBEDDINGS_HEARTBEAT_GRACE_S, WORKER_POOL_EMBEDDINGS
from plugin.framework.errors import ToolExecutionError
from plugin.scripting.config_limits import embeddings_worker_timeout_sec
from plugin.scripting.venv_worker import run_code_in_user_venv

log = logging.getLogger(__name__)


def _folder_search_mode() -> str:
    """Read Settings cross-file search mode for index/search RPC routing."""
    from plugin.framework.config import get_config

    return str(get_config("embeddings.folder_search_mode") or "none").strip().lower()


def _folder_search_rerank_options(search_mode: str) -> dict[str, Any]:
    """Build use_mmr / rerank_model for search RPC from Settings and backend mode."""
    from plugin.framework.constants import folder_rerank_enabled, resolve_folder_rerank_model

    if search_mode in ("hybrid", "llama_index", "zvec", "lancedb"):
        if folder_rerank_enabled():
            return {"use_mmr": True, "rerank_model": resolve_folder_rerank_model()}
        return {"use_mmr": False}
    return {"use_mmr": True}

_INDEX_STUB = """\
from plugin.embeddings.venv.embeddings_index import index_paragraphs as _index
result = _index(
    data["db_path"],
    data["meta_path"],
    data["model"],
    data["rows"],
    build_fts=data.get("build_fts", False),
    build_vectors=data.get("build_vectors", True),
)
"""

_DELETE_STUB = """\
from plugin.embeddings.venv.embeddings_index import delete_paragraphs as _delete
result = _delete(
    data["db_path"],
    data["meta_path"],
    data["keys"],
    model_name=data["model"],
    build_fts=data.get("build_fts", False),
    build_vectors=data.get("build_vectors", True),
)
"""

_HYBRID_SEARCH_STUB = """\
from plugin.embeddings.venv.embeddings_index import hybrid_search as _search
result = _search(
    data["db_path"],
    data["query"],
    data["k"],
    model_name=data["model"],
    near_slop=data.get("near_slop", 10),
    doc_url_filter=data.get("doc_url_filter"),
    search_mode=data.get("search_mode", "hybrid"),
    use_mmr=data.get("use_mmr", True),
    rerank_model=data.get("rerank_model"),
)
"""

_SEARCH_STUB = """\
from plugin.embeddings.venv.embeddings_index import knn_search as _search
result = _search(
    data["db_path"],
    data["query"],
    data["k"],
    model_name=data["model"],
    doc_url_filter=data.get("doc_url_filter"),
    search_mode=data.get("search_mode", "hybrid"),
    use_mmr=data.get("use_mmr", True),
    rerank_model=data.get("rerank_model"),
)
"""

_STATS_STUB = """\
from plugin.embeddings.venv.embeddings_index import collection_stats as _stats
result = _stats(
    data["db_path"],
    data["meta_path"],
    model_name=data.get("model", ""),
)
"""

_MAINTAIN_STUB = """\
from plugin.embeddings.venv.embeddings_index import maintain_folder_index as _maintain
result = _maintain(
    data["listing_root"],
    data["model"],
    data.get("mode", "auto"),
    search_mode=data.get("search_mode", "hybrid"),
)
"""


def _run_worker(ctx: Any, stub: str, payload: dict[str, Any], *, model: str) -> dict[str, Any]:
    timeout_sec = embeddings_worker_timeout_sec(ctx)
    response = run_code_in_user_venv(
        ctx,
        stub,
        data=payload,
        timeout_sec=timeout_sec,
        session_id=_embedding_session_id(model),
        worker_pool=WORKER_POOL_EMBEDDINGS,
    )
    if response.get("status") != "ok":
        message = str(response.get("message") or "Embeddings worker failed.")
        raise ToolExecutionError(message, code="EMBEDDING_INDEX_ERROR", details={"worker": response})
    result = response.get("result")
    if not isinstance(result, dict):
        raise ToolExecutionError(
            "Embeddings worker returned an unexpected result.",
            code="EMBEDDING_INDEX_ERROR",
            details={"result_type": type(result).__name__},
        )
    return result


def _run_worker_with_heartbeat(
    ctx: Any,
    stub: str,
    payload: dict[str, Any],
    *,
    model: str,
    heartbeat_fn: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    timeout_sec = embeddings_worker_timeout_sec(ctx)

    def _on_heartbeat(hb: dict[str, Any]) -> None:
        if heartbeat_fn:
            heartbeat_fn(hb)
        log.debug("embeddings index heartbeat: %s", hb)

    response = run_code_in_user_venv(
        ctx,
        stub,
        data=payload,
        timeout_sec=timeout_sec,
        session_id=_embedding_session_id(model),
        worker_pool=WORKER_POOL_EMBEDDINGS,
        allow_heartbeat=True,
        heartbeat_grace_sec=EMBEDDINGS_HEARTBEAT_GRACE_S,
        on_heartbeat=_on_heartbeat,
    )
    if response.get("status") != "ok":
        message = str(response.get("message") or "Embeddings worker failed.")
        raise ToolExecutionError(message, code="EMBEDDING_INDEX_ERROR", details={"worker": response})
    result = response.get("result")
    if not isinstance(result, dict):
        raise ToolExecutionError(
            "Embeddings worker returned an unexpected result.",
            code="EMBEDDING_INDEX_ERROR",
            details={"result_type": type(result).__name__},
        )
    return result


def maintain_folder_index(
    ctx: Any,
    listing_root: str,
    *,
    model: str,
    mode: str = "auto",
    search_mode: str | None = None,
    heartbeat_fn: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Run full folder corpus maintenance in the embeddings venv.
    Optional heartbeat_fn will receive per‑file progress dicts.
    """
    model_name = (model or "").strip()
    if not model_name:
        raise ToolExecutionError("No embedding model configured.", code="EMBEDDING_MODEL_MISSING")
    resolved_mode = str(search_mode or _folder_search_mode() or "hybrid").strip().lower()
    if resolved_mode not in ("hybrid", "llama_index", "fts", "embeddings", "zvec", "lancedb"):
        resolved_mode = "hybrid"
    return _run_worker_with_heartbeat(
        ctx,
        _MAINTAIN_STUB,
        {
            "listing_root": str(listing_root),
            "model": model_name,
            "mode": str(mode or "auto"),
            "search_mode": resolved_mode,
        },
        model=model_name or "corpus",
        heartbeat_fn=heartbeat_fn,
    )


def index_paragraphs(
    ctx: Any,
    db_path: str,
    meta_path: str,
    rows: list[dict[str, Any]],
    *,
    model: str,
    build_fts: bool = False,
    build_vectors: bool = True,
) -> dict[str, Any]:
    """Persist paragraph rows + vectors via the warm venv worker."""
    model_name = (model or "").strip()
    if build_vectors and not model_name:
        raise ToolExecutionError("No embedding model configured.", code="EMBEDDING_MODEL_MISSING")
    return _run_worker(
        ctx,
        _INDEX_STUB,
        {
            "db_path": str(db_path),
            "meta_path": str(meta_path),
            "model": model_name,
            "rows": list(rows or []),
            "build_fts": build_fts,
            "build_vectors": build_vectors,
        },
        model=model_name or "corpus",
    )


def delete_paragraphs(
    ctx: Any,
    db_path: str,
    meta_path: str,
    keys: list[dict[str, Any]],
    *,
    model: str,
    build_fts: bool = False,
    build_vectors: bool = True,
) -> dict[str, Any]:
    """Remove paragraph index rows via the warm venv worker."""
    model_name = (model or "").strip()
    return _run_worker(
        ctx,
        _DELETE_STUB,
        {
            "db_path": str(db_path),
            "meta_path": str(meta_path),
            "keys": list(keys or []),
            "model": model_name,
            "build_fts": build_fts,
            "build_vectors": build_vectors,
        },
        model=model_name or "corpus",
    )


def hybrid_search(
    ctx: Any,
    db_path: str,
    query: str,
    k: int,
    *,
    model: str,
    near_slop: int = 10,
    doc_url_filter: str | None = None,
) -> dict[str, Any]:
    """Hybrid FTS + semantic search over corpus.db via the warm venv worker."""
    search_mode = _folder_search_mode()
    model_name = (model or "").strip()
    if not model_name:
        raise ToolExecutionError("No embedding model configured.", code="EMBEDDING_MODEL_MISSING")
    payload: dict[str, Any] = {
        "db_path": str(db_path),
        "query": str(query or ""),
        "k": int(k or 10),
        "model": model_name,
        "near_slop": int(near_slop),
        "doc_url_filter": doc_url_filter,
        "search_mode": search_mode,
        **_folder_search_rerank_options(search_mode),
    }
    return _run_worker(ctx, _HYBRID_SEARCH_STUB, payload, model=model_name)


def knn_search(
    ctx: Any,
    db_path: str,
    query: str,
    k: int,
    *,
    model: str,
    doc_url_filter: str | None = None,
) -> dict[str, Any]:
    """Semantic search over a folder corpus.db via the warm venv worker."""
    search_mode = _folder_search_mode()
    model_name = (model or "").strip()
    if not model_name:
        raise ToolExecutionError("No embedding model configured.", code="EMBEDDING_MODEL_MISSING")
    payload: dict[str, Any] = {
        "db_path": str(db_path),
        "query": str(query or ""),
        "k": int(k or 5),
        "model": model_name,
        "doc_url_filter": doc_url_filter,
        "search_mode": search_mode,
        **_folder_search_rerank_options(search_mode),
    }
    return _run_worker(ctx, _SEARCH_STUB, payload, model=model_name)


def collection_stats(
    ctx: Any,
    db_path: str,
    meta_path: str,
    *,
    model: str = "",
) -> dict[str, Any]:
    """Lightweight corpus stats for host empty/stale checks."""
    model_name = (model or "").strip()
    return _run_worker(
        ctx,
        _STATS_STUB,
        {
            "db_path": str(db_path),
            "meta_path": str(meta_path),
            "model": model_name,
        },
        model=model_name or "stats",
    )


__all__ = [
    "collection_stats",
    "delete_paragraphs",
    "hybrid_search",
    "index_paragraphs",
    "knn_search",
    "maintain_folder_index",
]
