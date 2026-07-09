# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Trusted venv dispatch for folder FTS maintain/search RPC."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast


def dispatch_trusted(data: dict[str, Any], *, heartbeat_fn: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    """Route folder_fts trusted actions to folder FTS compute functions."""
    helper = str(data.get("helper") or "")
    params = data.get("params") or {}
    if not isinstance(params, dict):
        params = {}

    if helper == "maintain_folder_fts":
        from plugin.embeddings.venv.folder_fts import MaintainMode, maintain_folder_fts

        mode_raw = str(params.get("mode") or "auto")
        mode = cast(
            MaintainMode,
            mode_raw if mode_raw in ("auto", "cold", "incremental") else "auto",
        )
        return maintain_folder_fts(
            str(params.get("listing_root") or ""),
            mode,
            heartbeat_fn=heartbeat_fn,
        )
    if helper == "search_folder_fts":
        from plugin.embeddings.venv.folder_fts import search_folder_fts

        return search_folder_fts(
            str(params.get("fts_db_path") or ""),
            str(params.get("query") or ""),
            k=int(params.get("k") or 10),
            near_slop=int(params.get("near_slop", 10)),
        )
    if helper == "fts_stats":
        from plugin.embeddings.venv.folder_fts import fts_stats

        return fts_stats(
            str(params.get("fts_db_path") or ""),
            str(params.get("meta_path") or ""),
        )

    raise ValueError(f"Unknown folder_fts helper: {helper}")
