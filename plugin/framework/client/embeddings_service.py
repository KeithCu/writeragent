# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Host-side embeddings index RPC — Chroma + LangGraph in the venv worker."""
from __future__ import annotations

from typing import Any

from plugin.framework.client.embedding_client import _embedding_session_id
from plugin.framework.constants import WORKER_POOL_EMBEDDINGS
from plugin.framework.errors import ToolExecutionError
from plugin.scripting.config_limits import configured_python_exec_timeout
from plugin.scripting.venv_worker import run_code_in_user_venv

_INDEX_STUB = """\
from plugin.scripting.embeddings_index import index_paragraphs as _index
result = _index(
    data["persist_dir"],
    data["collection_name"],
    data["meta_path"],
    data["model"],
    data["rows"],
)
"""

_DELETE_STUB = """\
from plugin.scripting.embeddings_index import delete_paragraphs as _delete
result = _delete(
    data["persist_dir"],
    data["collection_name"],
    data["meta_path"],
    data["keys"],
    model_name=data["model"],
)
"""

_SEARCH_STUB = """\
from plugin.scripting.embeddings_index import knn_search as _search
result = _search(
    data["persist_dir"],
    data["collection_name"],
    data["query"],
    data["k"],
    model_name=data["model"],
    doc_url_filter=data.get("doc_url_filter"),
)
"""

_STATS_STUB = """\
from plugin.scripting.embeddings_index import collection_stats as _stats
result = _stats(
    data["persist_dir"],
    data["collection_name"],
    data["meta_path"],
    model_name=data.get("model", ""),
)
"""


def _run_worker(ctx: Any, stub: str, payload: dict[str, Any], *, model: str) -> dict[str, Any]:
    timeout_sec = configured_python_exec_timeout(ctx)
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


def index_paragraphs(
    ctx: Any,
    persist_dir: str,
    collection_name: str,
    meta_path: str,
    rows: list[dict[str, Any]],
    *,
    model: str,
) -> dict[str, Any]:
    """Persist paragraph rows + vectors via the warm venv worker."""
    model_name = (model or "").strip()
    if not model_name:
        raise ToolExecutionError("No embedding model configured.", code="EMBEDDING_MODEL_MISSING")
    return _run_worker(
        ctx,
        _INDEX_STUB,
        {
            "persist_dir": str(persist_dir),
            "collection_name": str(collection_name),
            "meta_path": str(meta_path),
            "model": model_name,
            "rows": list(rows or []),
        },
        model=model_name,
    )


def delete_paragraphs(
    ctx: Any,
    persist_dir: str,
    collection_name: str,
    meta_path: str,
    keys: list[dict[str, Any]],
    *,
    model: str,
) -> dict[str, Any]:
    """Remove paragraph index rows via the warm venv worker."""
    model_name = (model or "").strip()
    if not model_name:
        raise ToolExecutionError("No embedding model configured.", code="EMBEDDING_MODEL_MISSING")
    return _run_worker(
        ctx,
        _DELETE_STUB,
        {
            "persist_dir": str(persist_dir),
            "collection_name": str(collection_name),
            "meta_path": str(meta_path),
            "keys": list(keys or []),
            "model": model_name,
        },
        model=model_name,
    )


def knn_search(
    ctx: Any,
    persist_dir: str,
    collection_name: str,
    query: str,
    k: int,
    *,
    model: str,
    doc_url_filter: str | None = None,
) -> dict[str, Any]:
    """Semantic search over a folder Chroma index via the warm venv worker."""
    model_name = (model or "").strip()
    if not model_name:
        raise ToolExecutionError("No embedding model configured.", code="EMBEDDING_MODEL_MISSING")
    return _run_worker(
        ctx,
        _SEARCH_STUB,
        {
            "persist_dir": str(persist_dir),
            "collection_name": str(collection_name),
            "query": str(query or ""),
            "k": int(k or 5),
            "model": model_name,
            "doc_url_filter": doc_url_filter,
        },
        model=model_name,
    )


def collection_stats(
    ctx: Any,
    persist_dir: str,
    collection_name: str,
    meta_path: str,
    *,
    model: str = "",
) -> dict[str, Any]:
    """Lightweight Chroma corpus stats for host empty/stale checks."""
    model_name = (model or "").strip()
    return _run_worker(
        ctx,
        _STATS_STUB,
        {
            "persist_dir": str(persist_dir),
            "collection_name": str(collection_name),
            "meta_path": str(meta_path),
            "model": model_name,
        },
        model=model_name or "stats",
    )


__all__ = ["collection_stats", "delete_paragraphs", "index_paragraphs", "knn_search"]
