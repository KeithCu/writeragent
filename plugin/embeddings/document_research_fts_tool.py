# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""document_research search_nearby_files tool (hybrid FTS + embeddings via RRF)."""

from __future__ import annotations

import logging
from typing import Any, ClassVar

from plugin.framework.tool import ToolBase, ToolContext

log = logging.getLogger(__name__)

_DEFAULT_SEARCH_K = 10
_MAX_SEARCH_K = 30
_DEFAULT_NEAR_SLOP = 10


class SearchNearbyFiles(ToolBase):
    """Hybrid keyword + semantic search over indexed paragraphs in the active document folder."""

    name = "search_nearby_files"
    description = (
        "Search the active folder index (keyword BM25/NEAR + semantic embeddings, fused ranking). "
        "Returns ranked doc_url, score, snippet, and optional para_index hint. "
        "Use for cross-file discovery when filenames are unknown."
    )
    tier = "specialized"
    specialized_domain: ClassVar[str | None] = "document_research"
    specialized_cross_cutting: ClassVar[bool] = True
    is_mutation = False
    long_running = True
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Natural-language or keyword query."},
            "k": {
                "type": "integer",
                "description": f"Maximum hits to return (default {_DEFAULT_SEARCH_K}, max {_MAX_SEARCH_K}).",
            },
            "near_slop": {
                "type": "integer",
                "description": f"Token gap for multi-word keyword leg (default {_DEFAULT_NEAR_SLOP}).",
            },
            "file_subset": {
                "type": "string",
                "description": "Optional basename token or absolute path to restrict hits to matching files.",
            },
        },
        "required": ["query"],
    }

    def is_async(self) -> bool:
        return True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        from plugin.framework.constants import folder_search_enabled
        from plugin.framework.queue_executor import execute_on_main_thread

        if not folder_search_enabled(ctx.ctx):
            return self._tool_error(
                "Cross-file search is disabled. Enable Embeddings + FTS in Settings → Embeddings.",
                code="FOLDER_SEARCH_DISABLED",
            )

        query = kwargs.get("query")
        if not query:
            return self._tool_error("query is required")

        k_raw = kwargs.get("k", _DEFAULT_SEARCH_K)
        try:
            k = max(1, min(int(k_raw), _MAX_SEARCH_K))
        except (TypeError, ValueError):
            k = _DEFAULT_SEARCH_K

        near_raw = kwargs.get("near_slop", _DEFAULT_NEAR_SLOP)
        try:
            near_slop = max(0, int(near_raw))
        except (TypeError, ValueError):
            near_slop = _DEFAULT_NEAR_SLOP

        file_subset = kwargs.get("file_subset")

        def _run() -> dict[str, Any]:
            from plugin.doc.document_research_grep import resolve_grep_candidates
            from plugin.embeddings.embeddings_cache import (
                index_is_empty,
                resolve_index_context,
                zvec_collection_looks_populated,
                zvec_collection_path,
            )
            from plugin.embeddings.embeddings_indexer import ensure_index_wakeup
            from plugin.framework.client.embedding_client import get_embedding_model
            from plugin.framework.client.embeddings_service import hybrid_search
            from plugin.framework.config import get_config

            folder_key, db_path, meta_path, listing_root = resolve_index_context(ctx.ctx, ctx.doc)
            if folder_key is None or db_path is None or meta_path is None:
                resolve_err = listing_root or "No folder context"
                return {"status": "error", "message": resolve_err}

            # Mode-aware empty check for zvec side-by-side store (use raw Path values for index_is_empty)
            mode = str(get_config(ctx.ctx, "embeddings.folder_search_mode") or "none").strip().lower()
            looks_empty = False
            if mode == "zvec":
                zpath = zvec_collection_path(listing_root, create_parent=False)
                looks_empty = not zvec_collection_looks_populated(zpath)
            else:
                looks_empty = index_is_empty(meta_path, db_path)

            if looks_empty:
                ensure_index_wakeup(ctx.ctx, ctx.services, ctx.doc)
                return {
                    "status": "indexing",
                    "hits": [],
                    "folder_key": folder_key,
                    "stale": True,
                    "message": "Folder index is building in the background. Retry search_nearby_files shortly.",
                }

            allowed_urls: set[str] | None = None
            if file_subset:
                candidates, _truncated, err = resolve_grep_candidates(
                    ctx.ctx,
                    ctx.doc,
                    file_subset=str(file_subset),
                )
                if err:
                    return {"status": "error", "message": err}
                allowed_urls = {str(c.get("url") or "") for c in candidates if c.get("url")}

            model = get_embedding_model(ctx.ctx)
            # For zvec, pass zvec collection path in the db_path slot (venv zvec backend interprets it)
            search_path: str = str(zvec_collection_path(listing_root, create_parent=True)) if mode == "zvec" else str(db_path)
            try:
                result = hybrid_search(
                    ctx.ctx,
                    search_path,
                    str(query),
                    k,
                    model=model,
                    near_slop=near_slop,
                )
            except Exception as exc:
                log.exception("search_nearby_files failed")
                return self._tool_error(str(exc), code="FOLDER_HYBRID_SEARCH_ERROR")

            hits = list(result.get("hits") or [])
            if allowed_urls is not None:
                hits = [h for h in hits if h.get("doc_url") in allowed_urls]

            ensure_index_wakeup(ctx.ctx, ctx.services, ctx.doc)
            return {
                "status": "ok",
                "hits": hits,
                "folder_key": folder_key,
                "stale": False,
            }

        return execute_on_main_thread(_run)
