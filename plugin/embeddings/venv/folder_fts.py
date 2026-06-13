# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Trusted venv folder FTS5 index: ODF extract + SQLite search (no UNO)."""
from __future__ import annotations

import logging
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Literal

from plugin.embeddings.embeddings_cache import (
    diff_paragraph_rows,
    file_index_state_path,
    file_is_stale,
    mark_file_indexed,
    sync_file_paragraph_state,
)
from plugin.embeddings.embeddings_fs import (
    ParagraphChunk,
    WriterFileEntry,
    guess_indexable_paths,
    paragraph_chunks_from_path,
)
from plugin.embeddings.folder_fts_cache import (
    FTS_SCHEMA_VERSION,
    fts_db_path,
    fts_meta_path,
    needs_fts_cold_rebuild,
    write_fts_meta,
)
from plugin.framework.constants import EMBEDDINGS_HEARTBEAT_INTERVAL_S

log = logging.getLogger(__name__)

MaintainMode = Literal["auto", "cold", "incremental"]

__all__ = [
    "MaintainMode",
    "build_match_query",
    "fts_stats",
    "maintain_folder_fts",
    "search_folder_fts",
    "strip_fts_snippet_markers",
]

_NEAR_SLASH_RE = re.compile(r"(.+?)\s+NEAR\s*/\s*(\d+)\s+(.+)", re.IGNORECASE)
_FTS5_NEAR_CALL_RE = re.compile(r"\bNEAR\s*\(", re.IGNORECASE)
_BOOL_RE = re.compile(r"\b(AND|OR|NOT)\b", re.IGNORECASE)

_CREATE_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS passages USING fts5(
    body,
    doc_url UNINDEXED,
    para_index UNINDEXED,
    content_hash UNINDEXED,
    tokenize='porter unicode61'
);
"""


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


def _escape_fts_token(token: str) -> str:
    cleaned = str(token or "").strip()
    if not cleaned:
        return '""'
    escaped = cleaned.replace('"', '""')
    return f'"{escaped}"'


def build_match_query(query: str, *, near_slop: int = 10) -> str:
    """Build an FTS5 MATCH expression (multi-word defaults to NEAR with slop)."""
    raw = str(query or "").strip()
    if not raw:
        raise ValueError("query is required")
    slop = max(0, int(near_slop))

    if _FTS5_NEAR_CALL_RE.search(raw):
        return raw
    if _BOOL_RE.search(raw):
        return raw

    near_match = _NEAR_SLASH_RE.search(raw)
    if near_match:
        left_tokens = [t for t in near_match.group(1).split() if t.strip()]
        right_tokens = [t for t in near_match.group(3).split() if t.strip()]
        dist = int(near_match.group(2))
        tokens = left_tokens + right_tokens
        if not tokens:
            raise ValueError("NEAR query has no terms")
        quoted = " ".join(_escape_fts_token(t) for t in tokens)
        return f"NEAR({quoted}, {dist})"

    tokens = [t for t in raw.split() if t.strip()]
    if len(tokens) == 1:
        return _escape_fts_token(tokens[0])
    quoted = " ".join(_escape_fts_token(t) for t in tokens)
    return f"NEAR({quoted}, {slop})"


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_CREATE_SQL)
    conn.commit()


def _clear_fts_files(listing_root: str) -> None:
    from plugin.embeddings.embeddings_cache import clear_folder_cache

    clear_folder_cache(listing_root)


def _count_rows(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS c FROM passages").fetchone()
    return int(row["c"] if row else 0)


def _write_meta(listing_root: str, row_count: int) -> None:
    write_fts_meta(
        fts_meta_path(listing_root),
        schema_version=FTS_SCHEMA_VERSION,
        row_count=str(row_count),
        updated_at=str(time.time()),
    )


def _insert_rows(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    conn.executemany(
        "INSERT INTO passages(body, doc_url, para_index, content_hash) VALUES (?, ?, ?, ?)",
        [
            (
                str(row.get("text") or ""),
                str(row.get("doc_url") or ""),
                int(row.get("para_index") or 0),
                str(row.get("content_hash") or ""),
            )
            for row in rows
        ],
    )
    conn.commit()


def _delete_keys(conn: sqlite3.Connection, keys: list[dict[str, Any]]) -> None:
    if not keys:
        return
    conn.executemany(
        "DELETE FROM passages WHERE doc_url = ? AND para_index = ?",
        [(str(k.get("doc_url") or ""), int(k.get("para_index") or 0)) for k in keys],
    )
    conn.commit()


def _resolve_mode(listing_root: str, mode: MaintainMode) -> MaintainMode:
    if mode != "auto":
        return mode
    db = fts_db_path(listing_root, create_parent=False)
    meta = fts_meta_path(listing_root, create_parent=False)
    if needs_fts_cold_rebuild(meta, db):
        return "cold"
    return "incremental"


def _cold_build(
    listing_root: str,
    files: list[WriterFileEntry],
    hb: _HeartbeatThrottle,
) -> dict[str, Any]:
    _clear_fts_files(listing_root)
    db = fts_db_path(listing_root)
    all_rows: list[dict[str, Any]] = []
    file_chunks: dict[str, list[ParagraphChunk]] = {}
    total = len(files)

    for index, entry in enumerate(files):
        hb.force({"phase": "extract", "file": entry.name, "index": index, "total": total, "mode": "cold"})
        chunks = paragraph_chunks_from_path(entry.path, doc_url=entry.url, file_mtime=entry.modified)
        file_chunks[entry.url] = chunks
        for chunk in chunks:
            all_rows.append(
                {
                    "text": chunk.text,
                    "doc_url": chunk.doc_url,
                    "para_index": chunk.para_index,
                    "content_hash": chunk.content_hash,
                }
            )
        hb.ping({"phase": "extract", "file": entry.name, "paragraphs": len(chunks)})

    if not all_rows:
        _write_meta(listing_root, 0)
        return {"mode": "cold", "indexed_paragraphs": 0, "files": total}

    hb.force({"phase": "index", "paragraphs": len(all_rows), "mode": "cold"})
    with _connect(db) as conn:
        _ensure_schema(conn)
        _insert_rows(conn, all_rows)
        row_count = _count_rows(conn)

    state_path = file_index_state_path(listing_root)
    for entry in files:
        sync_file_paragraph_state(state_path, entry.url, file_chunks.get(entry.url, []), entry.modified)
    _write_meta(listing_root, row_count)

    return {
        "mode": "cold",
        "indexed_paragraphs": len(all_rows),
        "files": total,
        "row_count": row_count,
    }


def _incremental_refresh(
    listing_root: str,
    files: list[WriterFileEntry],
    hb: _HeartbeatThrottle,
) -> dict[str, Any]:
    db = fts_db_path(listing_root)
    state_path = file_index_state_path(listing_root)
    indexed = 0
    deleted = 0
    files_touched = 0
    total = len(files)

    with _connect(db) as conn:
        _ensure_schema(conn)
        for index, entry in enumerate(files):
            hb.ping({"phase": "scan", "file": entry.name, "index": index, "total": total})
            if not file_is_stale(state_path, entry.url, entry.modified):
                continue
            hb.force({"phase": "extract", "file": entry.name, "index": index, "total": total, "mode": "incremental"})
            chunks = paragraph_chunks_from_path(entry.path, doc_url=entry.url, file_mtime=entry.modified)
            to_index, to_delete = diff_paragraph_rows(state_path, chunks)
            if to_delete:
                hb.force({"phase": "delete", "file": entry.name, "keys": len(to_delete)})
                _delete_keys(conn, to_delete)
                deleted += len(to_delete)
            if to_index:
                hb.force({"phase": "index", "file": entry.name, "paragraphs": len(to_index)})
                _insert_rows(conn, to_index)
                sync_file_paragraph_state(state_path, entry.url, chunks, entry.modified)
                indexed += len(to_index)
                files_touched += 1
            elif not to_delete:
                mark_file_indexed(state_path, entry.url, entry.modified)
                files_touched += 1

        row_count = _count_rows(conn)

    _write_meta(listing_root, row_count)
    return {
        "mode": "incremental",
        "indexed_paragraphs": indexed,
        "deleted_paragraphs": deleted,
        "files_touched": files_touched,
        "files": total,
        "row_count": row_count,
    }


def maintain_folder_fts(
    listing_root: str,
    mode: MaintainMode = "auto",
    *,
    heartbeat_fn: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Full folder FTS maintenance — delegates to unified corpus maintain (FTS leg only)."""
    from plugin.embeddings.venv.embeddings_folder_maintain import maintain_folder_corpus

    return maintain_folder_corpus(
        listing_root,
        embedding_model="",
        search_mode="fts",
        mode=mode,
        heartbeat_fn=heartbeat_fn,
    )


