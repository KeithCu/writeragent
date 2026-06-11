# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Host-side paths and metadata for per-folder SQLite FTS5 (beside writeragent_embeddings/)."""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

from plugin.doc.embeddings_cache import folder_cache_dir, resolve_index_context

log = logging.getLogger(__name__)

FTS_SCHEMA_VERSION = "1"
FTS_DB_FILENAME = "fts5.db"
FTS_META_FILENAME = "fts_meta.json"


def fts_db_path(listing_root: str, *, create_parent: bool = True) -> Path:
    """SQLite FTS5 database beside the folder embeddings cache."""
    return folder_cache_dir(listing_root, create_parent=create_parent) / FTS_DB_FILENAME


def fts_meta_path(listing_root: str, *, create_parent: bool = True) -> Path:
    """JSON metadata for the FTS index (row counts, schema version)."""
    return folder_cache_dir(listing_root, create_parent=create_parent) / FTS_META_FILENAME


def read_fts_meta(meta_path: Path) -> dict[str, str]:
    if not meta_path.is_file():
        return {}
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log.debug("read_fts_meta failed for %s", meta_path, exc_info=True)
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items()}


def write_fts_meta(meta_path: Path, **fields: str) -> None:
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    current = read_fts_meta(meta_path)
    current.update({str(k): str(v) for k, v in fields.items()})
    meta_path.write_text(json.dumps(current, indent=2, sort_keys=True), encoding="utf-8")


def row_count_from_meta(meta_path: Path) -> int:
    meta = read_fts_meta(meta_path)
    raw = meta.get("row_count", "0")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def fts_schema_matches(meta_path: Path) -> bool:
    meta = read_fts_meta(meta_path)
    return meta.get("schema_version", "") == FTS_SCHEMA_VERSION


def fts_index_is_empty(meta_path: Path, db_path: Path | None = None) -> bool:
    """True when there is no usable FTS corpus."""
    if db_path is not None and not db_path.is_file():
        return True
    if not meta_path.is_file():
        return True
    return row_count_from_meta(meta_path) <= 0


def needs_fts_cold_rebuild(meta_path: Path, db_path: Path) -> bool:
    if not db_path.is_file():
        return True
    if not meta_path.is_file():
        return True
    if not fts_schema_matches(meta_path):
        return True
    return row_count_from_meta(meta_path) == 0


def resolve_fts_context(ctx: Any, model: Any) -> tuple[str | None, Path | None, Path | None, str]:
    """Return (folder_key, fts_db_path, fts_meta_path, listing_root) or error tuple."""
    folder_key, _persist, _meta, listing_or_err = resolve_index_context(ctx, model)
    if folder_key is None or not listing_or_err:
        return None, None, None, listing_or_err or "No nearby files found."
    listing_root = listing_or_err
    return folder_key, fts_db_path(listing_root), fts_meta_path(listing_root), listing_root


__all__ = [
    "FTS_DB_FILENAME",
    "FTS_META_FILENAME",
    "FTS_SCHEMA_VERSION",
    "fts_db_path",
    "fts_index_is_empty",
    "fts_meta_path",
    "fts_schema_matches",
    "needs_fts_cold_rebuild",
    "read_fts_meta",
    "resolve_fts_context",
    "row_count_from_meta",
    "write_fts_meta",
]
