# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Unified per-folder corpus: chunks table + FTS5 external content + sqlite-vec vec0."""
from __future__ import annotations

import importlib
import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_CHUNKS_DDL = """
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id INTEGER PRIMARY KEY,
    doc_url TEXT NOT NULL,
    para_index INTEGER NOT NULL,
    char_start INTEGER,
    char_end INTEGER,
    content_hash TEXT NOT NULL,
    file_mtime REAL,
    embedding_model TEXT,
    body TEXT NOT NULL,
    UNIQUE(doc_url, para_index, char_start, char_end, content_hash)
);

CREATE TABLE IF NOT EXISTS indexed_files (
    doc_url TEXT PRIMARY KEY,
    file_mtime REAL NOT NULL DEFAULT 0,
    last_indexed_at REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS indexed_paragraphs (
    doc_url TEXT NOT NULL,
    para_index INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    PRIMARY KEY (doc_url, para_index)
);
"""

_FTS_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS passages USING fts5(
    body,
    doc_url UNINDEXED,
    para_index UNINDEXED,
    content_hash UNINDEXED,
    tokenize='porter unicode61',
    content='chunks',
    content_rowid='chunk_id'
);
"""


def _pip_install_hint() -> str:
    from plugin.embeddings.venv.embeddings_index import EMBEDDINGS_VENV_PIP_INSTALL

    return EMBEDDINGS_VENV_PIP_INSTALL


def _import_sqlite_vec() -> Any:
    try:
        return importlib.import_module("sqlite_vec")
    except ImportError as exc:
        raise ImportError(
            f"sqlite-vec is not installed in the configured Python venv. Install with: {_pip_install_hint()}"
        ) from exc


def _load_vec_extension(conn: sqlite3.Connection) -> None:
    sqlite_vec = _import_sqlite_vec()
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)


def _vec_table_ddl(dim: int) -> str:
    return f"""
CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks USING vec0(
    chunk_id INTEGER PRIMARY KEY,
    embedding float[{int(dim)}]
);
"""


def connect_corpus_db(db_path: str | Path) -> sqlite3.Connection:
    """Open corpus.db with row factory."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def ensure_schema(
    conn: sqlite3.Connection,
    *,
    dim: int | None = None,
    with_fts: bool = False,
    with_vec: bool = False,
) -> None:
    """Create chunks (+ optional FTS5 / vec0) tables."""
    conn.executescript(_CHUNKS_DDL)
    if with_fts:
        conn.execute(_FTS_DDL)
    if with_vec:
        _load_vec_extension(conn)
        has_vec = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='vec_chunks'"
        ).fetchone()
        if not has_vec:
            if dim is None or dim <= 0:
                raise ValueError("dim is required when creating vec_chunks")
            conn.execute(_vec_table_ddl(dim))
    conn.commit()


def _dim_from_meta_path(meta_path: str) -> int | None:
    path = Path(str(meta_path or ""))
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        raw = data.get("dim", "0")
        dim = int(raw)
        return dim if dim > 0 else None
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None


