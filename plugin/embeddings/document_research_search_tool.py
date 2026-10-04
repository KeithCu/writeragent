# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""document_research search_embeddings tool."""

from __future__ import annotations

import logging
from typing import Any, ClassVar

from plugin.framework.tool import ToolBase, ToolContext

log = logging.getLogger(__name__)

_DEFAULT_SEARCH_K = 5
_MAX_SEARCH_K = 20


class SearchEmbeddings(ToolBase):
    """Semantic search over indexed paragraphs in the active document folder."""

    name: str | None = "search_embeddings"
    description: str = (
        "Search the active folder's semantic index for passages related to your query. "
        "Returns ranked doc_url, score, snippet (passage preview), and optional para_index hint. "
        "Use before delegate_read_document when you need cross-file discovery by meaning or topic."
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
        from plugin.embeddings.embeddings_service import knn_search
        from plugin.framework.config import get_config

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
            if mode == "zvec":
                search_path = str(zvec_collection_path(listing_root, create_parent=True))
            elif mode == "lancedb":
                search_path = str(lancedb_collection_path(listing_root, create_parent=True))
            else:
                search_path = str(db_path)

            context_result = {
                "search_path": search_path,
                "folder_key": folder_key,
            }

        if "error" in context_result:
            return {"status": "error", "message": context_result["error"]}

        if context_result.get("empty"):
            from plugin.embeddings.embeddings_indexer import get_failed_indexing_message
            failed_msg = get_failed_indexing_message(str(context_result.get("folder_key") or ""))
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
                "message": "Folder index is building in the background. Retry search_embeddings shortly.",
            }

        search_path = str(context_result["search_path"])
        model = get_embedding_model()

        try:
            result = knn_search(
                ctx.ctx,
                search_path,
                str(query),
                k,
                model=model,
            )
        except Exception as exc:
            log.exception("search_embeddings failed")
            return self._tool_error(str(exc), code="EMBEDDING_SEARCH_ERROR")

        def _wakeup2() -> None:
            ensure_index_wakeup(ctx.ctx, ctx.services, ctx.doc)

        if on_main_thread():
            _wakeup2()
        else:
            execute_on_main_thread(_wakeup2)

        hits = result.get("hits") or []
        return {
            "status": "ok",
            "hits": hits,
            "folder_key": context_result["folder_key"],
            "stale": False,
        }
