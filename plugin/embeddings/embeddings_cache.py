# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Per-folder corpus cache paths and host-side index state (sqlite-vec + JSON meta)."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any

from plugin.framework.constants import EMBEDDINGS_SCHEMA_VERSION as SCHEMA_VERSION

log = logging.getLogger(__name__)

EMBEDDINGS_CACHE_DIRNAME = "writeragent_embeddings"
STORAGE_BACKEND = "sqlite_vec"
CORPUS_META_FILENAME = "corpus_meta.json"
LEGACY_FILE_INDEX_STATE_FILENAME = "file_index_state.json"
CORPUS_DB_FILENAME = "corpus.db"
LEGACY_INDEX_DB = "index.db"
CHROMA_SUBDIR = "chroma"
LEGACY_FTS_DB = "fts5.db"
LEGACY_FTS_META = "fts_meta.json"


def folder_corpus_key(directory_path: str) -> str:
    """Stable cache key for a normalized directory path."""
    norm = os.path.normpath(os.path.abspath(directory_path))
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def resolve_folder_for_active_doc(ctx: Any, model: Any) -> str | None:
    """Directory whose siblings are indexed — same scope as list_nearby_files."""
    # Lazy import: document_research pulls uno; venv maintain code must not load it at import time.
    from plugin.doc.document_research import resolve_listing_directory

    return resolve_listing_directory(ctx, model)


def _normalized_listing_root(listing_root: str) -> str:
    return os.path.normpath(os.path.abspath(listing_root))


def folder_cache_dir(listing_root: str, *, create_parent: bool = True) -> Path:
    """Base directory for one folder's corpus.db + JSON state (beside indexed documents)."""
    path = Path(_normalized_listing_root(listing_root)) / EMBEDDINGS_CACHE_DIRNAME
    if create_parent:
        path.mkdir(parents=True, exist_ok=True)
    return path


def corpus_db_path(listing_root: str, *, create_parent: bool = True) -> Path:
    """Unified SQLite corpus (chunks + FTS5 + vec0) for the document folder."""
    return folder_cache_dir(listing_root, create_parent=create_parent) / CORPUS_DB_FILENAME


def zvec_collection_path(listing_root: str, *, create_parent: bool = True) -> Path:
    """Filesystem path for a zvec collection store for this folder (side-by-side with corpus.db)."""
    # Place under the same writeragent_embeddings/ sibling dir as the sqlite corpus.
    base = folder_cache_dir(listing_root, create_parent=create_parent)
    p = base / "zvec"
    if create_parent:
        p.mkdir(parents=True, exist_ok=True)
    return p


def zvec_collection_looks_populated(collection_path: Path) -> bool:
    """Host-safe (no zvec import) heuristic: dir exists and has any entries.
    Used for early 'empty index' checks in tools before kicking off background maintain.
    """
    try:
        p = Path(collection_path)
        if not p.exists() or not p.is_dir():
            return False
        # Any file or subdir is a signal that create_and_open has written something.
        return any(p.iterdir())
    except Exception:
        return False


def chroma_persist_dir(listing_root: str, *, create_parent: bool = True) -> Path:
    """Deprecated alias — returns corpus.db path (historical Chroma API name)."""
    return corpus_db_path(listing_root, create_parent=create_parent)


def corpus_meta_path(listing_root: str, *, create_parent: bool = True) -> Path:
    """JSON corpus metadata beside corpus.db."""
    return folder_cache_dir(listing_root, create_parent=create_parent) / CORPUS_META_FILENAME


def legacy_file_index_state_path(listing_root: str) -> Path:
    """Pre-v4 JSON incremental state (removed; delete on cache clear)."""
    return folder_cache_dir(listing_root, create_parent=False) / LEGACY_FILE_INDEX_STATE_FILENAME


def legacy_index_db_path(listing_root: str) -> Path:
    """Pre-v3 SQLite index path (removed on upgrade)."""
    return folder_cache_dir(listing_root, create_parent=False) / LEGACY_INDEX_DB


