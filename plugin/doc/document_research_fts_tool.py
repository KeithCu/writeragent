# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""document_research search_nearby_files tool (SQLite FTS5 via embeddings venv)."""

from __future__ import annotations

import logging
from typing import Any, ClassVar

from plugin.framework.tool import ToolBase, ToolContext

log = logging.getLogger(__name__)

_DEFAULT_SEARCH_K = 10
_MAX_SEARCH_K = 30
_DEFAULT_NEAR_SLOP = 10


class SearchNearbyFiles(ToolBase):
    """Lexical proximity search over indexed paragraphs in the active document folder."""

    name = "search_nearby_files"
    description = (
        "Search the active folder's full-text index for keyword/proximity matches (BM25 + NEAR). "
        "Returns ranked doc_url, score, snippet, and para_index hint. "
        "Use for cross-file keyword discovery when filenames are unknown."
    )
    tier = "specialized"
    specialized_domain: ClassVar[str | None] = "document_research"
    specialized_cross_cutting: ClassVar[bool] = True
    is_mutation = False
    long_running = True
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Keywords or NEAR/N proximity query."},
            "k": {
                "type": "integer",
                "description": f"Maximum hits to return (default {_DEFAULT_SEARCH_K}, max {_MAX_SEARCH_K}).",
            },
            "near_slop": {
                "type": "integer",
                "description": f"Token gap for multi-word queries (default {_DEFAULT_NEAR_SLOP}).",
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
        from plugin.framework.constants import document_research_uses_folder_fts
        from plugin.framework.queue_executor import execute_on_main_thread

        if not document_research_uses_folder_fts(ctx.ctx):
            return self._tool_error(
                "Folder full-text index is disabled. Enable it in Settings → Embeddings.",
                code="FOLDER_FTS_DISABLED",
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
            from plugin.doc.folder_fts_cache import fts_index_is_empty, resolve_fts_context
            from plugin.doc.folder_fts_indexer import ensure_fts_wakeup
            from plugin.framework.client.folder_fts_service import search_folder_fts

            folder_key, db_path, meta_path, listing_or_err = resolve_fts_context(ctx.ctx, ctx.doc)
            if folder_key is None or db_path is None or meta_path is None:
                return {"status": "error", "message": listing_or_err}

            if fts_index_is_empty(meta_path, db_path):
                ensure_fts_wakeup(ctx.ctx, ctx.services, ctx.doc)
                return {
                    "status": "indexing",
                    "hits": [],
                    "folder_key": folder_key,
                    "stale": True,
                    "message": "Folder FTS index is building in the background. Retry search_nearby_files shortly.",
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

            try:
                result = search_folder_fts(
                    ctx.ctx,
                    str(db_path),
                    str(query),
                    k,
                    near_slop=near_slop,
                )
            except Exception as exc:
                log.exception("search_nearby_files failed")
                return self._tool_error(str(exc), code="FOLDER_FTS_SEARCH_ERROR")

            hits = list(result.get("hits") or [])
            if allowed_urls is not None:
                hits = [h for h in hits if h.get("doc_url") in allowed_urls]

            ensure_fts_wakeup(ctx.ctx, ctx.services, ctx.doc)
            return {
                "status": "ok",
                "hits": hits,
                "folder_key": folder_key,
                "match": result.get("match"),
                "stale": False,
            }

        return execute_on_main_thread(_run)