def corpus_chunk_count(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS c FROM chunks").fetchone()
    return int(row["c"] if row else 0)


def _fts_delete_row(conn: sqlite3.Connection, chunk_id: int) -> None:
    conn.execute("INSERT INTO passages(passages, rowid) VALUES ('delete', ?)", (int(chunk_id),))


def _fts_index_row(conn: sqlite3.Connection, chunk_id: int) -> None:
    conn.execute("INSERT INTO passages(rowid) VALUES (?)", (int(chunk_id),))


def _delete_chunk_ids(conn: sqlite3.Connection, chunk_ids: list[int], *, with_fts: bool, with_vec: bool) -> None:
    if not chunk_ids:
        return
    if with_vec:
        _load_vec_extension(conn)
    for chunk_id in chunk_ids:
        if with_fts:
            _fts_delete_row(conn, chunk_id)
        if with_vec:
            conn.execute("DELETE FROM vec_chunks WHERE chunk_id = ?", (int(chunk_id),))
        conn.execute("DELETE FROM chunks WHERE chunk_id = ?", (int(chunk_id),))


def delete_by_doc_para(
    conn: sqlite3.Connection,
    doc_url: str,
    para_index: int,
    *,
    with_fts: bool = False,
    with_vec: bool = False,
) -> int:
    """Remove all sub-chunks for one paragraph."""
    doc_url = str(doc_url or "")
    rows = conn.execute(
        "SELECT chunk_id FROM chunks WHERE doc_url = ? AND para_index = ?",
        (doc_url, int(para_index)),
    ).fetchall()
    chunk_ids = [int(row["chunk_id"]) for row in rows]
    _delete_chunk_ids(conn, chunk_ids, with_fts=with_fts, with_vec=with_vec)
    conn.commit()
    return len(chunk_ids)


def delete_paragraph_keys(
    conn: sqlite3.Connection,
    keys: list[dict[str, Any]],
    *,
    with_fts: bool = False,
    with_vec: bool = False,
) -> int:
    deleted = 0
    for key in keys or []:
        deleted += delete_by_doc_para(
            conn,
            str(key.get("doc_url") or ""),
            int(key.get("para_index") or 0),
            with_fts=with_fts,
            with_vec=with_vec,
        )
    return deleted


def _insert_chunk_row(
    conn: sqlite3.Connection,
    *,
    doc_url: str,
    para_index: int,
    char_start: int,
    char_end: int,
    content_hash: str,
    body: str,
    file_mtime: float,
    embedding_model: str,
    with_fts: bool,
) -> int:
    conn.execute(
        """
        INSERT INTO chunks (
            doc_url, para_index, char_start, char_end, content_hash,
            file_mtime, embedding_model, body
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            doc_url,
            int(para_index),
            int(char_start),
            int(char_end),
            content_hash,
            float(file_mtime),
            embedding_model,
            body,
        ),
    )
    chunk_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    if with_fts:
        _fts_index_row(conn, chunk_id)
    return chunk_id


def upsert_chunk_with_vector(
    conn: sqlite3.Connection,
    chunk: dict[str, Any],
    vector: list[float],
    *,
    model: str,
    with_fts: bool,
    with_vec: bool,
) -> int:
    """Replace any existing row for the same sub-chunk key; return chunk_id."""
    doc_url = str(chunk.get("doc_url") or "")
    para_index = int(chunk.get("para_index") or 0)
    char_start = int(chunk.get("char_start") or 0)
    char_end = int(chunk.get("char_end") or 0)
    content_hash = str(chunk.get("content_hash") or "")
    body = str(chunk.get("text") or chunk.get("body") or "").strip()
    if not body:
        return 0

    existing = conn.execute(
        """
        SELECT chunk_id FROM chunks
        WHERE doc_url = ? AND para_index = ? AND char_start = ? AND char_end = ? AND content_hash = ?
        """,
        (doc_url, para_index, char_start, char_end, content_hash),
    ).fetchone()
    if existing is not None:
        _delete_chunk_ids(conn, [int(existing["chunk_id"])], with_fts=with_fts, with_vec=with_vec)

    chunk_id = _insert_chunk_row(
        conn,
        doc_url=doc_url,
        para_index=para_index,
        char_start=char_start,
        char_end=char_end,
        content_hash=content_hash,
        body=body,
        file_mtime=float(chunk.get("file_mtime") or 0.0),
        embedding_model=model,
        with_fts=with_fts,
    )
    if with_vec:
        import numpy as np

        _load_vec_extension(conn)
        emb = np.asarray(vector, dtype=np.float32)
        conn.execute(
            "INSERT INTO vec_chunks(chunk_id, embedding) VALUES (?, ?)",
            (chunk_id, emb),
        )
    return chunk_id


def insert_paragraph_rows(
    conn: sqlite3.Connection,
    rows: list[dict[str, Any]],
    *,
    with_fts: bool,
) -> int:
    """Insert paragraph-level rows (FTS-only path; no sub-chunk split)."""
    inserted = 0
    for row in rows or []:
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        _insert_chunk_row(
            conn,
            doc_url=str(row.get("doc_url") or ""),
            para_index=int(row.get("para_index") or 0),
            char_start=0,
            char_end=len(text),
            content_hash=str(row.get("content_hash") or ""),
            body=text,
            file_mtime=float(row.get("file_mtime") or 0.0),
            embedding_model="",
            with_fts=with_fts,
        )
        inserted += 1
    conn.commit()
    return inserted


def vec0_search(
    conn: sqlite3.Connection,
    query_vec: list[float],
    *,
    k: int,
    model: str,
    doc_url_filter: str | None = None,
) -> list[dict[str, Any]]:
    """kNN over vec_chunks joined to chunks; returns candidate dicts for MMR."""
    import numpy as np

    _load_vec_extension(conn)
    count_row = conn.execute("SELECT COUNT(*) AS c FROM vec_chunks").fetchone()
    count = int(count_row["c"] if count_row else 0)
    if count == 0:
        return []

    limit = min(max(int(k), 1), count)
    q = np.asarray(query_vec, dtype=np.float32)
    rows = conn.execute(
        """
        SELECT
            v.chunk_id,
            v.distance,
            c.doc_url,
            c.para_index,
            c.embedding_model,
            c.body
        FROM vec_chunks v
        JOIN chunks c ON c.chunk_id = v.chunk_id
        WHERE v.embedding MATCH ?
          AND k = ?
        ORDER BY v.distance
        """,
        (q, limit),
    ).fetchall()

    candidates: list[dict[str, Any]] = []
    for row in rows:
        if doc_url_filter and str(row["doc_url"] or "") != doc_url_filter:
            continue
        emb_model = str(row["embedding_model"] or "")
        if emb_model and emb_model != model:
            continue
        dist = float(row["distance"] or 0.0)
        score = max(0.0, 1.0 - dist)
        candidates.append(
            {
                "chunk_id": int(row["chunk_id"]),
                "doc_url": str(row["doc_url"] or ""),
                "para_index": int(row["para_index"] or 0),
                "embedding_model": emb_model,
                "snippet": str(row["body"] or ""),
                "score": score,
                "distance": dist,
            }
        )
    return candidates


def fts_corpus_search(
    conn: sqlite3.Connection,
    query: str,
    *,
    k: int = 10,
    near_slop: int = 10,
) -> list[dict[str, Any]]:
    """BM25 + NEAR search on unified corpus.db passages (rowid = chunk_id)."""
    from plugin.embeddings.venv.folder_fts import build_match_query, strip_fts_snippet_markers

    limit = max(1, min(int(k or 10), 50))
    match_expr = build_match_query(str(query or ""), near_slop=near_slop)
    sql = """
        SELECT
            p.rowid AS chunk_id,
            c.doc_url,
            c.para_index,
            snippet(p, 0, '[', ']', '…', 32) AS snippet,
            bm25(p) AS score
        FROM passages p
        JOIN chunks c ON c.chunk_id = p.rowid
        WHERE p MATCH ?
        ORDER BY score
        LIMIT ?
    """
    try:
        rows = conn.execute(sql, (match_expr, limit)).fetchall()
    except sqlite3.OperationalError as exc:
        log.debug("FTS corpus search failed for %r: %s", match_expr, exc)
        return []

    hits: list[dict[str, Any]] = []
    for row in rows:
        hits.append(
            {
                "chunk_id": int(row["chunk_id"]),
                "doc_url": str(row["doc_url"] or ""),
                "para_index": int(row["para_index"] or 0),
                "snippet": strip_fts_snippet_markers(str(row["snippet"] or "")),
                "score": float(row["score"] or 0.0),
            }
        )
    return hits


def load_embeddings_for_candidates(
    conn: sqlite3.Connection,
    candidates: list[dict[str, Any]],
) -> None:
    """Attach vec0 embeddings to candidate dicts for MMR (mutates *candidates*)."""
    import numpy as np

    if not candidates:
        return
    _load_vec_extension(conn)
    ids = [int(c["chunk_id"]) for c in candidates if c.get("chunk_id") is not None]
    if not ids:
        return
    by_id: dict[int, Any] = {}
    for chunk_id in ids:
        row = conn.execute(
            "SELECT chunk_id, embedding FROM vec_chunks WHERE chunk_id = ?",
            (int(chunk_id),),
        ).fetchone()
        if row is not None:
            by_id[int(row["chunk_id"])] = row["embedding"]
    for cand in candidates:
        cid = cand.get("chunk_id")
        if cid is None:
            continue
        raw = by_id.get(int(cid))
        if raw is None:
            continue
        if isinstance(raw, (bytes, memoryview, bytearray)):
            cand["embedding"] = np.frombuffer(raw, dtype=np.float32).copy()
        else:
            cand["embedding"] = np.asarray(raw, dtype=np.float32)


def get_file_index_info(conn: sqlite3.Connection, doc_url: str) -> dict[str, float | int]:
    """Return stored file_mtime, last_indexed_at, and indexed paragraph count."""
    doc_url = str(doc_url or "")
    row = conn.execute(
        "SELECT file_mtime, last_indexed_at FROM indexed_files WHERE doc_url = ?",
        (doc_url,),
    ).fetchone()
    para_row = conn.execute(
        "SELECT COUNT(*) AS c FROM indexed_paragraphs WHERE doc_url = ?",
        (doc_url,),
    ).fetchone()
    para_count = int(para_row["c"] if para_row else 0)
    if row is None:
        return {"file_mtime": 0.0, "last_indexed_at": 0.0, "chunk_count": para_count}
    return {
        "file_mtime": float(row["file_mtime"] or 0.0),
        "last_indexed_at": float(row["last_indexed_at"] or 0.0),
        "chunk_count": para_count,
    }


def file_is_stale_in_db(conn: sqlite3.Connection, doc_url: str, file_mtime: float) -> bool:
    """True when filesystem mtime is newer than last indexed timestamp."""
    info = get_file_index_info(conn, doc_url)
    if info["chunk_count"] == 0:
        return True
    return float(file_mtime) > float(info["last_indexed_at"])


def mark_file_indexed_in_db(
    conn: sqlite3.Connection,
    doc_url: str,
    file_mtime: float,
    *,
    indexed_at: float,
    paragraphs: dict[str, str] | None = None,
) -> None:
    """Advance file timestamps and optionally replace paragraph content hashes."""
    doc_url = str(doc_url or "")
    conn.execute(
        """
        INSERT INTO indexed_files (doc_url, file_mtime, last_indexed_at)
        VALUES (?, ?, ?)
        ON CONFLICT(doc_url) DO UPDATE SET
            file_mtime = excluded.file_mtime,
            last_indexed_at = excluded.last_indexed_at
        """,
        (doc_url, float(file_mtime), float(indexed_at)),
    )
    if paragraphs is None:
        conn.commit()
        return
    conn.execute("DELETE FROM indexed_paragraphs WHERE doc_url = ?", (doc_url,))
    for para_key, para_hash in paragraphs.items():
        try:
            para_index = int(para_key)
        except (TypeError, ValueError):
            continue
        conn.execute(
            """
            INSERT INTO indexed_paragraphs (doc_url, para_index, content_hash)
            VALUES (?, ?, ?)
            """,
            (doc_url, para_index, str(para_hash)),
        )
    conn.commit()


def diff_paragraph_rows_in_db(
    conn: sqlite3.Connection,
    chunks: list[Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (rows_to_index, keys_to_delete) comparing extracted chunks to DB state."""
    from plugin.embeddings.embeddings_fs import ParagraphChunk, chunk_to_index_row

    to_index: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()

    stored_by_para: dict[int, str] = {}
    if chunks:
        doc_url = str(chunks[0].doc_url if isinstance(chunks[0], ParagraphChunk) else "")
        if doc_url:
            rows = conn.execute(
                "SELECT para_index, content_hash FROM indexed_paragraphs WHERE doc_url = ?",
                (doc_url,),
            ).fetchall()
            stored_by_para = {int(row["para_index"]): str(row["content_hash"] or "") for row in rows}

    for chunk in chunks:
        if not isinstance(chunk, ParagraphChunk):
            continue
        key = (chunk.doc_url, chunk.para_index)
        seen.add(key)
        stored_hash = stored_by_para.get(chunk.para_index, "")
        if stored_hash == chunk.content_hash:
            continue
        to_index.append(chunk_to_index_row(chunk))

    if not chunks:
        return to_index, []

    doc_url = chunks[0].doc_url
    to_delete: list[dict[str, Any]] = []
    for para_index in stored_by_para:
        if (doc_url, para_index) not in seen:
            to_delete.append({"doc_url": doc_url, "para_index": para_index})

    return to_index, to_delete


def sync_file_paragraph_state_in_db(
    conn: sqlite3.Connection,
    doc_url: str,
    chunks: list[Any],
    file_mtime: float,
    *,
    indexed_at: float,
) -> None:
    """Update paragraph hashes after a successful index pass."""
    from plugin.embeddings.embeddings_fs import ParagraphChunk

    paragraphs: dict[str, str] = {}
    for chunk in chunks:
        if isinstance(chunk, ParagraphChunk):
            paragraphs[str(chunk.para_index)] = chunk.content_hash
    mark_file_indexed_in_db(
        conn,
        doc_url,
        file_mtime,
        indexed_at=indexed_at,
        paragraphs=paragraphs,
    )


__all__ = [
    "connect_corpus_db",
    "corpus_chunk_count",
    "delete_by_doc_para",
    "delete_paragraph_keys",
    "diff_paragraph_rows_in_db",
    "ensure_schema",
    "file_is_stale_in_db",
    "fts_corpus_search",
    "get_file_index_info",
    "insert_paragraph_rows",
    "load_embeddings_for_candidates",
    "mark_file_indexed_in_db",
    "sync_file_paragraph_state_in_db",
    "upsert_chunk_with_vector",
    "vec0_search",
]
