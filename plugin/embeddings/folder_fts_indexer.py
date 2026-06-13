# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Background per-folder FTS maintenance — delegates to unified corpus indexer."""
from __future__ import annotations

from typing import Any

from plugin.embeddings.embeddings_indexer import enqueue_folder_index, ensure_index_wakeup

__all__ = [
    "enqueue_folder_fts_index",
    "ensure_fts_wakeup",
]


def enqueue_folder_fts_index(ctx: Any, services: Any, model: Any) -> None:
    """Schedule background corpus maintenance (FTS leg uses shared corpus.db)."""
    enqueue_folder_index(ctx, services, model)


def ensure_fts_wakeup(ctx: Any, services: Any, model: Any) -> None:
    """Non-blocking wakeup when FTS search runs against a missing or stale cache."""
    ensure_index_wakeup(ctx, services, model)
