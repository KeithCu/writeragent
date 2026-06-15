# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Trusted venv folder corpus maintenance: ODF extract + unified corpus.db (FTS + vec0)."""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable, Literal

from plugin.embeddings.embeddings_cache import (
    clear_folder_cache,
    corpus_db_path,
    corpus_meta_path,
    diff_paragraph_rows,
    ensure_corpus_meta,
    file_is_stale,
    index_is_empty,
    mark_file_indexed,
    maybe_upgrade_legacy_index,
    needs_cold_rebuild,
    sync_file_paragraph_state,
    write_corpus_meta,
)
from plugin.embeddings.embeddings_fs import (
    WriterFileEntry,
    chunk_to_index_row,
    guess_indexable_paths,
    indexable_chunks_from_path,
)
from plugin.embeddings.venv.embeddings_ingest_graph import ingest_paragraphs
from plugin.embeddings.venv.embeddings_sqlite import (
    connect_corpus_db,
    corpus_chunk_count,
    delete_paragraph_keys,
    ensure_schema,
    insert_paragraph_rows,
    rebuild_fts_corpus_index,
    model_slug,
)
from plugin.framework.constants import EMBEDDINGS_HEARTBEAT_INTERVAL_S, EMBEDDINGS_SCHEMA_VERSION

log = logging.getLogger(__name__)

MaintainMode = Literal["auto", "cold", "incremental"]
SearchMode = Literal["fts", "embeddings", "hybrid"]

__all__ = ["MaintainMode", "SearchMode", "maintain_folder_corpus", "maintain_folder_index"]


class _HeartbeatThrottle:
    def __init__(self, heartbeat_fn: Callable[[dict[str, Any]], None] | None) -> None:
        self._fn = heartbeat_fn
        self._last = 0.0

    def ping(self, payload: dict[str, Any]) -> None:
        if self._fn is None:
            return
        now = time.monotonic()
        if now - self._last < EMBEDDINGS_HEARTBEAT_INTERVAL_S:
            return
        self._last = now
        self._fn(payload)

    def force(self, payload: dict[str, Any]) -> None:
        if self._fn is None:
            return
        self._last = time.monotonic()
        self._fn(payload)


def _build_flags(search_mode: str) -> tuple[bool, bool]:
    mode = str(search_mode or "").strip().lower()
    build_fts = mode in ("fts", "hybrid", "llama_index", "zvec", "chroma", "lancedb")
    build_vectors = mode in ("embeddings", "hybrid", "llama_index", "zvec", "chroma", "lancedb")
    return build_fts, build_vectors


def _resolve_mode(
    listing_root: str,
    embedding_model: str,
    mode: MaintainMode,
    *,
    build_vectors: bool,
) -> MaintainMode:
    if mode != "auto":
        return mode
    meta_path = corpus_meta_path(listing_root, create_parent=False)
    db_path = corpus_db_path(listing_root, create_parent=False)
    if index_is_empty(meta_path, db_path):
        return "cold"
    if build_vectors and needs_cold_rebuild(meta_path, embedding_model):
        return "cold"
    meta = {}
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            meta = {}
    if str(meta.get("schema_version", "")) != EMBEDDINGS_SCHEMA_VERSION:
        return "cold"
    return "incremental"


def _write_row_count_meta(listing_root: str, row_count: int, *, embedding_model: str = "", dim: int = 0) -> None:
    fields: dict[str, str] = {
        "schema_version": EMBEDDINGS_SCHEMA_VERSION,
        "storage_backend": "sqlite_vec",
        "row_count": str(row_count),
        "chunk_count": str(row_count),
        "updated_at": str(time.time()),
    }
    if embedding_model:
        fields["embedding_model"] = embedding_model
    if dim > 0:
        fields["dim"] = str(dim)
    write_corpus_meta(corpus_meta_path(listing_root), **fields)


def _ingest_rows(
    listing_root: str,
    embedding_model: str,
    rows: list[dict[str, Any]],
    *,
    delete_keys: list[dict[str, Any]] | None = None,
    build_fts: bool,
    build_vectors: bool,
    search_mode: str = "embeddings",
) -> dict[str, Any]:
    db_path = str(corpus_db_path(listing_root))
    meta_path = str(corpus_meta_path(listing_root))
    if str(search_mode).strip().lower() == "llama_index":
        from plugin.embeddings.venv.embeddings_llama_index import llama_index_ingest
        return llama_index_ingest(
            db_path,
            meta_path,
            embedding_model,
            rows,
            delete_keys=delete_keys,
            build_fts=build_fts,
            build_vectors=build_vectors,
        )

    if str(search_mode).strip().lower() == "chroma":
        from plugin.embeddings.venv.embeddings_chroma import chroma_ingest
        return chroma_ingest(
            db_path,
            meta_path,
            embedding_model,
            rows,
            delete_keys=delete_keys,
            build_fts=build_fts,
            build_vectors=build_vectors,
        )

    if build_vectors:
        return ingest_paragraphs(
            db_path,
            meta_path,
            embedding_model,
            rows,
            delete_keys=list(delete_keys or []),
            build_fts=build_fts,
            build_vectors=True,
        )
    conn = connect_corpus_db(db_path)
    try:
        ensure_schema(conn, with_fts=build_fts, with_vec=False)
        delete_paragraph_keys(conn, list(delete_keys or []), with_fts=build_fts, with_vec=False)
        inserted = insert_paragraph_rows(conn, rows, with_fts=build_fts)
        count = corpus_chunk_count(conn)
        _write_row_count_meta(listing_root, count)
        return {"indexed": inserted, "dim": 0, "storage_backend": "sqlite_vec", "upserted": inserted}
    finally:
        conn.close()


