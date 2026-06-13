# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Per-folder corpus cache paths and host-side index state (sqlite-vec + JSON)."""
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
FILE_INDEX_STATE_FILENAME = "file_index_state.json"
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


def chroma_persist_dir(listing_root: str, *, create_parent: bool = True) -> Path:
    """Deprecated alias — returns corpus.db path (historical Chroma API name)."""
    return corpus_db_path(listing_root, create_parent=create_parent)


def corpus_meta_path(listing_root: str, *, create_parent: bool = True) -> Path:
    """JSON corpus metadata beside corpus.db."""
    return folder_cache_dir(listing_root, create_parent=create_parent) / CORPUS_META_FILENAME


def file_index_state_path(listing_root: str, *, create_parent: bool = True) -> Path:
    """Host-side paragraph/file indexing state for incremental maintenance."""
    return folder_cache_dir(listing_root, create_parent=create_parent) / FILE_INDEX_STATE_FILENAME


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


def read_file_index_state(state_path: Path) -> dict[str, Any]:
    if not state_path.is_file():
        return {"files": {}}
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log.debug("read_file_index_state failed for %s", state_path, exc_info=True)
        return {"files": {}}
    if not isinstance(data, dict):
        return {"files": {}}
    files = data.get("files")
    if not isinstance(files, dict):
        data["files"] = {}
    return data


def write_file_index_state(state_path: Path, state: dict[str, Any]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


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
    if not stored:
        return True
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
    """Remove corpus.db and JSON state for a cold rebuild."""
    base = folder_cache_dir(listing_root, create_parent=False)
    _remove_path(base / CORPUS_DB_FILENAME)
    remove_stale_corpus_stores(listing_root)
    for name in (CORPUS_META_FILENAME, FILE_INDEX_STATE_FILENAME):
        _remove_path(base / name)


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


def _file_entry(state: dict[str, Any], doc_url: str) -> dict[str, Any]:
    files = state.setdefault("files", {})
    if not isinstance(files, dict):
        files = {}
        state["files"] = files
    entry = files.get(doc_url)
    if not isinstance(entry, dict):
        entry = {"file_mtime": 0.0, "last_indexed_at": 0.0, "paragraphs": {}}
        files[doc_url] = entry
    if "paragraphs" not in entry or not isinstance(entry["paragraphs"], dict):
        entry["paragraphs"] = {}
    return entry


def get_file_index_state(state_path: Path, doc_url: str) -> dict[str, float | int]:
    """Return stored file_mtime, last_indexed_at, and paragraph count for *doc_url*."""
    state = read_file_index_state(state_path)
    entry = (state.get("files") or {}).get(doc_url)
    if not isinstance(entry, dict):
        return {"file_mtime": 0.0, "last_indexed_at": 0.0, "chunk_count": 0}
    paragraphs = entry.get("paragraphs") or {}
    para_count = len(paragraphs) if isinstance(paragraphs, dict) else 0
    return {
        "file_mtime": float(entry.get("file_mtime") or 0.0),
        "last_indexed_at": float(entry.get("last_indexed_at") or 0.0),
        "chunk_count": int(para_count),
    }


def file_is_stale(state_path: Path, doc_url: str, file_mtime: float) -> bool:
    """True when filesystem mtime is newer than last indexed timestamp for *doc_url*."""
    info = get_file_index_state(state_path, doc_url)
    if info["chunk_count"] == 0:
        return True
    return float(file_mtime) > float(info["last_indexed_at"])


def mark_file_indexed(
    state_path: Path,
    doc_url: str,
    file_mtime: float,
    *,
    indexed_at: float | None = None,
    paragraphs: dict[str, str] | None = None,
) -> None:
    """Advance last_indexed_at/file_mtime for *doc_url*."""
    ts = float(indexed_at if indexed_at is not None else time.time())
    state = read_file_index_state(state_path)
    entry = _file_entry(state, doc_url)
    entry["file_mtime"] = float(file_mtime)
    entry["last_indexed_at"] = ts
    if paragraphs is not None:
        entry["paragraphs"] = {str(k): str(v) for k, v in paragraphs.items()}
    write_file_index_state(state_path, state)


def diff_paragraph_rows(
    state_path: Path,
    chunks: list[Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (rows_to_index, keys_to_delete) comparing extracted chunks to host JSON state."""
    from plugin.embeddings.embeddings_fs import ParagraphChunk, chunk_to_index_row

    state = read_file_index_state(state_path)
    to_index: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()

    for chunk in chunks:
        if not isinstance(chunk, ParagraphChunk):
            continue
        key = (chunk.doc_url, chunk.para_index)
        seen.add(key)
        entry = _file_entry(state, chunk.doc_url)
        paragraphs = entry.get("paragraphs") or {}
        stored_hash = str(paragraphs.get(str(chunk.para_index), ""))
        if stored_hash == chunk.content_hash:
            continue
        to_index.append(chunk_to_index_row(chunk))

    if not chunks:
        return to_index, []

    doc_url = chunks[0].doc_url
    entry = _file_entry(state, doc_url)
    paragraphs = entry.get("paragraphs") or {}
    to_delete: list[dict[str, Any]] = []
    for para_key in list(paragraphs.keys()):
        try:
            para_index = int(para_key)
        except (TypeError, ValueError):
            continue
        if (doc_url, para_index) not in seen:
            to_delete.append({"doc_url": doc_url, "para_index": para_index})

    return to_index, to_delete


def sync_file_paragraph_state(state_path: Path, doc_url: str, chunks: list[Any], file_mtime: float) -> None:
    """Update paragraph hashes after a successful venv index pass."""
    from plugin.embeddings.embeddings_fs import ParagraphChunk

    paragraphs: dict[str, str] = {}
    for chunk in chunks:
        if isinstance(chunk, ParagraphChunk):
            paragraphs[str(chunk.para_index)] = chunk.content_hash
    mark_file_indexed(state_path, doc_url, file_mtime, paragraphs=paragraphs)


def needs_cold_rebuild(meta_path: Path, embedding_model: str) -> bool:
    if not meta_path.is_file():
        return True
    if not schema_matches(meta_path):
        return True
    if chunk_count_from_meta(meta_path) == 0:
        return True
    return not model_matches_index(meta_path, embedding_model)
