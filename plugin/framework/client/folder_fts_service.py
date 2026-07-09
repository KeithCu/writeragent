# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Host-side folder FTS RPC — SQLite FTS5 maintain/search in the embeddings venv worker.

Search could run in-process (LO Python has stdlib sqlite3; reads are cheap). We still
route search and fts_stats through the embeddings venv so SQLite page cache and FTS
buffers stay out of the LibreOffice process — LO already holds UNO/doc/chat heaps;
keeping all fts5.db I/O in the worker avoids extra native allocations there. IPC cost
is acceptable for occasional document_research tool calls.
"""
from __future__ import annotations

import logging
from typing import Any

from plugin.framework.constants import EMBEDDINGS_WORKER_SESSION_PREFIX, WORKER_POOL_EMBEDDINGS
from plugin.scripting.config_limits import embeddings_worker_timeout_sec
from plugin.scripting.trusted_rpc import run_trusted_worker_action

log = logging.getLogger(__name__)

_FTS_SESSION_ID = f"{EMBEDDINGS_WORKER_SESSION_PREFIX}:folder_fts"


def _run_fts_action(
    ctx: Any,
    helper: str,
    params: dict[str, Any],
    *,
    allow_heartbeat: bool = False,
) -> dict[str, Any]:
    timeout_sec = embeddings_worker_timeout_sec(ctx)
    return run_trusted_worker_action(
        ctx,
        domain="folder_fts",
        helper=helper,
        params=params,
        session_id=_FTS_SESSION_ID,
        timeout_sec=timeout_sec,
        worker_pool=WORKER_POOL_EMBEDDINGS,
        allow_heartbeat=allow_heartbeat,
        error_code="FOLDER_FTS_ERROR",
        error_label="Folder FTS",
    )


def maintain_folder_fts(ctx: Any, listing_root: str, *, mode: str = "auto") -> dict[str, Any]:
    """Run full folder FTS maintenance in the embeddings venv."""
    return _run_fts_action(
        ctx,
        "maintain_folder_fts",
        {
            "listing_root": str(listing_root),
            "mode": str(mode or "auto"),
        },
        allow_heartbeat=True,
    )


def search_folder_fts(
    ctx: Any,
    fts_db_path: str,
    query: str,
    k: int,
    *,
    near_slop: int = 10,
) -> dict[str, Any]:
    """Lexical FTS search over a folder index via the warm venv worker."""
    return _run_fts_action(
        ctx,
        "search_folder_fts",
        {
            "fts_db_path": str(fts_db_path),
            "query": str(query or ""),
            "k": int(k or 10),
            "near_slop": int(near_slop or 10),
        },
    )


def fts_stats(ctx: Any, fts_db_path: str, meta_path: str) -> dict[str, Any]:
    """Lightweight FTS stats for host empty/stale checks."""
    return _run_fts_action(
        ctx,
        "fts_stats",
        {
            "fts_db_path": str(fts_db_path),
            "meta_path": str(meta_path),
        },
    )


__all__ = ["fts_stats", "maintain_folder_fts", "search_folder_fts"]
