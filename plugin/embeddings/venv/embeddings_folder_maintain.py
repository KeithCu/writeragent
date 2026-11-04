# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Trusted venv folder corpus maintenance: ODF extract + unified corpus.db (FTS + vec0)."""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Literal

from plugin.embeddings.embeddings_cache import (
    CorpusMeta,
    SearchMode,
    clear_folder_cache,
    corpus_db_path,
    corpus_meta_path,
    folder_store,
    maybe_upgrade_legacy_index,
)
from plugin.embeddings.embeddings_fs import WriterFileEntry, chunk_to_index_row, guess_indexable_paths, indexable_chunks_from_path
from plugin.embeddings.venv.embeddings_ingest_graph import ingest_paragraphs
from plugin.embeddings.venv.embeddings_sqlite import (
    connect_corpus_db,
    corpus_chunk_count,
    delete_paragraph_keys,
    diff_chunk_rows_in_db,
    ensure_schema,
    file_is_stale_in_db,
    get_all_indexed_urls_in_db,
    has_missing_vector_chunks,
    insert_paragraph_rows,
    mark_file_indexed_in_db,
    rebuild_fts_corpus_index,
    remove_file_from_index_in_db,
    sync_file_paragraph_state_in_db,
)
from plugin.embeddings.venv.heartbeat_throttle import HeartbeatThrottle

log = logging.getLogger(__name__)

MaintainMode = Literal["auto", "cold", "incremental"]

__all__ = ["MaintainMode", "SearchMode", "maintain_folder_corpus", "maintain_folder_index"]


def _build_flags(search_mode: str) -> tuple[bool, bool]:
    build_fts = search_mode in ("fts", "hybrid", "llama_index", "zvec", "lancedb")
    build_vectors = search_mode in ("embeddings", "hybrid", "llama_index", "zvec", "lancedb")
    return build_fts, build_vectors


def _resolve_mode(listing_root: str, embedding_model: str, mode: MaintainMode, *, build_vectors: bool, search_mode: str = "") -> MaintainMode:
    meta_path = corpus_meta_path(listing_root, create_parent=False)
    db_path = corpus_db_path(listing_root, create_parent=False)
    store = folder_store(listing_root, meta_path, db_path, mode=search_mode)
    if build_vectors and store.meta.needs_cold_rebuild(embedding_model):
        # Model change must cold-rebuild even when the caller asked for incremental.
        return "cold"
    if mode != "auto":
        return mode
    if store.is_empty:
        # corpus.db is absent for zvec/lancedb; folder_store probes the backend dir.
        return "cold"
    if not store.meta.is_valid:
        # Non-object meta is not a schema mismatch (#1153); do not cold-wipe.
        return "incremental"
    if not store.meta.schema_matches():
        return "cold"
    return "incremental"


def _ingest_rows(
    listing_root: str, embedding_model: str, rows: list[dict[str, Any]], *, delete_keys: list[dict[str, Any]] | None = None, build_fts: bool, build_vectors: bool, search_mode: str = "embeddings", fill_vector_gaps: bool = False, heartbeat_fn: Callable[[dict[str, Any]], None] | None = None
) -> dict[str, Any]:
    db_path = str(corpus_db_path(listing_root))
    meta_path = str(corpus_meta_path(listing_root))
    if fill_vector_gaps:
        # The gap is rows in sqlite chunks with no vec_chunks_<model> vector.
        # LlamaIndex uses that same table. Its empty ingest returns before
        # any embed, so this call always uses the sqlite graph.
        return ingest_paragraphs(db_path, meta_path, embedding_model, rows, delete_keys=list(delete_keys or []), build_fts=build_fts, build_vectors=True, fill_vector_gaps=True, heartbeat_fn=heartbeat_fn)
    if search_mode == "llama_index":
        from plugin.embeddings.venv.embeddings_llama_index import llama_index_ingest

        return llama_index_ingest(db_path, meta_path, embedding_model, rows, delete_keys=delete_keys, build_fts=build_fts, build_vectors=build_vectors, heartbeat_fn=heartbeat_fn)

    if build_vectors:
        return ingest_paragraphs(db_path, meta_path, embedding_model, rows, delete_keys=list(delete_keys or []), build_fts=build_fts, build_vectors=True, heartbeat_fn=heartbeat_fn)
    conn = connect_corpus_db(db_path)
    try:
        ensure_schema(conn, with_fts=build_fts, with_vec=False)
        delete_paragraph_keys(conn, list(delete_keys or []), with_fts=build_fts, with_vec=False)
        inserted = insert_paragraph_rows(conn, rows, with_fts=build_fts)
        count = corpus_chunk_count(conn)
        CorpusMeta.write_row_count(listing_root, count)
        return {"indexed": inserted, "dim": 0, "storage_backend": "sqlite_vec", "upserted": inserted}
    finally:
        conn.close()


