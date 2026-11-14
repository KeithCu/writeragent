# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Unified per-folder corpus: chunks table + FTS5 external content + sqlite-vec vec0, plus path-level corpus access."""
from __future__ import annotations

import importlib
import logging
import ntpath
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, cast

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

CREATE TABLE IF NOT EXISTS model_metadata (
    embedding_model TEXT PRIMARY KEY,
    dim INTEGER NOT NULL,
    updated_at REAL NOT NULL
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


# Resolved DB-API module. None until the first connect so import stays cheap
# and tests can clear it. stdlib sqlite3 when that build can load extensions;
# otherwise pysqlite3 if it is installed.
_dbapi_mod: Any = None


def _dbapi() -> Any:
    """DB-API module for corpus connections.

    pysqlite3 bundles SQLite with extension loading. python.org macOS CPython
    links SQLite built with SQLITE_OMIT_LOAD_EXTENSION (Apple's libsqlite;
    CPython's ``--enable-loadable-sqlite-extensions`` defaults off), so
    ``conn.enable_load_extension`` is absent and vec0 schema setup never runs.
    sqlite-vec documents that error:
    https://alexgarcia.xyz/sqlite-vec/python.html

    Use pysqlite3 only when stdlib sqlite3 cannot load extensions and the
    package is installed. Linux, Windows, and Homebrew Python stay on stdlib
    sqlite3. A host interpreter without pysqlite3 (LibreOffice) still opens
    the corpus for metadata; vec loading reports the install line.
    """
    global _dbapi_mod
    if _dbapi_mod is not None:
        return _dbapi_mod
    if hasattr(sqlite3.Connection, "enable_load_extension"):
        _dbapi_mod = sqlite3
        return _dbapi_mod
    try:
        from pysqlite3 import dbapi2 as pysqlite3
    except ImportError:
        _dbapi_mod = sqlite3
        return _dbapi_mod
    if hasattr(getattr(pysqlite3, "Connection", None), "enable_load_extension"):
        _dbapi_mod = pysqlite3
        return _dbapi_mod
    _dbapi_mod = sqlite3
    return _dbapi_mod


def _conn_operational_error(conn: Any) -> type[BaseException]:
    """OperationalError class for the DB-API that created *conn*.

    pysqlite3.OperationalError does not subclass sqlite3.OperationalError.
    Catching only the stdlib class lets a missing vec_version() or a bad FTS
    query escape and abort the caller.
    """
    module_name = type(conn).__module__
    try:
        module = importlib.import_module(module_name)
    except ImportError:
        return sqlite3.OperationalError
    err = getattr(module, "OperationalError", None)
    if isinstance(err, type) and issubclass(err, Exception):
        return err
    return sqlite3.OperationalError


def _load_vec_extension(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("SELECT vec_version()")
        return
    except _conn_operational_error(conn):
        pass

    if not hasattr(conn, "enable_load_extension"):
        # Same macOS stdlib gap as _dbapi. This connection was not opened on
        # an extension-capable library (pysqlite3 missing in this interpreter).
        raise ImportError(
            "sqlite3.Connection.enable_load_extension is missing, so sqlite-vec "
            "cannot load. Install pysqlite3, which bundles SQLite with extension "
            f"loading. Install with: {_pip_install_hint()}"
        )

    sqlite_vec = _import_sqlite_vec()
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)


def model_slug(model: str) -> str:
    """Generate a safe table/filename slug from a model name."""
    if not model:
        model = "all-MiniLM-L6-v2"
    return model.replace("/", "_").replace(":", "_").replace(" ", "_").replace("-", "_").replace(".", "_")


def _vec_table_ddl(tbl_name: str, dim: int) -> str:
    return f"""
CREATE VIRTUAL TABLE IF NOT EXISTS {tbl_name} USING vec0(
    chunk_id INTEGER PRIMARY KEY,
    embedding float[{int(dim)}]
);
"""


def _opts_tokens(opts: str) -> set[str]:
    return {part.strip().casefold() for part in opts.split(",") if part.strip()}


def _path_for_mount_match(path: str | Path) -> str:
    """Normalize *path* for prefix comparison against psutil mount points."""
    text = str(path)
    if sys.platform == "win32":
        return ntpath.normcase(ntpath.normpath(text))
    return os.path.realpath(text)


def _longest_mount_partition(path: str | Path, partitions: list[Any]) -> Any | None:
    """Pick the disk_partitions row whose mountpoint is the longest prefix of *path*."""
    target = _path_for_mount_match(path)
    best_len = -1
    best_part: Any | None = None
    for part in partitions:
        raw_mp = str(part.mountpoint or "").strip()
        if not raw_mp:
            continue
        mp = _path_for_mount_match(raw_mp)
        if target == mp:
            mp_len = len(mp)
        elif mp in ("/", "\\") and target.startswith(mp):
            mp_len = 1
        elif target.startswith(mp.rstrip("/\\") + "/") or target.startswith(mp.rstrip("/\\") + "\\"):
            mp_len = len(mp.rstrip("/\\"))
        else:
            continue
        if mp_len > best_len:
            best_len = mp_len
            best_part = part
    return best_part


def _partition_is_local(path: str | Path, part: Any) -> bool:
    """Classify a psutil disk_partitions row as local or remote."""
    fstype = str(part.fstype or "").strip()
    opts = str(part.opts or "")
    if sys.platform == "win32":
        # Mapped shares and local NTFS both report NTFS; psutil tags DRIVE_REMOTE in opts.
        return "remote" not in _opts_tokens(opts)
    if sys.platform == "darwin":
        # psutil copies MNT_LOCAL into a ``local`` mount option token.
        return "local" in _opts_tokens(opts)
    from plugin.contrib.fstype_network import fstype_is_network

    if not fstype:
        return True
    return not fstype_is_network(fstype)


def path_is_local_disk(path: str | Path) -> bool:
    """Check whether a folder resides on a local disk rather than a network mount."""
    path_text = str(path)
    if path_text.startswith(("\\\\", "//")):
        return False
    try:
        import psutil
    except ImportError:
        return True
    try:
        partitions = psutil.disk_partitions(all=True)
    except Exception:
        return True
    part = _longest_mount_partition(path, partitions)
    if part is None:
        return True
    return _partition_is_local(path, part)


def connect_corpus_db(db_path: str | Path) -> sqlite3.Connection:
    """Open corpus.db with row factory.

    The DB-API module comes from ``_dbapi`` so macOS CPython without loadable
    SQLite extensions still gets a connection that can load sqlite-vec.
    """
    dbapi = _dbapi()
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = dbapi.connect(str(path))
    conn.row_factory = dbapi.Row
    wanted = "wal" if path_is_local_disk(path.parent) else "delete"
    # WAL on a local disk; a known network filesystem uses DELETE so two
    # hosts do not share one WAL; an unrecognized type stays WAL.
    if wanted == "wal":
        res = conn.execute("PRAGMA journal_mode=WAL").fetchone()
    else:
        res = conn.execute("PRAGMA journal_mode=DELETE").fetchone()
    actual = str(res[0]).casefold() if res else ""
    if actual != wanted:
        log.warning(
            "Corpus database journal mode mismatch in %s: wanted %s, got %s",
            path.parent,
            wanted,
            actual,
        )
    return cast(sqlite3.Connection, conn)


def ensure_schema(
    conn: sqlite3.Connection,
    *,
    dim: int | None = None,
    with_fts: bool = False,
    with_vec: bool = False,
    model: str = "",
) -> None:
    """Create chunks (+ optional FTS5 / vec0) tables."""
    conn.executescript(_CHUNKS_DDL)
    if with_fts:
        conn.execute(_FTS_DDL)
    if with_vec:
        _load_vec_extension(conn)

        # Legacy migration: rename old vec_chunks table if it exists
        has_legacy_vec = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='vec_chunks'"
        ).fetchone()
        if has_legacy_vec:
            legacy_model = "all-MiniLM-L6-v2"
            legacy_slug = model_slug(legacy_model)
            legacy_tbl = f"vec_chunks_{legacy_slug}"
            try:
                conn.execute(f"ALTER TABLE vec_chunks RENAME TO {legacy_tbl}")
                log.info("Migrated legacy vec_chunks table to %s", legacy_tbl)
            except Exception:
                log.warning("Could not rename legacy vec_chunks table, dropping instead", exc_info=True)
                conn.execute("DROP TABLE IF EXISTS vec_chunks")

        active_model = model or "all-MiniLM-L6-v2"
        slug = model_slug(active_model)
        tbl_name = f"vec_chunks_{slug}"
        has_vec = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (tbl_name,),
        ).fetchone()
        if not has_vec:
            if dim is None or dim <= 0:
                raise ValueError(f"dim is required when creating vec_chunks table for model {active_model}")
            conn.execute(_vec_table_ddl(tbl_name, dim))
    conn.commit()