def _extract_file_chunks(entry: WriterFileEntry) -> tuple[int, list[Any]]:
    """Return native passage count and embed chunk rows for one file."""
    return indexable_chunks_from_path(entry.path, doc_url=entry.url, file_mtime=entry.modified)


def _cold_build(
    listing_root: str,
    embedding_model: str,
    files: list[WriterFileEntry],
    hb: _HeartbeatThrottle,
    *,
    build_fts: bool,
    build_vectors: bool,
    search_mode: str = "embeddings",
) -> dict[str, Any]:
    clear_folder_cache(listing_root)
    if build_vectors:
        ensure_corpus_meta(corpus_meta_path(listing_root), embedding_model=embedding_model)
    db_path = corpus_db_path(listing_root)
    indexed = 0
    upserted = 0
    total = len(files)

    for index, entry in enumerate(files):
        hb.force({"phase": "extract", "file": entry.name, "index": index, "total": total, "mode": "cold"})
        paragraph_count, chunks = _extract_file_chunks(entry)
        rows = [chunk_to_index_row(chunk) for chunk in chunks]
        hb.force(
            {
                "phase": "extract",
                "file": entry.name,
                "paragraphs": paragraph_count,
                "chunks": len(rows),
                "mode": "cold",
            }
        )
        if not rows:
            sync_file_paragraph_state(db_path, entry.url, chunks, entry.modified)
            continue
        phase = "embed" if build_vectors else "index"
        result = _ingest_rows(
            listing_root,
            embedding_model,
            rows,
            build_fts=build_fts,
            build_vectors=build_vectors,
            search_mode=search_mode,
        )
        file_upserted = int(result.get("upserted") or result.get("indexed") or 0)
        hb.force(
            {
                "phase": phase,
                "file": entry.name,
                "paragraphs": paragraph_count,
                "chunks": file_upserted,
                "upserted": file_upserted,
                "mode": "cold",
            }
        )
        sync_file_paragraph_state(db_path, entry.url, chunks, entry.modified)
        indexed += len(rows)
        upserted += file_upserted

    db_path = corpus_db_path(listing_root, create_parent=False)
    row_count = 0
    if db_path.is_file():
        conn = connect_corpus_db(db_path)
        try:
            row_count = corpus_chunk_count(conn)
        finally:
            conn.close()
    _write_row_count_meta(listing_root, row_count, embedding_model=embedding_model)

    return {
        "mode": "cold",
        "indexed_paragraphs": indexed,
        "files": total,
        "upserted": upserted,
        "row_count": row_count,
    }