def _remove_path(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink()
        return True
    except OSError:
        log.debug("Could not remove %s", path, exc_info=True)
        return False


def read_corpus_meta(meta_path: Path) -> dict[str, str]:
    """Load corpus_meta.json; return empty dict when missing."""
    if not meta_path.is_file():
        return {}
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log.debug("read_corpus_meta failed for %s", meta_path, exc_info=True)
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items()}


def write_corpus_meta(meta_path: Path, **fields: str) -> None:
    """Merge *fields* into corpus_meta.json."""
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    current = read_corpus_meta(meta_path)
    current.update({str(k): str(v) for k, v in fields.items()})
    meta_path.write_text(json.dumps(current, indent=2, sort_keys=True), encoding="utf-8")


def _open_index_db(db_path: Path) -> Any:
    """Open corpus.db and ensure base schema (including indexed_* tables)."""
    from plugin.embeddings.venv.embeddings_sqlite import connect_corpus_db, ensure_schema

    conn = connect_corpus_db(db_path)
    ensure_schema(conn)
    return conn


def get_file_index_state(db_path: Path, doc_url: str) -> dict[str, float | int]:
    """Return stored file_mtime, last_indexed_at, and paragraph count for *doc_url*."""
    from plugin.embeddings.venv.embeddings_sqlite import get_file_index_info

    if not db_path.is_file():
        return {"file_mtime": 0.0, "last_indexed_at": 0.0, "chunk_count": 0}
    conn = _open_index_db(db_path)
    try:
        return get_file_index_info(conn, doc_url)
    finally:
        conn.close()


def file_is_stale(db_path: Path, doc_url: str, file_mtime: float) -> bool:
    """True when filesystem mtime is newer than last indexed timestamp for *doc_url*."""
    from plugin.embeddings.venv.embeddings_sqlite import file_is_stale_in_db

    if not db_path.is_file():
        return True
    conn = _open_index_db(db_path)
    try:
        return file_is_stale_in_db(conn, doc_url, file_mtime)
    finally:
        conn.close()


def mark_file_indexed(
    db_path: Path,
    doc_url: str,
    file_mtime: float,
    *,
    indexed_at: float | None = None,
    paragraphs: dict[str, str] | None = None,
) -> None:
    """Advance last_indexed_at/file_mtime for *doc_url* in corpus.db."""
    from plugin.embeddings.venv.embeddings_sqlite import mark_file_indexed_in_db

    ts = float(indexed_at if indexed_at is not None else time.time())
    conn = _open_index_db(db_path)
    try:
        mark_file_indexed_in_db(
            conn,
            doc_url,
            file_mtime,
            indexed_at=ts,
            paragraphs=paragraphs,
        )
    finally:
        conn.close()


