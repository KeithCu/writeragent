# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Trusted venv DuckDB SQL compute (folder read-only) — runs in user venv worker.

Phase A–C: CSV/Parquet/JSON (direct) + sibling .xlsx/.xls/.ods via host LO import
(preloaded grids) + multi-table catalog. Phase D: one in-memory DuckDB per
shared-kernel workbook session (``calc:`` / ``rps:`` / ``notebook:``) until
Reset Python Session. Isolated / chat trusted actions stay per-request.

Host always resolves scoped_dir and validates. Read-only policy (no writes/attach/export).
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
from typing import Any

log = logging.getLogger(__name__)

# Small shared result/error shapes (duplicated from analysis for zero coupling in A)
MAX_TABLE_ROWS = 200  # generous for SQL results vs analysis 50


from plugin.scripting.venv.coerce import (
    ok_result as _ok_result,
    error_result as _error_result,
    missing_package_error as _missing_package_error,
)

# Workbook-keyed sessions may keep one DuckDB. Domain prefixes used by
# run_trusted_action (``writeragent:sql``) are routing ids, not kernels —
# caching on those would leak one catalog across every document.
_PERSISTABLE_PREFIXES = ("calc:", "rps:", "notebook:")

_SESSION_CONNECTIONS: dict[str, Any] = {}
_SESSION_LOCK = threading.Lock()


def persistable_duckdb_session_id(session_id: str | None) -> str | None:
    """Return the cache key for a shared-kernel session, or ``None`` (per-request).

    ``calc:…:init`` shares the workbook key so an init script and ``=PY()``
    cells see the same catalog. Isolated executes pass no cell ``session_id``.
    """
    sid = (session_id or "").strip()
    if not sid:
        return None
    if sid.endswith(":init"):
        sid = sid[: -len(":init")]
    if any(sid.startswith(prefix) for prefix in _PERSISTABLE_PREFIXES):
        return sid
    return None


def _sandbox_session_id() -> str | None:
    try:
        from plugin.scripting.venv.venv_sandbox import current_sandbox_session_id
    except ImportError:
        return None
    return current_sandbox_session_id()


def resolve_duckdb_session_id(session_id: str | None = None) -> str | None:
    """Explicit id, else the current shared-kernel sandbox session (if persistable)."""
    if session_id is not None:
        return persistable_duckdb_session_id(session_id)
    return persistable_duckdb_session_id(_sandbox_session_id())


def _close_connection(con: Any) -> None:
    try:
        con.close()
    except Exception:
        log.debug("DuckDB session close failed", exc_info=True)


def _connection_alive(con: Any) -> bool:
    try:
        con.execute("SELECT 1")
        return True
    except Exception:
        return False


def reset_session_duckdb(session_id: str | None = None) -> None:
    """Close cached connection(s). ``None`` drops every session (tests / worker wipe).

    Reset Python Session calls this for the workbook id so registered tables
    do not survive the namespace wipe.
    """
    with _SESSION_LOCK:
        if session_id is None:
            cons = list(_SESSION_CONNECTIONS.values())
            _SESSION_CONNECTIONS.clear()
        else:
            keys = {session_id}
            normalized = persistable_duckdb_session_id(session_id)
            if normalized:
                keys.add(normalized)
            cons = [_SESSION_CONNECTIONS.pop(key, None) for key in keys]
    for con in cons:
        if con is not None:
            _close_connection(con)


def session_duckdb(session_id: str | None = None) -> Any:
    """Return a DuckDB in-memory connection for ``=PY()`` / tools.

    Shared kernel (persistable ``session_id`` or current sandbox session): the
    same connection and registered tables until Reset Python Session or
    ``invalidate_session_tables()``. Isolated / chat (no persistable session):
    a fresh connection each call — same as ``duckdb.connect()`` today.
    """
    con, _persist = _acquire_duckdb(session_id)
    return con


def invalidate_session_tables(
    names: list[str] | tuple[str, ...] | None = None,
    *,
    session_id: str | None = None,
) -> None:
    """Drop registered tables, or close the session catalog when *names* is omitted.

    Use this when a cell must discard a snapshot without Reset Python Session.
    """
    key = resolve_duckdb_session_id(session_id)
    if key is None:
        return
    if not names:
        reset_session_duckdb(key)
        return
    with _SESSION_LOCK:
        con = _SESSION_CONNECTIONS.get(key)
    if con is None:
        return
    for raw in names:
        name = str(raw).strip()
        if not name:
            continue
        try:
            if hasattr(con, "unregister"):
                con.unregister(name)
            else:
                con.execute(f'DROP VIEW IF EXISTS "{name}"')
        except Exception:
            log.debug("invalidate_session_tables: could not drop %s", name, exc_info=True)