def _incremental_refresh(
    listing_root: str,
    embedding_model: str,
    files: list[WriterFileEntry],
    hb: _HeartbeatThrottle,
    *,
    build_fts: bool,
    build_vectors: bool,
    search_mode: str = "embeddings",
) -> dict[str, Any]:
    db_path = corpus_db_path(listing_root)
    indexed = 0
    deleted = 0
    files_touched = 0
    total = len(files)

    for index, entry in enumerate(files):
        hb.ping({"phase": "scan", "file": entry.name, "index": index, "total": total})
        if not file_is_stale(db_path, entry.url, entry.modified):
            continue
        hb.force({"phase": "extract", "file": entry.name, "index": index, "total": total, "mode": "incremental"})
        paragraph_count, chunks = _extract_file_chunks(entry)
        to_index, to_delete = diff_paragraph_rows(db_path, chunks)
        hb.force(
            {
                "phase": "extract",
                "file": entry.name,
                "paragraphs": paragraph_count,
                "chunks": len(chunks),
                "mode": "incremental",
            }
        )
        if to_delete:
            hb.force({"phase": "delete", "file": entry.name, "keys": len(to_delete)})
            _ingest_rows(
                listing_root,
                embedding_model,
                [],
                delete_keys=to_delete,
                build_fts=build_fts,
                build_vectors=build_vectors,
                search_mode=search_mode,
            )
            deleted += len(to_delete)
            sync_file_paragraph_state(db_path, entry.url, chunks, entry.modified)
        if to_index:
            phase = "embed" if build_vectors else "index"
            result = _ingest_rows(
                listing_root,
                embedding_model,
                to_index,
                build_fts=build_fts,
                build_vectors=build_vectors,
                search_mode=search_mode,
            )
            file_upserted = int(result.get("upserted") or result.get("indexed") or 0)
            hb.force(
                {
                    "phase": phase,
                    "file": entry.name,
                    "paragraphs": paragraph_count,
                    "chunks": file_upserted,
                    "upserted": file_upserted,
                    "mode": "incremental",
                }
            )
            sync_file_paragraph_state(db_path, entry.url, chunks, entry.modified)
            indexed += len(to_index)
            files_touched += 1
        elif not to_delete:
            mark_file_indexed(db_path, entry.url, entry.modified)
            files_touched += 1

    # Vector alignment check: ensure all chunks in DB are embedded for the active model
    if build_vectors:
        has_missing = False
        conn = connect_corpus_db(db_path)
        try:
            from plugin.embeddings.venv.embeddings_sqlite import _load_vec_extension
            _load_vec_extension(conn)
            slug = model_slug(embedding_model)
            tbl_name = f"vec_chunks_{slug}"
            # Check if table exists
            has_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (tbl_name,),
            ).fetchone()
            if not has_table:
                has_missing = True
            else:
                row = conn.execute(
                    f"SELECT 1 FROM chunks WHERE chunk_id NOT IN (SELECT chunk_id FROM {tbl_name}) LIMIT 1"
                ).fetchone()
                if row is not None:
                    has_missing = True
        except Exception:
            log.exception("Error checking for missing vectors in vector alignment check")
            has_missing = True
        finally:
            conn.close()

        if has_missing:
            _ingest_rows(
                listing_root,
                embedding_model,
                [],
                build_fts=False,
                build_vectors=True,
                search_mode=search_mode,
            )

    db_path_final = corpus_db_path(listing_root, create_parent=False)
    row_count = 0
    if db_path_final.is_file():
        conn = connect_corpus_db(db_path_final)
        try:
            row_count = corpus_chunk_count(conn)
        finally:
            conn.close()
    _write_row_count_meta(listing_root, row_count, embedding_model=embedding_model)

    return {
        "mode": "incremental",
        "indexed_paragraphs": indexed,
        "deleted_paragraphs": deleted,
        "files_touched": files_touched,
        "files": total,
        "row_count": row_count,
    }


def maintain_folder_corpus(
    listing_root: str,
    *,
    embedding_model: str = "",
    search_mode: str = "embeddings",
    mode: MaintainMode = "auto",
    heartbeat_fn: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Full folder corpus maintenance (ODF extract + corpus.db FTS and/or vec0)."""
    model = (embedding_model or "").strip()
    build_fts, build_vectors = _build_flags(search_mode)
    if build_vectors and not model:
        raise ValueError("embedding model name is required for embeddings/hybrid modes")
    root = str(listing_root or "").strip()
    if not root:
        raise ValueError("listing_root is required")

    maybe_upgrade_legacy_index(root)
    resolved_mode = _resolve_mode(root, model, mode, build_vectors=build_vectors)
    hb = _HeartbeatThrottle(heartbeat_fn)
    hb.force({"phase": "start", "mode": resolved_mode, "listing_root": root, "search_mode": search_mode})

    if str(search_mode or "").strip().lower() == "zvec":
        # Zvec is a full replacement store (dense + FTS + hybrid native). Side-by-side with sqlite corpus.
        from plugin.embeddings.venv.embeddings_zvec import maintain_folder_zvec

        return maintain_folder_zvec(
            root,
            model,
            mode=mode,
            heartbeat_fn=heartbeat_fn,
            hb=hb,
        )

    if str(search_mode or "").strip().lower() == "lancedb":
        from plugin.embeddings.venv.embeddings_lancedb import maintain_folder_lancedb

        return maintain_folder_lancedb(
            root,
            model,
            mode=mode,
            heartbeat_fn=heartbeat_fn,
            hb=hb,
        )

    files = guess_indexable_paths(root)
    if resolved_mode == "cold":
        out = _cold_build(root, model, files, hb, build_fts=build_fts, build_vectors=build_vectors, search_mode=search_mode)
    else:
        out = _incremental_refresh(root, model, files, hb, build_fts=build_fts, build_vectors=build_vectors, search_mode=search_mode)

    if build_fts:
        db_path = corpus_db_path(root, create_parent=False)
        if db_path.is_file():
            conn = connect_corpus_db(db_path)
            try:
                rebuild_fts_corpus_index(conn)
            finally:
                conn.close()

    hb.force({"phase": "done", **out})
    log.info("Corpus maintain %s (%s) for %s: %s", resolved_mode, search_mode, root, out)
    return out


def maintain_folder_index(
    listing_root: str,
    *,
    embedding_model: str,
    mode: MaintainMode = "auto",
    heartbeat_fn: Callable[[dict[str, Any]], None] | None = None,
    search_mode: str = "embeddings",
) -> dict[str, Any]:
    """Backward-compatible alias for maintain_folder_corpus."""
    return maintain_folder_corpus(
        listing_root,
        embedding_model=embedding_model,
        search_mode=search_mode,
        mode=mode,
        heartbeat_fn=heartbeat_fn,
    )