def rebuild_fts_corpus_index(conn: sqlite3.Connection) -> None:
    """Rebuild FTS5 passages index from external content table (chunks).

    External-content FTS can get out of sync when chunks are bulk-loaded or upgraded;
    hybrid search depends on a fresh rebuild after maintain completes.
    """
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='passages'").fetchone()
    if row is None:
        return
    conn.execute("INSERT INTO passages(passages) VALUES('rebuild')")
    conn.commit()


def corpus_chunk_count(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS c FROM chunks").fetchone()
    return int(row["c"] if row else 0)


def stored_model_dim(conn: sqlite3.Connection, model: str) -> int | None:
    """Return embedding dimension recorded for *model*, or None when unknown.

    On a cold corpus ``model_metadata`` may not exist yet. macOS often opens the
    DB via pysqlite3; its ``OperationalError`` is not a subclass of stdlib
    ``sqlite3.OperationalError``, so missing-table errors must be caught per
    connection (see ``_conn_operational_error``).
    """
    try:
        row = conn.execute(
            "SELECT dim FROM model_metadata WHERE embedding_model = ?",
            (model,),
        ).fetchone()
        if row is not None:
            return int(row["dim"])
    except _conn_operational_error(conn):
        pass
    return None


def _missing_vector_chunk_row(row: Any) -> dict[str, Any]:
    return {
        "chunk_id": int(row["chunk_id"]),
        "doc_url": str(row["doc_url"] or ""),
        "para_index": int(row["para_index"] or 0),
        "char_start": int(row["char_start"] or 0),
        "char_end": int(row["char_end"] or 0),
        "content_hash": str(row["content_hash"] or ""),
        "text": str(row["text"] or "").strip(),
        "file_mtime": float(row["file_mtime"] or 0.0),
    }


def _vec_table_if_exists(conn: sqlite3.Connection, model: str) -> str | None:
    slug = model_slug(model)
    tbl_name = f"vec_chunks_{slug}"
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (tbl_name,),
    ).fetchone()
    if not has_table:
        return None
    return tbl_name