def _acquire_duckdb(session_id: str | None) -> tuple[Any, bool]:
    """Return ``(connection, persist)``. Persist means do not close after the query."""
    import duckdb  # type: ignore[import-not-found]

    key = resolve_duckdb_session_id(session_id)
    if key is None:
        # Isolated / writeragent:sql trusted action: per-request catalog.
        return duckdb.connect(), False
    with _SESSION_LOCK:
        con = _SESSION_CONNECTIONS.get(key)
        if con is not None and _connection_alive(con):
            return con, True
        if con is not None:
            _close_connection(con)
        con = duckdb.connect()
        _SESSION_CONNECTIONS[key] = con
        return con, True


def _register_relation(con: Any, name: str, rel: Any) -> None:
    """Replace a prior registration so a recalc snapshot overwrites a stale table."""
    try:
        if hasattr(con, "unregister"):
            con.unregister(name)
    except Exception:
        pass
    con.register(name, rel)


@contextlib.contextmanager
def _scoped_cwd(path: str):
    """Temporarily chdir so relative filenames in user SQL resolve safely under scoped_dir."""
    old = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(old)


def _looks_like_write_or_escape(sql: str) -> bool:
    # Crude substring scan (tokens in strings/CTEs/comments can false-positive).
    # Real threats it covers are path escape (``..``, absolute paths, COPY TO),
    # which tests/scripting/test_duckdb_sql.py already pins. Writes to registered
    # tables never hit disk. Do not replace this with ``duckdb.connect(read_only=True)``
    # — see the connect site. Engine-side alternative: ``allowed_directories=[scoped_dir]``
    # plus ``lock_configuration`` at connect, then re-prove those folder tests.
    s = " " + sql.upper() + " "
    write_tokens = (
        " COPY ",
        " ATTACH ",
        " INSTALL ",
        " LOAD ",
        " EXPORT ",
        " CREATE OR REPLACE ",
        " INSERT ",
        " UPDATE ",
        " DELETE ",
        " DROP ",
        " ALTER ",
    )
    if any(tok in s for tok in write_tokens):
        return True
    # crude but effective for A: reject obvious escapes even under chdir
    if ".." in sql or sql.strip().startswith(("/", "\\")) or ":\\" in sql or "~/" in sql:
        return True
    return False


def _validate_files(scoped_dir: str, files: list[str] | None) -> list[str]:
    """Return list of validated absolute paths for the given basenames. Reject escapes."""
    if not scoped_dir or not os.path.isdir(scoped_dir):
        raise ValueError("scoped_dir must be an existing directory")
    base = os.path.realpath(os.path.abspath(scoped_dir))
    validated: list[str] = []
    for raw in files or []:
        bn = os.path.basename(str(raw).strip())
        if not bn or bn in (".", "..") or "/" in bn or "\\" in bn:
            continue
        candidate = os.path.join(base, bn)
        if not os.path.isfile(candidate):
            continue
        rp = os.path.realpath(candidate)
        # must be strictly under base (or equal for weird case)
        if rp == base or rp.startswith(base + os.sep):
            validated.append(rp)
        else:
            log.warning("rejected path outside scoped_dir: %s", candidate)
    return validated


def _register_preloaded(con: Any, preloaded: dict[str, Any] | None) -> None:
    if not preloaded:
        return
    from plugin.scripting.venv.coerce import coerce_to_dataframe

    for orig_name, data in preloaded.items():
        if not orig_name or not data:
            continue
        try:
            if isinstance(data, dict) and "grid" in data:
                g = data["grid"]
                h = bool(data.get("headers", True))
                coerced = coerce_to_dataframe(g, headers=h, sheet_hint=orig_name)
            else:
                coerced = coerce_to_dataframe(data, headers=True, sheet_hint=orig_name)
            _register_relation(con, orig_name, coerced.df)
            stem = os.path.splitext(orig_name)[0]
            if stem and stem != orig_name:
                try:
                    _register_relation(con, stem, coerced.df)
                except Exception:
                    pass
        except Exception as reg_err:
            log.warning("Failed to register preloaded table %s: %s", orig_name, reg_err)