def diff_chunk_rows(
    db_path: Path,
    chunks: list[Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (rows_to_index, keys_to_delete) comparing extracted chunks to corpus.db."""
    from plugin.embeddings.venv.embeddings_sqlite import diff_chunk_rows_in_db

    if not db_path.is_file():
        from plugin.embeddings.embeddings_fs import ParagraphChunk, chunk_to_index_row

        to_index = [chunk_to_index_row(c) for c in chunks if isinstance(c, ParagraphChunk)]
        return to_index, []
    conn = _open_index_db(db_path)
    try:
        return diff_chunk_rows_in_db(conn, chunks)
    finally:
        conn.close()


def diff_paragraph_rows(
    db_path: Path,
    chunks: list[Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Alias for :func:`diff_chunk_rows` (chunk-locator incremental diff)."""
    return diff_chunk_rows(db_path, chunks)


def sync_file_paragraph_state(db_path: Path, doc_url: str, chunks: list[Any], file_mtime: float) -> None:
    """Update paragraph hashes in corpus.db after a successful index pass."""
    from plugin.embeddings.venv.embeddings_sqlite import sync_file_paragraph_state_in_db

    conn = _open_index_db(db_path)
    try:
        sync_file_paragraph_state_in_db(
            conn,
            doc_url,
            chunks,
            file_mtime,
            indexed_at=time.time(),
        )
    finally:
        conn.close()


def ensure_corpus_meta(
    meta_path: Path,
    *,
    embedding_model: str,
    dim: int | None = None,
    chunk_count: int | None = None,
) -> None:
    """Initialize or refresh corpus metadata on the host."""
    now = str(time.time())
    fields: dict[str, str] = {
        "schema_version": SCHEMA_VERSION,
        "embedding_model": embedding_model,
        "storage_backend": STORAGE_BACKEND,
        "updated_at": now,
    }
    if dim is not None:
        fields["dim"] = str(dim)
    if chunk_count is not None:
        fields["chunk_count"] = str(chunk_count)
    write_corpus_meta(meta_path, **fields)


def chunk_count_from_meta(meta_path: Path) -> int:
    meta = read_corpus_meta(meta_path)
    raw = meta.get("chunk_count", "0")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def index_is_empty(meta_path: Path, db_path: Path | None = None) -> bool:
    """True when corpus has no indexed chunks."""
    if db_path is not None and not db_path.is_file():
        return True
    if not meta_path.is_file():
        return True
    return chunk_count_from_meta(meta_path) <= 0


def model_matches_index(meta_path: Path, embedding_model: str) -> bool:
    """False when stored embedding_model differs (requires cold rebuild)."""
    meta = read_corpus_meta(meta_path)
    stored = meta.get("embedding_model", "").strip()
    return stored == embedding_model.strip()


def schema_matches(meta_path: Path) -> bool:
    meta = read_corpus_meta(meta_path)
    return meta.get("schema_version", "") == SCHEMA_VERSION


def remove_stale_corpus_stores(listing_root: str) -> bool:
    """Delete pre-v3 stores (Chroma dir, legacy index.db, separate fts5.db)."""
    base = folder_cache_dir(listing_root, create_parent=False)
    removed = False
    removed |= _remove_path(base / CHROMA_SUBDIR)
    removed |= _remove_path(base / LEGACY_INDEX_DB)
    removed |= _remove_path(base / LEGACY_FTS_DB)
    removed |= _remove_path(base / LEGACY_FTS_META)
    if removed:
        log.info("Removed stale embeddings stores in %s (corpus.db cold rebuild)", base)
    return removed


def remove_legacy_index(listing_root: str) -> bool:
    """Backward-compatible alias for stale store cleanup."""
    return remove_stale_corpus_stores(listing_root)


def clear_folder_cache(listing_root: str) -> None:
    """Remove corpus.db and JSON state for a cold rebuild. Also clears zvec store for the folder."""
    base = folder_cache_dir(listing_root, create_parent=False)
    _remove_path(base / CORPUS_DB_FILENAME)
    remove_stale_corpus_stores(listing_root)
    for name in (CORPUS_META_FILENAME, LEGACY_FILE_INDEX_STATE_FILENAME):
        _remove_path(base / name)
    # Side-by-side zvec store (directory managed by zvec.create_and_open)
    _remove_path(base / "zvec")


def maybe_upgrade_legacy_index(listing_root: str) -> None:
    """On first access after upgrade, drop stale v1/v2 stores."""
    meta = corpus_meta_path(listing_root, create_parent=False)
    if schema_matches(meta):
        remove_stale_corpus_stores(listing_root)
        return
    clear_folder_cache(listing_root)


def resolve_index_context(ctx: Any, model: Any) -> tuple[str, Path, Path, str] | tuple[None, None, None, str]:
    """Return (folder_key, corpus_db_path, corpus_meta_path, listing_root) or error tuple."""
    listing_root = resolve_folder_for_active_doc(ctx, model)
    if not listing_root:
        return None, None, None, "No nearby files found. Save the document or open sibling files in LibreOffice."
    folder_key = folder_corpus_key(listing_root)
    maybe_upgrade_legacy_index(listing_root)
    db_path = corpus_db_path(listing_root)
    meta = corpus_meta_path(listing_root)
    return folder_key, db_path, meta, listing_root


def index_db_path(listing_root: str, *, create_parent: bool = True) -> Path:
    """Deprecated alias for corpus_db_path."""
    return corpus_db_path(listing_root, create_parent=create_parent)


def needs_cold_rebuild(meta_path: Path, embedding_model: str) -> bool:
    if not meta_path.is_file():
        return True
    if not schema_matches(meta_path):
        return True
    if chunk_count_from_meta(meta_path) == 0:
        return True
    # Model mismatch does not require a cold rebuild of the DB anymore;
    # missing vectors will be aligned incrementally.
    return False