def missing_vector_chunks(
    conn: sqlite3.Connection,
    model: str,
) -> list[dict[str, Any]]:
    """Return chunk rows that have no vector in the active model's vec_chunks table.

    When the vec table does not exist yet, every chunk row is returned (cold corpus).
    """
    tbl_name = _vec_table_if_exists(conn, model)
    if tbl_name:
        _load_vec_extension(conn)
        sql = f"""
            SELECT chunk_id, doc_url, para_index, char_start, char_end, content_hash, body AS text, file_mtime
            FROM chunks
            WHERE chunk_id NOT IN (SELECT chunk_id FROM {tbl_name})
            """
    else:
        sql = """
            SELECT chunk_id, doc_url, para_index, char_start, char_end, content_hash, body AS text, file_mtime
            FROM chunks
            """
    rows = conn.execute(sql).fetchall()
    return [_missing_vector_chunk_row(row) for row in rows]


def has_missing_vector_chunks(conn: sqlite3.Connection, model: str) -> bool:
    """True when at least one chunk row lacks a vector for *model*."""
    tbl_name = _vec_table_if_exists(conn, model)
    if tbl_name:
        _load_vec_extension(conn)
        row = conn.execute(
            f"""
            SELECT 1 FROM chunks
            WHERE chunk_id NOT IN (SELECT chunk_id FROM {tbl_name})
            LIMIT 1
            """
        ).fetchone()
    else:
        row = conn.execute("SELECT 1 FROM chunks LIMIT 1").fetchone()
    return row is not None


