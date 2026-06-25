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

    name = "search_embeddings"
    description = (
        "Search the active folder's semantic index for passages related to your query. "
        "Returns ranked doc_url, score, snippet (passage preview), and optional para_index hint. "
        "Use before delegate_read_document when you need cross-file discovery by meaning or topic."
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
        },
        "required": ["query"],
    }

    def is_async(self) -> bool:
        return True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
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

        def _run() -> dict[str, Any]:
            from plugin.embeddings.embeddings_cache import (
                index_is_empty,
                resolve_index_context,
                zvec_collection_looks_populated,
                zvec_collection_path,
                lancedb_collection_looks_populated,
                lancedb_collection_path,
            )
            from plugin.embeddings.embeddings_indexer import ensure_index_wakeup
            from plugin.framework.client.embedding_client import get_embedding_model
            from plugin.framework.client.embeddings_service import knn_search
            from plugin.framework.config import get_config

            folder_key, db_path, meta_path, listing_root = resolve_index_context(ctx.ctx, ctx.doc)
            if folder_key is None or db_path is None or meta_path is None:
                # When resolve fails it returns error string in 4th
                resolve_err = listing_root or "No folder context"
                return {"status": "error", "message": resolve_err}

            # Mode-aware empty check so zvec/lancedb work side-by-side without sqlite corpus
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

            if looks_empty:
                ensure_index_wakeup(ctx.ctx, ctx.services, ctx.doc)
                return {
                    "status": "indexing",
                    "hits": [],
                    "folder_key": folder_key,
                    "stale": True,
                    "message": "Folder index is building in the background. Retry search_embeddings shortly.",
                }

            model = get_embedding_model()
            # For zvec/lancedb mode, pass the corresponding collection path string in the 'db_path' slot;
            # the backend in the venv treats the path as its collection root.
            search_path: str
            if mode == "zvec":
                search_path = str(zvec_collection_path(listing_root, create_parent=True))
            elif mode == "lancedb":
                search_path = str(lancedb_collection_path(listing_root, create_parent=True))
            else:
                search_path = str(db_path)
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

            hits = result.get("hits") or []
            ensure_index_wakeup(ctx.ctx, ctx.services, ctx.doc)
            return {
                "status": "ok",
                "hits": hits,
                "folder_key": folder_key,
                "stale": False,
            }

        return execute_on_main_thread(_run)
