# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Host-side embeddings index RPC — sqlite-vec + LangGraph in the venv worker."""

from __future__ import annotations

from typing import Any, Callable

from plugin.embeddings.embedding_client import _embedding_session_id
from plugin.framework.constants import WORKER_POOL_EMBEDDINGS
from plugin.framework.errors import ToolExecutionError
from plugin.scripting.config_limits import embeddings_worker_timeout_sec
from plugin.scripting.trusted_rpc import run_trusted_worker_action


def _folder_search_mode() -> str:
    """Read Settings cross-file search mode for index/search RPC routing."""
    from plugin.framework.config import get_config

    return str(get_config("embeddings.folder_search_mode") or "none").strip().lower()


_FOLDER_SEARCH_MODES = ("hybrid", "llama_index", "fts", "embeddings", "zvec", "lancedb")


def _resolved_folder_search_mode(search_mode: str | None) -> str:
    """Settings mode, or an explicit override, restricted to known backends."""
    resolved = str(search_mode or _folder_search_mode() or "hybrid").strip().lower()
    if resolved not in _FOLDER_SEARCH_MODES:
        return "hybrid"
    return resolved


def _folder_search_rerank_options(search_mode: str) -> dict[str, Any]:
    """Build use_mmr / rerank_model for search RPC from Settings and backend mode."""
    from plugin.framework.constants import folder_rerank_enabled, resolve_folder_rerank_model

    if search_mode in ("hybrid", "llama_index", "zvec", "lancedb"):
        if folder_rerank_enabled():
            return {"use_mmr": True, "rerank_model": resolve_folder_rerank_model()}
        return {"use_mmr": False}
    return {"use_mmr": True}


def _run_embeddings_action(
    ctx: Any,
    helper: str,
    params: dict[str, Any],
    *,
    model: str,
    allow_heartbeat: bool = False,
    heartbeat_fn: Callable[[dict[str, Any]], None] | None = None,
    stop_checker: Callable[[], bool] | None = None,
    cancellation_scope: Any | None = None,
) -> dict[str, Any]:
    timeout_sec = embeddings_worker_timeout_sec()
    res = run_trusted_worker_action(
        ctx, domain="embeddings_index", helper=helper, params=params, session_id=_embedding_session_id(model), timeout_sec=timeout_sec, worker_pool=WORKER_POOL_EMBEDDINGS, allow_heartbeat=allow_heartbeat, heartbeat_fn=heartbeat_fn, error_code="EMBEDDING_INDEX_ERROR", error_label="Embeddings", stop_checker=stop_checker, cancellation_scope=cancellation_scope
    )
    if isinstance(res, dict) and res.get("error"):
        from plugin.framework.errors import ToolExecutionError
        raise ToolExecutionError(str(res["error"]), code="EMBEDDING_INDEX_ERROR")
    return res


def maintain_folder_index(ctx: Any, listing_root: str, *, model: str, mode: str = "auto", search_mode: str | None = None, heartbeat_fn: Callable[[dict[str, Any]], None] | None = None, stop_checker: Callable[[], bool] | None = None, cancellation_scope: Any | None = None) -> dict[str, Any]:
    """Run full folder corpus maintenance in the embeddings venv.
    Optional heartbeat_fn will receive per‑file progress dicts.
    """
    model_name = (model or "").strip()
    if not model_name:
        raise ToolExecutionError("No embedding model configured.", code="EMBEDDING_MODEL_MISSING")
    resolved_mode = _resolved_folder_search_mode(search_mode)
    return _run_embeddings_action(ctx, "maintain_folder_index", {"listing_root": str(listing_root), "model": model_name, "mode": str(mode or "auto"), "search_mode": resolved_mode}, model=model_name or "corpus", allow_heartbeat=True, heartbeat_fn=heartbeat_fn, stop_checker=stop_checker, cancellation_scope=cancellation_scope)


