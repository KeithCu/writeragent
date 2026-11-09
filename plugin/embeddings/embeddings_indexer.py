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
    folder_corpus_key,
    maybe_upgrade_legacy_index,
    parse_search_mode,
    resolve_folder_for_active_doc,
)
from plugin.embeddings.embedding_client import get_embedding_model
from plugin.embeddings.embeddings_service import maintain_folder_index as maintain_folder_index_rpc
from plugin.framework.constants import configured_folder_search_mode, folder_search_enabled
from plugin.framework.send_cancellation import get_current_send_cancellation, is_cancelled
from plugin.framework.worker_pool import run_in_background

__all__ = ["enqueue_folder_index", "ensure_index_wakeup"]

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


def _resolve_search_mode() -> str:
    """Map Settings cross-file search mode to venv maintain/search backend."""
    return parse_search_mode(configured_folder_search_mode())


def _index_worker(ctx: Any, folder_key: str, listing_root: str) -> None:
    try:
        # The host delete must finish before the child writes to the same tree.
        maybe_upgrade_legacy_index(listing_root)
        model = get_embedding_model()
        maintain_folder_index_rpc(
            ctx,
            listing_root,
            model=model,
            mode="auto",
            search_mode=_resolve_search_mode(),
        )
    except Exception:
        log.exception("Background corpus index failed for folder %s", folder_key)
    finally:
        _clear_enqueue(folder_key)


def enqueue_folder_index(ctx: Any, model: Any) -> None:
    """Schedule background corpus maintenance for the active document folder."""
    if not folder_search_enabled():
        return
    from plugin.framework.queue_executor import main

    listing_root = main.call(lambda: resolve_folder_for_active_doc(ctx, model))
    if not listing_root:
        return
    folder_key = folder_corpus_key(listing_root)
    if not _try_enqueue(folder_key):
        return

    def _run() -> None:
        _index_worker(ctx, folder_key, listing_root)

    run_in_background(_run, name=f"corpus-index-{folder_key[:8]}", dedicated=True)


def ensure_index_wakeup(ctx: Any, model: Any) -> None:
    """Non-blocking wakeup when search runs against a missing or stale cache."""
    # TurnController has no cancellation scope. Stop latches SendCancellation.
    if is_cancelled(get_current_send_cancellation()):
        return
    enqueue_folder_index(ctx, model)
