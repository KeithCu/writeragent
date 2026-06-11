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

from plugin.framework.constants import EMBEDDINGS_HEARTBEAT_GRACE_S, EMBEDDINGS_WORKER_SESSION_PREFIX, WORKER_POOL_EMBEDDINGS
from plugin.framework.errors import ToolExecutionError
from plugin.scripting.config_limits import embeddings_worker_timeout_sec
from plugin.scripting.venv_worker import run_code_in_user_venv

log = logging.getLogger(__name__)

_FTS_SESSION_ID = f"{EMBEDDINGS_WORKER_SESSION_PREFIX}:folder_fts"

_MAINTAIN_STUB = """\
from plugin.scripting.folder_fts import maintain_folder_fts as _maintain
result = _maintain(
    data["listing_root"],
    data.get("mode", "auto"),
)
"""

_SEARCH_STUB = """\
from plugin.scripting.folder_fts import search_folder_fts as _search
result = _search(
    data["fts_db_path"],
    data["query"],
    k=data["k"],
    near_slop=data.get("near_slop", 10),
)
"""

_STATS_STUB = """\
from plugin.scripting.folder_fts import fts_stats as _stats
result = _stats(
    data["fts_db_path"],
    data["meta_path"],
)
"""


def _run_worker(ctx: Any, stub: str, payload: dict[str, Any]) -> dict[str, Any]:
    timeout_sec = embeddings_worker_timeout_sec(ctx)
    response = run_code_in_user_venv(
        ctx,
        stub,
        data=payload,
        timeout_sec=timeout_sec,
        session_id=_FTS_SESSION_ID,
        worker_pool=WORKER_POOL_EMBEDDINGS,
    )
    if response.get("status") != "ok":
        message = str(response.get("message") or "Folder FTS worker failed.")
        raise ToolExecutionError(message, code="FOLDER_FTS_ERROR", details={"worker": response})
    result = response.get("result")
    if not isinstance(result, dict):
        raise ToolExecutionError(
            "Folder FTS worker returned an unexpected result.",
            code="FOLDER_FTS_ERROR",
            details={"result_type": type(result).__name__},
        )
    return result


def _run_worker_with_heartbeat(ctx: Any, stub: str, payload: dict[str, Any]) -> dict[str, Any]:
    timeout_sec = embeddings_worker_timeout_sec(ctx)

    def _on_heartbeat(hb: dict[str, Any]) -> None:
        log.debug("folder FTS heartbeat: %s", hb)

    response = run_code_in_user_venv(
        ctx,
        stub,
        data=payload,
        timeout_sec=timeout_sec,
        session_id=_FTS_SESSION_ID,
        worker_pool=WORKER_POOL_EMBEDDINGS,
        allow_heartbeat=True,
        heartbeat_grace_sec=EMBEDDINGS_HEARTBEAT_GRACE_S,
        on_heartbeat=_on_heartbeat,
    )
    if response.get("status") != "ok":
        message = str(response.get("message") or "Folder FTS worker failed.")
        raise ToolExecutionError(message, code="FOLDER_FTS_ERROR", details={"worker": response})
    result = response.get("result")
    if not isinstance(result, dict):
        raise ToolExecutionError(
            "Folder FTS worker returned an unexpected result.",
            code="FOLDER_FTS_ERROR",
            details={"result_type": type(result).__name__},
        )
    return result


def maintain_folder_fts(ctx: Any, listing_root: str, *, mode: str = "auto") -> dict[str, Any]:
    """Run full folder FTS maintenance in the embeddings venv."""
    return _run_worker_with_heartbeat(
        ctx,
        _MAINTAIN_STUB,
        {
            "listing_root": str(listing_root),
            "mode": str(mode or "auto"),
        },
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
    return _run_worker(
        ctx,
        _SEARCH_STUB,
        {
            "fts_db_path": str(fts_db_path),
            "query": str(query or ""),
            "k": int(k or 10),
            "near_slop": int(near_slop or 10),
        },
    )


def fts_stats(ctx: Any, fts_db_path: str, meta_path: str) -> dict[str, Any]:
    """Lightweight FTS stats for host empty/stale checks."""
    return _run_worker(
        ctx,
        _STATS_STUB,
        {
            "fts_db_path": str(fts_db_path),
            "meta_path": str(meta_path),
        },
    )


__all__ = ["fts_stats", "maintain_folder_fts", "search_folder_fts"]