def strip_fts_snippet_markers(snippet: str) -> str:
    """Remove FTS5 snippet() highlight brackets; readable plain text for agents and UI."""
    return str(snippet or "").replace("[", "").replace("]", "")


def search_folder_fts(
    fts_db_path_str: str,
    query: str,
    *,
    k: int = 10,
    near_slop: int = 10,
) -> dict[str, Any]:
    """BM25 search over a folder FTS5 index."""
    db_path = Path(str(fts_db_path_str or ""))
    if not db_path.is_file():
        return {"hits": [], "query": query, "match": ""}

    limit = max(1, min(int(k or 10), 30))
    match_expr = build_match_query(str(query or ""), near_slop=near_slop)
    sql = """
        SELECT
            doc_url,
            para_index,
            snippet(passages, 0, '[', ']', '…', 32) AS snippet,
            bm25(passages) AS score
        FROM passages
        WHERE passages MATCH ?
        ORDER BY score
        LIMIT ?
    """
    hits: list[dict[str, Any]] = []
    with _connect(db_path) as conn:
        try:
            rows = conn.execute(sql, (match_expr, limit)).fetchall()
        except sqlite3.OperationalError as exc:
            log.debug("FTS search failed for %r: %s", match_expr, exc)
            return {"hits": [], "query": query, "match": match_expr, "error": str(exc)}

    for row in rows:
        hits.append(
            {
                "doc_url": str(row["doc_url"] or ""),
                "para_index": int(row["para_index"] or 0),
                "snippet": strip_fts_snippet_markers(str(row["snippet"] or "")),
                "score": float(row["score"] or 0.0),
            }
        )
    return {"hits": hits, "query": query, "match": match_expr}


def fts_stats(fts_db_path_str: str, meta_path_str: str) -> dict[str, Any]:
    """Lightweight FTS corpus stats for host empty/stale checks."""
    db_path = Path(str(fts_db_path_str or ""))
    meta_path = Path(str(meta_path_str or ""))
    row_count = 0
    if db_path.is_file():
        with _connect(db_path) as conn:
            row_count = _count_rows(conn)
    from plugin.embeddings.folder_fts_cache import read_fts_meta

    meta = read_fts_meta(meta_path)
    return {
        "row_count": row_count,
        "schema_version": meta.get("schema_version", ""),
        "updated_at": meta.get("updated_at", ""),
    }
