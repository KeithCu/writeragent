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

    name: str | None = "search_nearby_files"
    description: str = (
        "Search the active folder index (keyword BM25/NEAR + semantic embeddings, fused ranking). "
        "Returns ranked doc_url, score, snippet, and optional para_index hint. "
        "Use for cross-file discovery when filenames are unknown."
    )
    tier: str = "specialized"
    specialized_domain: ClassVar[str | None] = "document_research"
    specialized_cross_cutting: ClassVar[bool] = True
    is_mutation: bool | None = False
    long_running: bool = True
    parameters: dict[str, Any] | None = {
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
        if ctx.stop_checker and ctx.stop_checker():
            return {"status": "error", "message": "Cancelled"}
        from plugin.framework.constants import folder_search_enabled
        from plugin.framework.queue_executor import execute_on_main_thread

        if not folder_search_enabled():
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

        from plugin.doc.document_research_grep import resolve_grep_candidates
        from plugin.embeddings.embeddings_cache import (
            index_is_empty,
            query_blocked_for_model,
            resolve_index_context,
            zvec_collection_looks_populated,
            zvec_collection_path,
            lancedb_collection_looks_populated,
            lancedb_collection_path,
        )
        from plugin.embeddings.embeddings_indexer import ensure_index_wakeup
        from plugin.embeddings.embedding_client import get_embedding_model
        from plugin.embeddings.embeddings_service import hybrid_search
        from plugin.framework.config import get_config
        import pathlib

        def _resolve_context() -> dict[str, Any]:
            folder_key, db_path, meta_path, listing_root = resolve_index_context(ctx.ctx, ctx.doc)
            return {
                "folder_key": folder_key,
                "db_path": db_path,
                "meta_path": meta_path,
                "listing_root": listing_root
            }

        from plugin.framework.thread_guard import on_main_thread
        if on_main_thread():
            ctx_data = _resolve_context()
        else:
            ctx_data = execute_on_main_thread(_resolve_context)

        folder_key = ctx_data["folder_key"]
        db_path = ctx_data["db_path"]
        meta_path = ctx_data["meta_path"]
        listing_root = ctx_data["listing_root"]

        if folder_key is None or db_path is None or meta_path is None:
            return {"status": "error", "message": listing_root or "No folder context"}

        mode = str(get_config("embeddings.folder_search_mode") or "none").strip().lower()
        looks_empty = False
        if mode == "zvec":
            zpath = zvec_collection_path(listing_root, create_parent=False)
            looks_empty = not zvec_collection_looks_populated(zpath)
        elif mode == "lancedb":
            lpath = lancedb_collection_path(listing_root, create_parent=False)
            looks_empty = not lancedb_collection_looks_populated(lpath)
        else:
            looks_empty = index_is_empty(meta_path, db_path)

        if not looks_empty and mode != "fts" and query_blocked_for_model(meta_path, get_embedding_model()):
            looks_empty = True

        if looks_empty:
            def _wakeup() -> None:
                ensure_index_wakeup(ctx.ctx, ctx.services, ctx.doc)
            if on_main_thread():
                _wakeup()
            else:
                execute_on_main_thread(_wakeup)
            context_result = {"empty": True, "folder_key": folder_key}
        else:
            allowed_urls: set[str] | None = None
            if file_subset:
                def _do_resolve():
                    return resolve_grep_candidates(ctx.ctx, ctx.doc, file_subset=str(file_subset))
                if on_main_thread():
                    candidates, _truncated, err = _do_resolve()
                else:
                    candidates, _truncated, err = execute_on_main_thread(_do_resolve)

                if err:
                    return {"status": "error", "message": err}
                allowed_urls = set()
                for c in candidates:
                    url = str(c.get("url") or "")
                    if not url:
                        path = c.get("path")
                        if path:
                            url = pathlib.Path(path).as_uri()
                    if url:
                        allowed_urls.add(url)

            if mode == "zvec":
                search_path = str(zvec_collection_path(listing_root, create_parent=True))
            elif mode == "lancedb":
                search_path = str(lancedb_collection_path(listing_root, create_parent=True))
            else:
                search_path = str(db_path)

            context_result = {
                "search_path": search_path,
                "folder_key": folder_key,
                "allowed_urls": allowed_urls,
            }

        if "error" in context_result:
            return {"status": "error", "message": context_result["error"]}

        if context_result.get("empty"):
            from plugin.embeddings.embeddings_indexer import get_failed_indexing_message
            failed_msg = get_failed_indexing_message(context_result["folder_key"])
            if failed_msg:
                return {
                    "status": "error",
                    "message": f"Background indexing failed: {failed_msg}",
                    "folder_key": context_result["folder_key"],
                }
            return {
                "status": "indexing",
                "hits": [],
                "folder_key": context_result["folder_key"],
                "stale": True,
                "message": "Folder index is building in the background. Retry search_nearby_files shortly.",
            }

        search_path = context_result["search_path"]
        allowed_urls = context_result["allowed_urls"]
        model = get_embedding_model()

        try:
            result = hybrid_search(
                ctx.ctx,
                search_path,
                str(query),
                k,
                model=model,
                near_slop=near_slop,
                doc_url_filter=context_result["allowed_urls"],
            )
        except Exception as exc:
            log.exception("search_nearby_files failed")
            return self._tool_error(str(exc), code="FOLDER_HYBRID_SEARCH_ERROR")

        def _wakeup() -> None:
            ensure_index_wakeup(ctx.ctx, ctx.services, ctx.doc)

        if on_main_thread():
            _wakeup()
        else:
            execute_on_main_thread(_wakeup)

        hits = list(result.get("hits") or [])
        return {
            "status": "ok",
            "hits": hits,
            "folder_key": context_result["folder_key"],
            "stale": False,
        }