def _extract_file_chunks(entry: WriterFileEntry) -> tuple[int, list[Any] | None]:
    """Return native passage count and embed chunk rows for one file."""
    return indexable_chunks_from_path(entry.path, doc_url=entry.url, file_mtime=entry.modified)


def _cold_build(listing_root: str, embedding_model: str, files: list[WriterFileEntry], hb: HeartbeatThrottle, *, build_fts: bool, build_vectors: bool, search_mode: str = "embeddings") -> dict[str, Any]:
    clear_folder_cache(listing_root)
    if build_vectors:
        CorpusMeta.ensure(corpus_meta_path(listing_root), embedding_model=embedding_model)
    db_path = corpus_db_path(listing_root)
    indexed = 0
    upserted = 0
    total = len(files)
    conn = connect_corpus_db(db_path)
    try:
        ensure_schema(conn, with_fts=build_fts, with_vec=False)
        for index, entry in enumerate(files):
            hb.emit({"phase": "extract", "file": entry.name, "index": index, "total": total, "mode": "cold"}, immediate=True)
            try:
                paragraph_count, chunks = _extract_file_chunks(entry)
            except Exception:
                log.warning("Extraction failed for %s", entry.name, exc_info=True)
                continue
            if chunks is None:
                continue
            rows = [chunk_to_index_row(chunk) for chunk in chunks]
            hb.emit({"phase": "extract", "file": entry.name, "paragraphs": paragraph_count, "chunks": len(rows), "mode": "cold"}, immediate=True)
            if not rows:
                sync_file_paragraph_state_in_db(conn, entry.url, chunks, entry.modified, indexed_at=time.time())
                continue
            phase = "embed" if build_vectors else "index"
            result = _ingest_rows(listing_root, embedding_model, rows, build_fts=build_fts, build_vectors=build_vectors, search_mode=search_mode, heartbeat_fn=lambda p: hb.emit(p, immediate=True))
            file_upserted = int(result.get("upserted") or result.get("indexed") or 0)
            hb.emit({"phase": phase, "file": entry.name, "paragraphs": paragraph_count, "chunks": file_upserted, "upserted": file_upserted, "mode": "cold"}, immediate=True)
            sync_file_paragraph_state_in_db(conn, entry.url, chunks, entry.modified, indexed_at=time.time())
            indexed += len(rows)
            upserted += file_upserted
        row_count = corpus_chunk_count(conn)
    finally:
        conn.close()
    CorpusMeta.write_row_count(listing_root, row_count, embedding_model=embedding_model)

    return {"mode": "cold", "indexed_paragraphs": indexed, "files": total, "upserted": upserted, "row_count": row_count}