def _register_flat_files(con: Any, flat_files: dict[str, str] | None) -> None:
    if not flat_files:
        return
    for name, path in flat_files.items():
        if not name or not path:
            continue
        try:
            p = str(path)
            lower = p.lower()
            if lower.endswith((".csv", ".tsv")):
                rel = con.read_csv(p)
            elif lower.endswith(".parquet"):
                rel = con.read_parquet(p)
            elif lower.endswith((".json", ".jsonl")):
                rel = con.read_json(p)
            else:
                rel = con.read_csv(p)
            _register_relation(con, name, rel)
        except Exception as flat_err:
            log.warning("Failed to register flat file table %s from %s: %s", name, path, flat_err)


def query_folder_sql(
    scoped_dir: str | None,
    sql: str,
    files: list[str] | dict[str, str] | None = None,
    preloaded: dict[str, Any] | None = None,
    flat_files: dict[str, str] | None = None,
    *,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Run read-only SQL against scoped folder files + preloaded tables (from sibling spreadsheets or live ranges).

    - preloaded: dict table_name -> 2D grid data (from host LO reads for ranges/office files).
    - files: list of basenames (legacy, uses chdir + filename refs) or dict name->basename for flat files.
    - flat_files: dict name -> full validated path for direct DuckDB reads (preferred for named files in Phase C+).
    - session_id: shared-kernel workbook id. When persistable (or the current
      ``=PY()`` sandbox session is), reuse one connection and keep tables
      that this call does not re-register. Isolated / omitted: per-request.
    'data' is conventional for sheet ranges.
    """
    helper = "query_folder_sql"
    try:
        import duckdb  # type: ignore[import-not-found]
    except ImportError:
        return _missing_package_error(helper, "duckdb")
    del duckdb

    if not sql or not str(sql).strip():
        return _error_result("INVALID_SQL", "sql is required", helper=helper)

    if _looks_like_write_or_escape(str(sql)):
        return _error_result("READONLY_VIOLATION", "SQL contains write, attach, or path escape", helper=helper)

    if not scoped_dir and (files or flat_files):
        return _error_result("MISSING_SCOPED_DIR", "scoped_dir is required for file-based queries (resolved on host)", helper=helper)

    try:
        base = os.path.realpath(os.path.abspath(scoped_dir)) if scoped_dir else None

        # Handle legacy files list or new dict for flat files
        legacy_files = None
        if isinstance(files, list):
            legacy_files = files
            validated = _validate_files(scoped_dir, files) if scoped_dir else []
        elif isinstance(files, dict):
            # files as {name: basename}, validate later or assume host did
            validated = []
            flat_from_files = {k: os.path.join(base, os.path.basename(v)) if base else v for k,v in files.items()}
            flat_files = {** (flat_files or {}), **flat_from_files}
        else:
            validated = _validate_files(scoped_dir, files) if files and scoped_dir else []

        # In-memory catalog. DuckDB refuses ``read_only=True`` on ``:memory:``.
        # Shared kernel reuses one connection; isolated always opens+closes.
        con, persist = _acquire_duckdb(session_id)
        try:
            _register_preloaded(con, preloaded)
            _register_flat_files(con, flat_files)

            if base and (legacy_files or not flat_files) and (validated or legacy_files):
                with _scoped_cwd(base):
                    df = con.execute(sql).df()
            else:
                df = con.execute(sql).df()
        finally:
            if not persist:
                _close_connection(con)

        total = int(len(df))
        limited = df.head(MAX_TABLE_ROWS)
        rows = limited.where(limited.notna(), None).values.tolist()
        cols = [str(c) for c in limited.columns]

        used = [os.path.basename(p) for p in validated]
        if preloaded:
            used = list(preloaded.keys()) + used
        if flat_files:
            used = list(flat_files.keys()) + used
        return _ok_result(
            helper,
            columns=cols,
            rows=rows,
            truncated=total > MAX_TABLE_ROWS,
            total_rows=total,
            files_used=used,
        )
    except Exception as exc:  # broad: duckdb errors, IO, etc. surface message
        log.exception("query_folder_sql failed")
        return _error_result("DUCKDB_ERROR", str(exc), helper=helper)
