# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Background per-folder SQLite FTS maintenance (ODF extract in embeddings venv)."""
from __future__ import annotations

import logging
import threading
from typing import Any

from plugin.doc.folder_fts_cache import resolve_fts_context
from plugin.framework.client.folder_fts_service import maintain_folder_fts as maintain_folder_fts_rpc
from plugin.framework.constants import document_research_uses_folder_fts
from plugin.framework.worker_pool import run_in_background

__all__ = [
    "enqueue_folder_fts_index",
    "ensure_fts_wakeup",
]

log = logging.getLogger(__name__)

_inflight: set[str] = set()
_inflight_lock = threading.Lock()


def _try_enqueue(folder_key: str) -> bool:
    with _inflight_lock:
        if folder_key in _inflight:
            return False
        _inflight.add(folder_key)
        return True


def _clear_enqueue(folder_key: str) -> None:
    with _inflight_lock:
        _inflight.discard(folder_key)


def _index_worker(ctx: Any, folder_key: str, listing_root: str) -> None:
    try:
        maintain_folder_fts_rpc(ctx, listing_root, mode="auto")
    except Exception:
        log.exception("Background folder FTS index failed for folder %s", folder_key)
    finally:
        _clear_enqueue(folder_key)


def enqueue_folder_fts_index(ctx: Any, services: Any, model: Any) -> None:
    """Schedule background FTS maintenance for the active document folder."""
    del services
    if not document_research_uses_folder_fts(ctx):
        return
    folder_key, _db, _meta, listing_root = resolve_fts_context(ctx, model)
    if folder_key is None or listing_root is None:
        return
    if not _try_enqueue(folder_key):
        return

    def _run() -> None:
        _index_worker(ctx, folder_key, listing_root)

    run_in_background(_run, name=f"folder-fts-index-{folder_key[:8]}")


def ensure_fts_wakeup(ctx: Any, services: Any, model: Any) -> None:
    """Non-blocking wakeup when search runs against a missing or stale FTS cache."""
    enqueue_folder_fts_index(ctx, services, model)