def _incremental_refresh(listing_root: str, embedding_model: str, files: list[WriterFileEntry], hb: HeartbeatThrottle, *, build_fts: bool, build_vectors: bool, search_mode: str = "embeddings") -> dict[str, Any]:
    db_path = corpus_db_path(listing_root)
    indexed = 0
    deleted = 0
    files_touched = 0
    total = len(files)

    current_urls = {entry.url for entry in files}
    conn = connect_corpus_db(db_path)
    try:
        ensure_schema(conn, with_fts=build_fts, with_vec=False)
        indexed_urls = get_all_indexed_urls_in_db(conn)

        for url in indexed_urls:
            if url in current_urls:
                continue
            _to_index, to_delete = diff_chunk_rows_in_db(conn, url, [])
            if to_delete:
                file_name = url.split("/")[-1] if "/" in url else url
                hb.emit({"phase": "delete", "file": file_name, "keys": len(to_delete)}, immediate=True)
                _ingest_rows(listing_root, embedding_model, [], delete_keys=to_delete, build_fts=build_fts, build_vectors=build_vectors, search_mode=search_mode, heartbeat_fn=lambda p: hb.emit(p, immediate=True))
                deleted += len(to_delete)
            remove_file_from_index_in_db(conn, url)
            files_touched += 1

        for index, entry in enumerate(files):
            hb.emit({"phase": "scan", "file": entry.name, "index": index, "total": total})
            if not file_is_stale_in_db(conn, entry.url, entry.modified):
                continue
            hb.emit({"phase": "extract", "file": entry.name, "index": index, "total": total, "mode": "incremental"}, immediate=True)
            try:
                paragraph_count, chunks = _extract_file_chunks(entry)
            except Exception:
                log.warning("Extraction failed for %s", entry.name, exc_info=True)
                continue
            if chunks is None:
                continue
            to_index, to_delete = diff_chunk_rows_in_db(conn, entry.url, chunks)
            hb.emit(
                {
                    "phase": "extract",
                    "file": entry.name,
                    "paragraphs": paragraph_count,
                    "chunks": len(chunks),
                    "mode": "incremental",
                },
                immediate=True,
            )
            if to_delete or to_index:
                if to_delete:
                    hb.emit({"phase": "delete", "file": entry.name, "keys": len(to_delete)}, immediate=True)
                    deleted += len(to_delete)

                phase = "embed" if build_vectors and to_index else "index"
                result = _ingest_rows(listing_root, embedding_model, to_index, delete_keys=to_delete, build_fts=build_fts, build_vectors=build_vectors, search_mode=search_mode, heartbeat_fn=lambda p: hb.emit(p, immediate=True))
                if to_index:
                    file_upserted = int(result.get("upserted") or result.get("indexed") or 0)
                    hb.emit({"phase": phase, "file": entry.name, "paragraphs": paragraph_count, "chunks": file_upserted, "upserted": file_upserted, "mode": "incremental"}, immediate=True)
                    indexed += len(to_index)
                files_touched += 1
                sync_file_paragraph_state_in_db(conn, entry.url, chunks, entry.modified, indexed_at=time.time())
            else:
                mark_file_indexed_in_db(conn, entry.url, entry.modified, indexed_at=time.time())
                files_touched += 1

        row_count = corpus_chunk_count(conn)
    finally:
        conn.close()

    # Same-model gaps only. A different embedding model never reaches here:
    # _resolve_mode cold-rebuilds so search does not query a wrong-dim table.
    if build_vectors:
        has_missing = False
        conn = connect_corpus_db(db_path)
        try:
            from plugin.embeddings.venv.embeddings_sqlite import _load_vec_extension

            _load_vec_extension(conn)
            has_missing = has_missing_vector_chunks(conn, embedding_model)
        except Exception:
            log.exception("Error checking for missing vectors in vector alignment check")
            has_missing = True
        finally:
            conn.close()

        if has_missing:
            _ingest_rows(listing_root, embedding_model, [], build_fts=False, build_vectors=True, search_mode=search_mode, fill_vector_gaps=True, heartbeat_fn=lambda p: hb.emit(p, immediate=True))

    CorpusMeta.write_row_count(listing_root, row_count, embedding_model=embedding_model)

    return {"mode": "incremental", "indexed_paragraphs": indexed, "deleted_paragraphs": deleted, "files_touched": files_touched, "files": total, "row_count": row_count}


def maintain_folder_corpus(listing_root: str, *, embedding_model: str = "", search_mode: str = "embeddings", mode: MaintainMode = "auto", heartbeat_fn: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    """Full folder corpus maintenance (ODF extract + corpus.db FTS and/or vec0)."""
    model = (embedding_model or "").strip()
    build_fts, build_vectors = _build_flags(search_mode)
    if build_vectors and not model:
        raise ValueError("embedding model name is required for embeddings/hybrid modes")
    root = str(listing_root or "").strip()
    if not root:
        raise ValueError("listing_root is required")

    maybe_upgrade_legacy_index(root)
    resolved_mode = _resolve_mode(root, model, mode, build_vectors=build_vectors, search_mode=search_mode)
    hb = HeartbeatThrottle(heartbeat_fn)
    hb.emit({"phase": "start", "mode": resolved_mode, "listing_root": root, "search_mode": search_mode}, immediate=True)

    if search_mode in ("zvec", "lancedb"):
        # These stores ignore MaintainMode and reopen the existing collection.
        # Its vector dimension is fixed at create time. A cold resolution
        # (model change, empty collection, schema) must delete that store
        # first or the next query hits the previous dimension. Missing
        # corpus.db is not a cold signal: zvec and lancedb never create it.
        if resolved_mode == "cold":
            clear_folder_cache(root)
        if search_mode == "zvec":
            from plugin.embeddings.venv.embeddings_zvec import maintain_folder_zvec

            return maintain_folder_zvec(root, model, mode=resolved_mode, heartbeat_fn=heartbeat_fn, hb=hb)
        from plugin.embeddings.venv.embeddings_lancedb import maintain_folder_lancedb

        return maintain_folder_lancedb(root, model, mode=resolved_mode, heartbeat_fn=heartbeat_fn, hb=hb)

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

    hb.emit({"phase": "done", **out}, immediate=True)
    log.info("Corpus maintain %s (%s) for %s: %s", resolved_mode, search_mode, root, out)
    return out


def maintain_folder_index(listing_root: str, *, embedding_model: str, mode: MaintainMode = "auto", heartbeat_fn: Callable[[dict[str, Any]], None] | None = None, search_mode: str = "embeddings") -> dict[str, Any]:
    """Public helper/RPC name; implements via :func:`maintain_folder_corpus`."""
    return maintain_folder_corpus(listing_root, embedding_model=embedding_model, search_mode=search_mode, mode=mode, heartbeat_fn=heartbeat_fn)
