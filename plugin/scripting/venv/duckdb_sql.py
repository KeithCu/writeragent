# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Trusted venv DuckDB SQL compute (folder read-only) — runs in user venv worker.

Phase A–C: CSV/Parquet/JSON (direct) + sibling .xlsx/.xls/.ods via host LO import
(preloaded grids) + multi-table catalog. Phase D: one in-memory DuckDB per
shared-kernel workbook session (``calc:`` / ``rps:`` / ``notebook:``) until
Reset Python Session. Isolated / chat trusted actions stay per-request.

Host always resolves scoped_dir and validates. Read-only policy (no disk/network
writes/attach/export). Result rows are capped at ``MAX_TABLE_ROWS``. Callers
must treat ``truncated`` / ``warning`` / ``flags`` as user-visible — a short
``rows`` list is not the full set.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import threading
from typing import Any

from plugin.scripting.venv.coerce import (
    ok_result as _ok_result,
    error_result as _error_result,
    missing_package_error as _missing_package_error,
    table_from_df as _table_from_df,
)

log = logging.getLogger(__name__)

# Small shared result/error shapes (duplicated from analysis for zero coupling in A)
MAX_TABLE_ROWS = 200  # generous for SQL results vs analysis 50

# Direct DuckDB binders (venv). Sibling .xlsx/.xls/.ods stay on the host LO import path.
FLAT_CSV_EXTS = (".csv", ".tsv")
FLAT_PARQUET_EXTS = (".parquet",)
FLAT_JSON_EXTS = (".json", ".jsonl", ".ndjson")
FLAT_FILE_EXTS = FLAT_CSV_EXTS + FLAT_PARQUET_EXTS + FLAT_JSON_EXTS
_OFFICE_HINT_EXTS = (".xlsx", ".xls", ".ods")


