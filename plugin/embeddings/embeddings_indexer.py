# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Background per-folder corpus index maintenance (corpus.db in venv)."""
from __future__ import annotations

import logging
import threading
from typing import Any

from plugin.embeddings.embeddings_cache import (
    file_is_stale,
    resolve_index_context,
)
from plugin.framework.client.embedding_client import get_embedding_model
from plugin.framework.client.embeddings_service import maintain_folder_index as maintain_folder_index_rpc
from plugin.framework.config import get_config
from plugin.framework.constants import folder_search_enabled
from plugin.framework.worker_pool import run_in_background

# Re-export for tests
__all__ = [
    "enqueue_folder_index",
    "ensure_index_wakeup",
    "file_is_stale",
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


def _resolve_search_mode(ctx: Any) -> str:
    """Map Settings cross-file search mode to venv maintain/search backend."""
    mode = str(get_config("embeddings.folder_search_mode") or "none").strip().lower()
    if mode in ("hybrid", "llama_index", "zvec", "lancedb", "fts", "embeddings"):
        return mode
    return "hybrid"


def _index_worker(ctx: Any, folder_key: str, listing_root: str) -> None:
    try:
        model = get_embedding_model()
        maintain_folder_index_rpc(
            ctx,
            listing_root,
            model=model,
            mode="auto",
            search_mode=_resolve_search_mode(ctx),
        )
    except Exception:
        log.exception("Background corpus index failed for folder %s", folder_key)
    finally:
        _clear_enqueue(folder_key)


def enqueue_folder_index(ctx: Any, services: Any, model: Any) -> None:
    """Schedule background corpus maintenance for the active document folder."""
    del services  # venv maintain does not use UNO services
    if not folder_search_enabled():
        return
    from plugin.framework.queue_executor import execute_on_main_thread

    def _resolve() -> tuple[str, Any, Any, str] | tuple[None, None, None, str]:
        return resolve_index_context(ctx, model)

    resolved = execute_on_main_thread(_resolve)
    folder_key, _db, _meta, listing_root = resolved[0], resolved[1], resolved[2], resolved[3]
    if folder_key is None or listing_root is None:
        return
    if not _try_enqueue(folder_key):
        return

    def _run() -> None:
        _index_worker(ctx, folder_key, listing_root)

    run_in_background(_run, name=f"corpus-index-{folder_key[:8]}")


def ensure_index_wakeup(ctx: Any, services: Any, model: Any) -> None:
    """Non-blocking wakeup when search runs against a missing or stale cache."""
    enqueue_folder_index(ctx, services, model)