def index_paragraphs(ctx: Any, db_path: str, meta_path: str, rows: list[dict[str, Any]], *, model: str, build_fts: bool = False, build_vectors: bool = True, search_mode: str | None = None, stop_checker: Callable[[], bool] | None = None, cancellation_scope: Any | None = None) -> dict[str, Any]:
    """Persist paragraph rows + vectors via the warm venv worker."""
    model_name = (model or "").strip()
    if build_vectors and not model_name:
        raise ToolExecutionError("No embedding model configured.", code="EMBEDDING_MODEL_MISSING")
    # Pass search_mode. Without it, dispatch uses the sqlite default and a
    # configured zvec, LanceDB, or LlamaIndex backend never sees the rows.
    resolved_mode = _resolved_folder_search_mode(search_mode)
    return _run_embeddings_action(ctx, "index_paragraphs", {"db_path": str(db_path), "meta_path": str(meta_path), "model": model_name, "rows": list(rows or []), "build_fts": build_fts, "build_vectors": build_vectors, "search_mode": resolved_mode}, model=model_name or "corpus", stop_checker=stop_checker, cancellation_scope=cancellation_scope)


def delete_paragraphs(ctx: Any, db_path: str, meta_path: str, keys: list[dict[str, Any]], *, model: str, build_fts: bool = False, build_vectors: bool = True, search_mode: str | None = None, stop_checker: Callable[[], bool] | None = None, cancellation_scope: Any | None = None) -> dict[str, Any]:
    """Remove paragraph index rows via the warm venv worker."""
    model_name = (model or "").strip()
    resolved_mode = _resolved_folder_search_mode(search_mode)
    return _run_embeddings_action(ctx, "delete_paragraphs", {"db_path": str(db_path), "meta_path": str(meta_path), "keys": list(keys or []), "model": model_name, "build_fts": build_fts, "build_vectors": build_vectors, "search_mode": resolved_mode}, model=model_name or "corpus", stop_checker=stop_checker, cancellation_scope=cancellation_scope)


def hybrid_search(ctx: Any, db_path: str, query: str, k: int, *, model: str, near_slop: int = 10, doc_url_filter: str | None = None, stop_checker: Callable[[], bool] | None = None, cancellation_scope: Any | None = None) -> dict[str, Any]:
    """Hybrid FTS + semantic search over corpus.db via the warm venv worker."""
    search_mode = _folder_search_mode()
    model_name = (model or "").strip()
    if not model_name:
        raise ToolExecutionError("No embedding model configured.", code="EMBEDDING_MODEL_MISSING")
    return _run_embeddings_action(
        ctx, "hybrid_search", {"db_path": str(db_path), "query": str(query or ""), "k": int(k or 10), "model": model_name, "near_slop": int(near_slop), "doc_url_filter": doc_url_filter, "search_mode": search_mode, **_folder_search_rerank_options(search_mode)}, model=model_name, stop_checker=stop_checker, cancellation_scope=cancellation_scope
    )


def knn_search(ctx: Any, db_path: str, query: str, k: int, *, model: str, doc_url_filter: str | None = None, stop_checker: Callable[[], bool] | None = None, cancellation_scope: Any | None = None) -> dict[str, Any]:
    """Semantic search over a folder corpus.db via the warm venv worker."""
    search_mode = _folder_search_mode()
    model_name = (model or "").strip()
    if not model_name:
        raise ToolExecutionError("No embedding model configured.", code="EMBEDDING_MODEL_MISSING")
    return _run_embeddings_action(ctx, "knn_search", {"db_path": str(db_path), "query": str(query or ""), "k": int(k or 5), "model": model_name, "doc_url_filter": doc_url_filter, "search_mode": search_mode, **_folder_search_rerank_options(search_mode)}, model=model_name, stop_checker=stop_checker, cancellation_scope=cancellation_scope)


def collection_stats(ctx: Any, db_path: str, meta_path: str, *, model: str = "", stop_checker: Callable[[], bool] | None = None, cancellation_scope: Any | None = None) -> dict[str, Any]:
    """Lightweight corpus stats for host empty/stale checks."""
    model_name = (model or "").strip()
    return _run_embeddings_action(ctx, "collection_stats", {"db_path": str(db_path), "meta_path": str(meta_path), "model": model_name}, model=model_name or "stats", stop_checker=stop_checker, cancellation_scope=cancellation_scope)


__all__ = ["collection_stats", "delete_paragraphs", "hybrid_search", "index_paragraphs", "knn_search", "maintain_folder_index"]