class FlatFileError(ValueError):
    """Typed folder-file failure so query_folder_sql can return a stable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _flat_ext(path: str) -> str:
    return os.path.splitext(path)[1].lower()


def unsupported_flat_type_message(filename: str, ext: str) -> str:
    shown = ext or "(no extension)"
    direct = ", ".join(FLAT_FILE_EXTS)
    office = ", ".join(_OFFICE_HINT_EXTS)
    return (
        f"Unsupported folder file type {shown} for {filename!r}. "
        f"Direct DuckDB reads: {direct}. "
        f"Spreadsheets ({office}) use the LibreOffice import path."
    )


def _assert_under_scoped_dir(scoped_dir: str, path: str) -> str:
    base = os.path.realpath(os.path.abspath(scoped_dir))
    rp = os.path.realpath(os.path.abspath(path))
    if rp == base or not rp.startswith(base + os.sep):
        raise FlatFileError(
            "READONLY_VIOLATION",
            f"path outside scoped_dir: {os.path.basename(path)}",
        )
    return rp


def resolve_flat_file_path(scoped_dir: str, spec: str) -> str:
    """Resolve a caller file spec to a real path under *scoped_dir*.

    Only the basename is used (host/LLM cannot pass ``../`` escapes). Missing
    and unsupported types fail loud — do not skip and let SQL look like a
    missing ``FROM``.
    """
    if not scoped_dir or not os.path.isdir(scoped_dir):
        raise FlatFileError("MISSING_SCOPED_DIR", "scoped_dir must be an existing directory")
    raw = str(spec).strip()
    if not raw:
        raise FlatFileError("MISSING_FILE", "file spec is empty")
    normalized = raw.replace("\\", "/")
    if any(part == ".." for part in normalized.split("/")):
        raise FlatFileError("READONLY_VIOLATION", f"file spec escapes scoped_dir: {raw}")
    bn = os.path.basename(raw)
    if not bn or bn in (".", ".."):
        raise FlatFileError("READONLY_VIOLATION", f"invalid file spec {raw!r}")
    ext = _flat_ext(bn)
    if ext not in FLAT_FILE_EXTS:
        raise FlatFileError("UNSUPPORTED_FILE_TYPE", unsupported_flat_type_message(bn, ext))
    candidate = os.path.join(os.path.realpath(os.path.abspath(scoped_dir)), bn)
    if not os.path.isfile(candidate):
        raise FlatFileError(
            "MISSING_FILE",
            f"Folder file {bn!r} was not found under the document folder",
        )
    return _assert_under_scoped_dir(scoped_dir, candidate)


def _read_flat_relation(con: Any, path: str) -> Any:
    """Bind a scoped flat file with the DuckDB reader that matches its suffix.

    Unknown suffixes used to fall through to ``read_csv``, so Parquet/JSON
    mis-reads looked like CSV parse noise (or a later missing table).
    """
    ext = _flat_ext(path)
    if ext in FLAT_CSV_EXTS:
        return con.read_csv(path)
    if ext in FLAT_PARQUET_EXTS:
        return con.read_parquet(path)
    if ext in FLAT_JSON_EXTS:
        # jsonl/ndjson are newline-delimited; .json is auto (array or ndjson).
        if ext in (".jsonl", ".ndjson"):
            return con.read_json(path, format="newline_delimited")
        return con.read_json(path)
    raise FlatFileError("UNSUPPORTED_FILE_TYPE", unsupported_flat_type_message(os.path.basename(path), ext))


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
    same guarded connection and registered tables until Reset Python Session or
    ``invalidate_session_tables()``. Isolated / chat (no persistable session):
    a fresh connection each call. ``execute`` / ``sql`` use the read-only
    firewall; raw ``import duckdb`` still bypasses that wrap.
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
    raw = con._con if isinstance(con, GuardedDuckDBConnection) else con
    for item in names:
        name = str(item).strip()
        if not name:
            continue
        try:
            if hasattr(raw, "unregister"):
                raw.unregister(name)
            else:
                raw.execute(f'DROP VIEW IF EXISTS "{name}"')
        except Exception:
            log.debug("invalidate_session_tables: could not drop %s", name, exc_info=True)


def _acquire_duckdb(session_id: str | None) -> tuple[Any, bool]:
    """Return ``(connection, persist)``. Persist means do not close after the query.

    Cached connections are ``GuardedDuckDBConnection`` wrappers so
    ``session_duckdb() is session_duckdb()`` stays true for a persistable id
    and ``execute`` / ``sql`` still hit the firewall.
    """
    import duckdb  # type: ignore[import-not-found]

    key = resolve_duckdb_session_id(session_id)
    if key is None:
        # Isolated / writeragent:sql trusted action: per-request catalog.
        return GuardedDuckDBConnection(duckdb.connect()), False
    with _SESSION_LOCK:
        con = _SESSION_CONNECTIONS.get(key)
        if con is not None and _connection_alive(con):
            return con, True
        if con is not None:
            _close_connection(con)
        con = GuardedDuckDBConnection(duckdb.connect())
        _SESSION_CONNECTIONS[key] = con
        return con, True


def _register_relation(con: Any, name: str, rel: Any) -> None:
    """Replace a prior registration so a recalc snapshot overwrites a stale table."""
    raw = con._con if isinstance(con, GuardedDuckDBConnection) else con
    try:
        if hasattr(raw, "unregister"):
            raw.unregister(name)
    except Exception:
        pass
    raw.register(name, rel)


# Disk/network side effects only. In-memory CREATE VIEW / TABLE / INSERT stay
# allowed so shared-kernel ``session_duckdb()`` register workflows work.
# Do not use ``duckdb.connect(read_only=True)`` on ``:memory:`` — the engine
# refuses that. Word-boundary scan after comment/string strip (tokens inside
# comments or string literals are not statements).
_BLOCKED_STMT_RE = re.compile(
    r"(?is)(?<![A-Z0-9_])(?:COPY|EXPORT|ATTACH|INSTALL|LOAD)\b"
)

_STRING_RE = re.compile(r"(?s)('(?:''|[^'])*'|\"(?:\"\"|[^\"]*)\")")
_LINE_COMMENT_RE = re.compile(r"--[^\n]*")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_DRIVE_RE = re.compile(r"^[A-Za-z]:[/\\]")
_URI_RE = re.compile(r"(?i)^(?:https?|s3|file|ftp)://")
# Unquoted remainder: ../, drive, URI, or absolute /path (not ``a / b`` division).
_REMAINDER_PATH_RE = re.compile(
    r"""(?ix)
    (?:
        \.\.[/\\]
        | ~/
        | [A-Za-z]:[/\\]
        | (?:https?|s3|file|ftp)://
        | (?:^|[\s=,(])[/\\](?!\s)
    )
    """
)


class ReadonlyViolation(ValueError):
    """COPY/escape blocked the same way as ``query_folder_sql`` (``READONLY_VIOLATION``)."""

    code = "READONLY_VIOLATION"


class GuardedDuckDBConnection:
    """Delegate to an in-memory DuckDB connection; ``execute`` / ``sql`` use the firewall.

    Register / CREATE VIEW stay available. Raw ``import duckdb`` still bypasses this
    wrap — demos and ``=PY()`` should prefer ``session_duckdb()`` / ``run_sql``.
    """

    def __init__(self, con: Any) -> None:
        self._con = con

    def execute(self, sql: str, *args: Any, **kwargs: Any) -> Any:
        _raise_if_write_or_escape(sql)
        return self._con.execute(sql, *args, **kwargs)

    def sql(self, sql: str, *args: Any, **kwargs: Any) -> Any:
        _raise_if_write_or_escape(sql)
        return self._con.sql(sql, *args, **kwargs)

    def close(self) -> None:
        self._con.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._con, name)


def _strip_sql_comments_and_strings(sql: str) -> tuple[str, list[str]]:
    """Return (sql without comments/strings, inner string literals).

    Statements are judged on the remainder; path escapes live in the strings.
    """
    strings: list[str] = []

    def _keep_string(match: re.Match[str]) -> str:
        raw = match.group(0)
        strings.append(raw[1:-1])
        return " "

    no_block = _BLOCK_COMMENT_RE.sub(" ", sql)
    no_line = _LINE_COMMENT_RE.sub(" ", no_block)
    remainder = _STRING_RE.sub(_keep_string, no_line)
    return remainder, strings


def _string_looks_like_escape(literal: str) -> bool:
    text = literal.strip()
    if not text:
        return False
    if text.startswith(("/", "\\", "~/")) or text.startswith("~\\"):
        return True
    if ".." in text and ("/" in text or "\\" in text):
        return True
    if _DRIVE_RE.match(text) or _URI_RE.match(text):
        return True
    return False


def _file_spec_looks_like_escape(spec: str) -> bool:
    """True when a files= entry is not a scoped basename (``../``, slashes, ``~``)."""
    text = str(spec).strip()
    if not text:
        return False
    base = os.path.basename(text)
    return base != text or base in (".", "..") or ".." in text


def _looks_like_write_or_escape(sql: str) -> bool:
    # Comment/string strip avoids false positives (``SELECT 'COPY later'``) while
    # still catching ``FROM '/etc/passwd'`` and ``COPY … TO`` as statements.
    # Do not treat ``ROUND(a / b)`` as a path — only ``/name`` without a space.
    remainder, strings = _strip_sql_comments_and_strings(str(sql))
    if _BLOCKED_STMT_RE.search(remainder):
        return True
    if _REMAINDER_PATH_RE.search(remainder):
        return True
    return any(_string_looks_like_escape(item) for item in strings)


def _raise_if_write_or_escape(sql: str) -> None:
    if _looks_like_write_or_escape(sql):
        raise ReadonlyViolation("SQL contains write, attach, or path escape")


def _truncation_warning(total: int) -> str:
    return (
        f"Result truncated: showing {MAX_TABLE_ROWS} of {total} rows "
        f"(MAX_TABLE_ROWS={MAX_TABLE_ROWS}). This is not the full result — "
        f"add LIMIT or aggregate to see the complete set."
    )


def _ok_sql_result(
    helper: str,
    df: Any,
    *,
    files_used: list[str] | None = None,
) -> dict[str, Any]:
    """Shape a SQL DataFrame like analysis helpers: tables + flags + metrics."""
    total = int(len(df))
    truncated = total > MAX_TABLE_ROWS
    table = _table_from_df(df, name="sql_result", max_rows=MAX_TABLE_ROWS)
    # table_from_df already sets truncated / total_rows; keep columns/rows at
    # top level so existing chat/tool callers keep working.
    cols = list(table["columns"])
    rows = list(table["rows"])
    warning = _truncation_warning(total) if truncated else None
    flags = [warning] if warning else []
    payload: dict[str, Any] = {
        "columns": cols,
        "rows": rows,
        "truncated": truncated,
        "total_rows": total,
        "row_cap": MAX_TABLE_ROWS,
        "files_used": files_used or [],
        "tables": [table],
        "flags": flags,
        "metrics": {
            "returned_rows": len(rows),
            "total_rows": total,
            "row_cap": MAX_TABLE_ROWS,
            "truncated": truncated,
        },
    }
    if warning:
        payload["warning"] = warning
        # ``message`` is a priority key in generic RPS HTML insert — bold, not a footnote.
        payload["message"] = warning
    return _ok_result(helper, **payload)


@contextlib.contextmanager
def _scoped_cwd(path: str):
    """Temporarily chdir so relative filenames in user SQL resolve safely under scoped_dir."""
    old = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(old)


def _validate_files(scoped_dir: str, files: list[str] | None) -> list[str]:
    """Return validated absolute paths for the given specs. Fail loud on gaps."""
    validated: list[str] = []
    for raw in files or []:
        spec = str(raw).strip()
        if not spec:
            continue
        validated.append(resolve_flat_file_path(scoped_dir, spec))
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


def _register_flat_files(
    con: Any,
    flat_files: dict[str, str] | None,
    scoped_dir: str | None = None,
) -> None:
    if not flat_files:
        return
    raw = con._con if isinstance(con, GuardedDuckDBConnection) else con
    for name, path in flat_files.items():
        if not name or not path:
            continue
        p = str(path)
        if scoped_dir:
            # Host sends a full path; still require the file to live under scoped_dir.
            if os.path.isfile(p):
                ext = _flat_ext(p)
                if ext not in FLAT_FILE_EXTS:
                    raise FlatFileError(
                        "UNSUPPORTED_FILE_TYPE",
                        unsupported_flat_type_message(os.path.basename(p), ext),
                    )
                p = _assert_under_scoped_dir(scoped_dir, p)
            else:
                p = resolve_flat_file_path(scoped_dir, p)
        elif not os.path.isfile(p):
            raise FlatFileError(
                "MISSING_FILE",
                f"Folder file {os.path.basename(p)!r} (table {name!r}) was not found",
            )
        else:
            ext = _flat_ext(p)
            if ext not in FLAT_FILE_EXTS:
                raise FlatFileError(
                    "UNSUPPORTED_FILE_TYPE",
                    unsupported_flat_type_message(os.path.basename(p), ext),
                )
        try:
            # Unwrap GuardedDuckDBConnection so read_* hits the engine, not execute().
            rel = _read_flat_relation(raw, p)
        except FlatFileError:
            raise
        except Exception as flat_err:
            # Used to log-and-skip: SQL then failed with a missing table instead of
            # the real Parquet/JSON read error.
            raise FlatFileError(
                "FLAT_FILE_READ_ERROR",
                f"Could not read {os.path.basename(p)!r} as table {name!r}: {flat_err}",
            ) from flat_err
        _register_relation(con, name, rel)


def run_sql(sql: str, con: Any | None = None, *, session_id: str | None = None) -> dict[str, Any]:
    """Guarded SQL execute with the same honesty fields as ``query_folder_sql``.

    Prefer this from ``=PY()`` / shared-kernel cells instead of raw
    ``import duckdb``. With no ``con``, uses ``_acquire_duckdb`` so a persistable
    sandbox session reuses the Phase D catalog. Isolated / omitted session:
    one-shot connection.
    """
    helper = "run_sql"
    try:
        import duckdb  # type: ignore[import-not-found]
    except ImportError:
        return _missing_package_error(helper, "duckdb")
    del duckdb

    if not sql or not str(sql).strip():
        return _error_result("INVALID_SQL", "sql is required", helper=helper)

    if _looks_like_write_or_escape(str(sql)):
        return _error_result("READONLY_VIOLATION", "SQL contains write, attach, or path escape", helper=helper)

    persist = False
    acquired: Any = None
    try:
        if con is None:
            acquired, persist = _acquire_duckdb(session_id)
            df = acquired.execute(sql).df()
        elif isinstance(con, GuardedDuckDBConnection):
            df = con.execute(sql).df()
        else:
            df = con.execute(sql).df()
        return _ok_sql_result(helper, df)
    except ReadonlyViolation as exc:
        return _error_result("READONLY_VIOLATION", str(exc), helper=helper)
    except Exception as exc:
        log.exception("run_sql failed")
        return _error_result("DUCKDB_ERROR", str(exc), helper=helper)
    finally:
        if acquired is not None and not persist:
            _close_connection(acquired)


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

    Results longer than ``MAX_TABLE_ROWS`` set ``truncated=True`` and a visible
    ``warning`` / ``flags`` / ``message`` — do not treat ``rows`` as complete.
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

    file_specs: list[str] = []
    if isinstance(files, list):
        file_specs = [str(x) for x in files]
    elif isinstance(files, dict):
        file_specs = [str(v) for v in files.values()]
    if any(_file_spec_looks_like_escape(spec) for spec in file_specs):
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
            validated = []
            if not scoped_dir:
                raise FlatFileError("MISSING_SCOPED_DIR", "scoped_dir is required for file-based queries")
            flat_from_files = {k: resolve_flat_file_path(scoped_dir, v) for k, v in files.items() if k and str(v).strip()}
            flat_files = {**(flat_files or {}), **flat_from_files}
        else:
            validated = _validate_files(scoped_dir, files) if files and scoped_dir else []

        # In-memory catalog. DuckDB refuses ``read_only=True`` on ``:memory:``.
        # Shared kernel reuses one connection; isolated always opens+closes.
        con, persist = _acquire_duckdb(session_id)
        try:
            _register_preloaded(con, preloaded)
            _register_flat_files(con, flat_files, scoped_dir=base)

            if base and (legacy_files or not flat_files) and (validated or legacy_files):
                with _scoped_cwd(base):
                    df = con.execute(sql).df()
            else:
                df = con.execute(sql).df()
        finally:
            if not persist:
                _close_connection(con)

        used = [os.path.basename(p) for p in validated]
        if preloaded:
            used = list(preloaded.keys()) + used
        if flat_files:
            used = list(flat_files.keys()) + used
        return _ok_sql_result(helper, df, files_used=used)
    except FlatFileError as exc:
        return _error_result(exc.code, str(exc), helper=helper)
    except Exception as exc:  # broad: duckdb errors, IO, etc. surface message
        log.exception("query_folder_sql failed")
        return _error_result("DUCKDB_ERROR", str(exc), helper=helper)
