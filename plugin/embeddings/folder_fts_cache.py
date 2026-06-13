# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Host-side paths for folder FTS (unified corpus.db beside writeragent_embeddings/)."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

from plugin.embeddings.embeddings_cache import (
    corpus_db_path,
    corpus_meta_path,
    index_is_empty,
    read_corpus_meta,
    resolve_index_context,
    schema_matches,
    write_corpus_meta,
)

log = logging.getLogger(__name__)

FTS_SCHEMA_VERSION = "3"
FTS_DB_FILENAME = "corpus.db"
FTS_META_FILENAME = "corpus_meta.json"


def fts_db_path(listing_root: str, *, create_parent: bool = True) -> Path:
    """Unified corpus.db (FTS5 passages live in the same file as vec0)."""
    return corpus_db_path(listing_root, create_parent=create_parent)


def fts_meta_path(listing_root: str, *, create_parent: bool = True) -> Path:
    """Shared corpus_meta.json for FTS and embeddings."""
    return corpus_meta_path(listing_root, create_parent=create_parent)


def read_fts_meta(meta_path: Path) -> dict[str, str]:
    return read_corpus_meta(meta_path)


def write_fts_meta(meta_path: Path, **fields: str) -> None:
    write_corpus_meta(meta_path, **fields)


def row_count_from_meta(meta_path: Path) -> int:
    meta = read_fts_meta(meta_path)
    raw = meta.get("row_count") or meta.get("chunk_count") or "0"
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def fts_schema_matches(meta_path: Path) -> bool:
    return schema_matches(meta_path)


def fts_index_is_empty(meta_path: Path, db_path: Path | None = None) -> bool:
    """True when there is no usable FTS corpus."""
    return index_is_empty(meta_path, db_path)


def needs_fts_cold_rebuild(meta_path: Path, db_path: Path) -> bool:
    if not db_path.is_file():
        return True
    if not meta_path.is_file():
        return True
    if not fts_schema_matches(meta_path):
        return True
    return row_count_from_meta(meta_path) == 0


def resolve_fts_context(ctx: Any, model: Any) -> tuple[str | None, Path | None, Path | None, str]:
    """Return (folder_key, corpus_db_path, corpus_meta_path, listing_root) or error tuple."""
    folder_key, db_path, meta_path, listing_or_err = resolve_index_context(ctx, model)
    if folder_key is None or not listing_or_err:
        return None, None, None, listing_or_err or "No nearby files found."
    return folder_key, db_path, meta_path, listing_or_err


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
