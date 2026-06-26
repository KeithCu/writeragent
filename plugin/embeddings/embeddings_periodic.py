# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Periodic background embeddings folder indexer (mtime vs last_indexed_at)."""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

log = logging.getLogger(__name__)

_scheduled = False
_schedule_lock = threading.Lock()


def schedule_periodic_embeddings_indexer_once(ctx: Any) -> None:
    """Start periodic folder index maintenance at most once per process (embeddings and/or FTS)."""
    from plugin.framework.constants import folder_search_enabled
    from plugin.framework.worker_pool import run_in_background

    if not folder_search_enabled():
        return
    global _scheduled
    with _schedule_lock:
        if _scheduled:
            log.debug("embeddings periodic indexer: already scheduled this process")
            return
        _scheduled = True
    log.info("embeddings periodic indexer: scheduling background worker")
    run_in_background(run_periodic_embeddings_indexer, ctx, name="embeddings_periodic_indexer")


def run_periodic_embeddings_indexer(ctx: Any) -> None:
    """Daemon loop: enqueue incremental folder index for the active document folder."""
    from plugin.embeddings.embeddings_indexer import enqueue_folder_index
    from plugin.framework.constants import EMBEDDINGS_INDEX_INTERVAL_S, folder_search_enabled
    from plugin.framework.uno_context import get_active_document
    from plugin.main import get_services

    log.info("embeddings periodic indexer: started (interval=%ss)", EMBEDDINGS_INDEX_INTERVAL_S)
    while True:
        time.sleep(EMBEDDINGS_INDEX_INTERVAL_S)
        if not folder_search_enabled():
            continue
        from plugin.framework.queue_executor import execute_on_main_thread

        def _tick() -> None:
            model = get_active_document(ctx)
            if model is None:
                return
            try:
                services = get_services()
                enqueue_folder_index(ctx, services, model)
            except Exception:
                log.exception("embeddings periodic indexer tick failed")

        execute_on_main_thread(_tick)