def _fts_index_row(conn: sqlite3.Connection, chunk_id: int) -> None:
    conn.execute("INSERT INTO passages(rowid) VALUES (?)", (int(chunk_id),))


def _delete_chunk_ids(conn: sqlite3.Connection, chunk_ids: list[int], *, with_fts: bool, with_vec: bool) -> None:
    if not chunk_ids:
        return
    chunk_id_params = [(int(cid),) for cid in chunk_ids]
    if with_fts:
        conn.executemany("INSERT INTO passages(passages, rowid) VALUES ('delete', ?)", chunk_id_params)
    if with_vec:
        _load_vec_extension(conn)
        # Find all vec_chunks_* virtual tables (excluding shadow tables)
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND sql LIKE 'CREATE VIRTUAL TABLE%vec0%' AND name LIKE 'vec_chunks_%'"
        ).fetchall()
        for row in tables:
            tbl = row["name"]
            conn.executemany(f"DELETE FROM {tbl} WHERE chunk_id = ?", chunk_id_params)
    conn.executemany("DELETE FROM chunks WHERE chunk_id = ?", chunk_id_params)


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


def delete_by_chunk_locator(
    conn: sqlite3.Connection,
    doc_url: str,
    para_index: int,
    char_start: int,
    char_end: int,
    *,
    with_fts: bool = False,
    with_vec: bool = False,
) -> int:
    """Remove one sub-chunk row by locator (any content_hash)."""
    doc_url = str(doc_url or "")
    rows = conn.execute(
        """
        SELECT chunk_id FROM chunks
        WHERE doc_url = ? AND para_index = ? AND char_start = ? AND char_end = ?
        """,
        (doc_url, int(para_index), int(char_start), int(char_end)),
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
        doc_url = str(key.get("doc_url") or "")
        para_index = int(key.get("para_index") or 0)
        if "char_start" in key and "char_end" in key:
            deleted += delete_by_chunk_locator(
                conn,
                doc_url,
                para_index,
                int(key.get("char_start") or 0),
                int(key.get("char_end") or 0),
                with_fts=with_fts,
                with_vec=with_vec,
            )
        else:
            deleted += delete_by_doc_para(
                conn,
                doc_url,
                para_index,
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
    with_fts: bool,
) -> int:
    conn.execute(
        """
        INSERT INTO chunks (
            doc_url, para_index, char_start, char_end, content_hash,
            file_mtime, body
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            doc_url,
            int(para_index),
            int(char_start),
            int(char_end),
            content_hash,
            float(file_mtime),
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

    chunk_id = chunk.get("chunk_id")
    if chunk_id is None:
        existing = conn.execute(
            """
            SELECT chunk_id FROM chunks
            WHERE doc_url = ? AND para_index = ? AND char_start = ? AND char_end = ? AND content_hash = ?
            """,
            (doc_url, para_index, char_start, char_end, content_hash),
        ).fetchone()
        if existing is not None:
            chunk_id = int(existing["chunk_id"])
        else:
            chunk_id = _insert_chunk_row(
                conn,
                doc_url=doc_url,
                para_index=para_index,
                char_start=char_start,
                char_end=char_end,
                content_hash=content_hash,
                body=body,
                file_mtime=float(chunk.get("file_mtime") or 0.0),
                with_fts=with_fts,
            )

    if with_vec and vector:
        import numpy as np

        _load_vec_extension(conn)
        emb = np.asarray(vector, dtype=np.float32)
        slug = model_slug(model)
        tbl_name = f"vec_chunks_{slug}"
        conn.execute(
            f"INSERT OR REPLACE INTO {tbl_name}(chunk_id, embedding) VALUES (?, ?)",
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
    slug = model_slug(model)
    tbl_name = f"vec_chunks_{slug}"

    # Check if table exists
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (tbl_name,),
    ).fetchone()
    if not has_table:
        return []

    count_row = conn.execute(f"SELECT COUNT(*) AS c FROM {tbl_name}").fetchone()
    count = int(count_row["c"] if count_row else 0)
    if count == 0:
        return []

    limit = min(max(int(k), 1), count)
    q = np.asarray(query_vec, dtype=np.float32)

    sql = f"""
        SELECT
            v.chunk_id,
            v.distance,
            c.doc_url,
            c.para_index,
            c.body
        FROM {tbl_name} v
        JOIN chunks c ON c.chunk_id = v.chunk_id
        WHERE v.embedding MATCH ?
          AND k = ?
    """
    params: list[Any] = [q, limit]

    if doc_url_filter:
        sql += " AND v.chunk_id IN (SELECT chunk_id FROM chunks WHERE doc_url = ?)"
        params.append(doc_url_filter)

    sql += " ORDER BY v.distance"

    rows = conn.execute(sql, tuple(params)).fetchall()

    candidates: list[dict[str, Any]] = []
    for row in rows:
        dist = float(row["distance"] or 0.0)
        score = max(0.0, 1.0 - dist)
        candidates.append(
            {
                "chunk_id": int(row["chunk_id"]),
                "doc_url": str(row["doc_url"] or ""),
                "para_index": int(row["para_index"] or 0),
                "embedding_model": model,
                "snippet": str(row["body"] or ""),
                "score": score,
                "distance": dist,
            }
        )
    return candidates


_NEAR_SLASH_RE = re.compile(r"(.+?)\s+NEAR\s*/\s*(\d+)\s+(.+)", re.IGNORECASE)
_FTS5_NEAR_CALL_RE = re.compile(r"\bNEAR\s*\(", re.IGNORECASE)
_BOOL_RE = re.compile(r"\b(AND|OR|NOT)\b", re.IGNORECASE)


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


def strip_fts_snippet_markers(snippet: str) -> str:
    """Remove FTS5 snippet() highlight brackets; readable plain text for agents and UI."""
    return str(snippet or "").replace("[", "").replace("]", "")


def fts_corpus_search(
    conn: sqlite3.Connection,
    query: str,
    *,
    k: int = 10,
    near_slop: int = 10,
    doc_url_filter: str | None = None,
) -> list[dict[str, Any]]:
    """BM25 + NEAR search on unified corpus.db passages (rowid = chunk_id)."""
    limit = max(1, min(int(k or 10), 50))
    match_expr = build_match_query(str(query or ""), near_slop=near_slop)

    sql = """
        SELECT
            p.rowid AS chunk_id,
            c.doc_url,
            c.para_index,
            snippet(passages, 0, '[', ']', '…', 32) AS snippet,
            bm25(passages) AS score
        FROM passages p
        JOIN chunks c ON c.chunk_id = p.rowid
        WHERE passages MATCH ?
    """
    params: list[Any] = [match_expr]

    if doc_url_filter:
        sql += " AND c.doc_url = ?"
        params.append(doc_url_filter)

    sql += " ORDER BY score LIMIT ?"
    params.append(limit)

    try:
        rows = conn.execute(sql, tuple(params)).fetchall()
    except _conn_operational_error(conn) as exc:
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
    model: str = "",
) -> None:
    """Attach vec0 embeddings to candidate dicts for MMR (mutates *candidates*)."""
    import numpy as np

    if not candidates:
        return
    _load_vec_extension(conn)
    slug = model_slug(model)
    tbl_name = f"vec_chunks_{slug}"

    # Check if table exists, fallback to legacy vec_chunks if it exists
    has_table = conn.execute(
        f"SELECT 1 FROM sqlite_master WHERE type='table' AND name='{tbl_name}'"
    ).fetchone()
    if not has_table:
        has_legacy = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='vec_chunks'"
        ).fetchone()
        if has_legacy:
            tbl_name = "vec_chunks"
        else:
            return

    ids = [int(c["chunk_id"]) for c in candidates if c.get("chunk_id") is not None]
    if not ids:
        return
    by_id: dict[int, Any] = {}
    # SQLite has a limit on parameters in IN clause (typically 999),
    # chunk the ids array to be safe
    chunk_size = 900
    for i in range(0, len(ids), chunk_size):
        batch_ids = ids[i : i + chunk_size]
        placeholders = ",".join("?" * len(batch_ids))
        query = f"SELECT chunk_id, embedding FROM {tbl_name} WHERE chunk_id IN ({placeholders})"
        cursor = conn.execute(query, tuple(int(x) for x in batch_ids))
        for row in cursor.fetchall():
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
    """Return stored file_mtime, last_indexed_at, and indexed chunk count."""
    doc_url = str(doc_url or "")
    row = conn.execute(
        "SELECT file_mtime, last_indexed_at FROM indexed_files WHERE doc_url = ?",
        (doc_url,),
    ).fetchone()
    chunk_row = conn.execute(
        "SELECT COUNT(*) AS c FROM chunks WHERE doc_url = ?",
        (doc_url,),
    ).fetchone()
    chunk_count = int(chunk_row["c"] if chunk_row else 0)
    if row is None:
        return {"file_mtime": 0.0, "last_indexed_at": 0.0, "chunk_count": chunk_count}
    return {
        "file_mtime": float(row["file_mtime"] or 0.0),
        "last_indexed_at": float(row["last_indexed_at"] or 0.0),
        "chunk_count": chunk_count,
    }


def file_is_stale_in_db(conn: sqlite3.Connection, doc_url: str, file_mtime: float) -> bool:
    """True when *file_mtime* is newer than the mtime stored for *doc_url*.

    Compare against the stored ``file_mtime`` captured from the file when it
    was indexed. ``last_indexed_at`` is wall-clock ``time.time()`` at write
    time, so a restored or ``touch -d`` mtime that moved forward but stayed
    older than that clock looks fresh, and a file mtime ahead of the clock
    looks stale on every pass.
    """
    info = get_file_index_info(conn, doc_url)
    if info["chunk_count"] == 0:
        return True
    return float(file_mtime) > float(info["file_mtime"])


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



def get_all_indexed_urls_in_db(conn: sqlite3.Connection) -> list[str]:
    """Return a list of all doc_url entries in indexed_files."""
    rows = conn.execute("SELECT doc_url FROM indexed_files").fetchall()
    return [str(row["doc_url"] or "") for row in rows]


def remove_file_from_index_in_db(conn: sqlite3.Connection, doc_url: str) -> None:
    """Remove a file's freshness metadata from the index."""
    doc_url = str(doc_url or "")
    conn.execute("DELETE FROM indexed_files WHERE doc_url = ?", (doc_url,))
    conn.execute("DELETE FROM indexed_paragraphs WHERE doc_url = ?", (doc_url,))
    conn.commit()


def diff_chunk_rows_in_db(
    conn: sqlite3.Connection,
    doc_url: str,
    chunks: list[Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (rows_to_index, keys_to_delete) comparing extracted chunks to corpus.db."""
    from plugin.embeddings.embeddings_fs import ParagraphChunk, chunk_to_index_row

    to_index: list[dict[str, Any]] = []
    seen: set[tuple[str, int, int, int]] = set()

    stored: dict[tuple[int, int, int], str] = {}
    doc_url = str(doc_url or "")
    if doc_url:
        rows = conn.execute(
            """
            SELECT para_index, char_start, char_end, content_hash
            FROM chunks WHERE doc_url = ?
            """,
            (doc_url,),
        ).fetchall()
        for row in rows:
            locator = (int(row["para_index"]), int(row["char_start"]), int(row["char_end"]))
            stored[locator] = str(row["content_hash"] or "")

    for chunk in chunks:
        if not isinstance(chunk, ParagraphChunk):
            continue
        locator = (chunk.para_index, chunk.char_start, chunk.char_end)
        key = (chunk.doc_url, *locator)
        seen.add(key)
        stored_hash = stored.get(locator, "")
        if stored_hash == chunk.content_hash:
            continue
        to_index.append(chunk_to_index_row(chunk))

    # Empty extracts still purge this document. Do not require chunks[0].
    to_delete: list[dict[str, Any]] = []
    if doc_url:
        for (para_index, char_start, char_end), _stored_hash in stored.items():
            if (doc_url, para_index, char_start, char_end) not in seen:
                to_delete.append(
                    {
                        "doc_url": doc_url,
                        "para_index": para_index,
                        "char_start": char_start,
                        "char_end": char_end,
                    }
                )

    return to_index, to_delete


def paragraph_body_for_locator(conn: sqlite3.Connection, doc_url: str, para_index: int) -> str:
    """Concatenate embed sub-chunks for one ODF paragraph (ordered by char_start)."""
    bodies = paragraph_bodies_for_locators(conn, {(str(doc_url or ""), int(para_index or 0))})
    return bodies.get((str(doc_url or ""), int(para_index or 0)), "")


def paragraph_bodies_for_locators(
    conn: sqlite3.Connection,
    locators: set[tuple[str, int]],
) -> dict[tuple[str, int], str]:
    """Batch-load full paragraph text for (doc_url, para_index) locators."""
    if not locators:
        return {}

    clauses: list[str] = []
    params: list[Any] = []
    for doc_url, para_index in locators:
        clauses.append("(doc_url = ? AND para_index = ?)")
        params.extend([doc_url, int(para_index)])

    rows = conn.execute(
        f"""
        SELECT doc_url, para_index, char_start, body
        FROM chunks
        WHERE {" OR ".join(clauses)}
        ORDER BY doc_url, para_index, char_start
        """,
        params,
    ).fetchall()

    grouped: dict[tuple[str, int], list[str]] = {}
    for row in rows:
        key = (str(row["doc_url"] or ""), int(row["para_index"] or 0))
        grouped.setdefault(key, []).append(str(row["body"] or "").strip())

    # Sub-chunks use CHUNK_OVERLAP at ingest; joining with spaces may repeat boundary text — acceptable v1.
    return {key: " ".join(p for p in parts if p) for key, parts in grouped.items()}


def sync_file_paragraph_state_in_db(
    conn: sqlite3.Connection,
    doc_url: str,
    chunks: list[Any],
    file_mtime: float,
    *,
    indexed_at: float,
) -> None:
    """Advance file timestamps after a successful index pass."""
    del chunks
    mark_file_indexed_in_db(
        conn,
        doc_url,
        file_mtime,
        indexed_at=indexed_at,
        paragraphs=None,
    )


__all__ = [
    "connect_corpus_db",
    "corpus_chunk_count",
    "delete_by_chunk_locator",
    "delete_by_doc_para",
    "delete_paragraph_keys",
    "diff_chunk_rows_in_db",
    "ensure_schema",
    "file_is_stale_in_db",
    "fts_corpus_search",
    "get_all_indexed_urls_in_db",
    "get_file_index_info",
    "has_missing_vector_chunks",
    "insert_paragraph_rows",
    "load_embeddings_for_candidates",
    "mark_file_indexed_in_db",
    "missing_vector_chunks",
    "paragraph_body_for_locator",
    "paragraph_bodies_for_locators",
    "rebuild_fts_corpus_index",
    "remove_file_from_index_in_db",
    "stored_model_dim",
    "sync_file_paragraph_state_in_db",
    "upsert_chunk_with_vector",
    "vec0_search",
]
