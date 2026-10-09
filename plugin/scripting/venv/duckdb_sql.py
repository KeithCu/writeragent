# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Trusted venv DuckDB SQL compute (folder read-only) — runs in user venv worker.

CSV/Parquet/JSON (direct) + sibling .xlsx/.xls/.ods via host LO import
(preloaded grids) + multi-table catalog. One in-memory DuckDB per
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
from typing import Any, Iterator

from plugin.scripting.venv.coerce import (
    ok_result as _ok_result,
    error_result as _error_result,
    table_from_df as _table_from_df,
)

log = logging.getLogger(__name__)

MAX_TABLE_ROWS = 200

# Direct DuckDB binders (venv). Sibling .xlsx/.xls/.ods stay on the host LO import path.
FLAT_CSV_EXTS = (".csv", ".tsv")
FLAT_PARQUET_EXTS = (".parquet",)
FLAT_JSON_EXTS = (".json", ".jsonl", ".ndjson")
FLAT_FILE_EXTS = FLAT_CSV_EXTS + FLAT_PARQUET_EXTS + FLAT_JSON_EXTS
_OFFICE_HINT_EXTS = (".xlsx", ".xls", ".ods")

_READONLY_VIOLATION_MESSAGE = "SQL contains write, attach, or path escape"


# One exception type for a flat-file failure and a read-only violation.
# The code stays on the instance, and callers catch RuntimeError.
class SqlError(RuntimeError):
    """Typed SQL failure so query_folder_sql / run_sql can return a stable code."""

    code: str

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _require_duckdb(helper: str = "duckdb_sql") -> Any:
    """Import duckdb or raise SqlError('MISSING_PACKAGE', ...)."""
    try:
        import duckdb  # type: ignore[import-not-found]

        return duckdb
    except ImportError as exc:
        raise SqlError("MISSING_PACKAGE", f"duckdb is required for {helper}.") from exc


def _duckdb_version_tuple() -> tuple[int, ...]:
    try:
        import duckdb  # type: ignore[import-not-found]

        ver = str(getattr(duckdb, "__version__", ""))
        return tuple(int(p) for p in ver.split(".")[:2] if p.isdigit())
    except Exception:
        return (0, 0)


def _is_duckdb_error(exc: BaseException) -> bool:
    try:
        import duckdb  # type: ignore[import-not-found]

        return isinstance(exc, duckdb.Error)
    except Exception:
        return False


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
        raise SqlError(
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
        raise SqlError("MISSING_SCOPED_DIR", "scoped_dir must be an existing directory")
    raw = str(spec).strip()
    if not raw:
        raise SqlError("MISSING_FILE", "file spec is empty")
    normalized = raw.replace("\\", "/")
    if any(part == ".." for part in normalized.split("/")):
        raise SqlError("READONLY_VIOLATION", f"file spec escapes scoped_dir: {raw}")
    bn = os.path.basename(raw)
    if not bn or bn in (".", ".."):
        raise SqlError("READONLY_VIOLATION", f"invalid file spec {raw!r}")
    ext = _flat_ext(bn)
    if ext not in FLAT_FILE_EXTS:
        raise SqlError("UNSUPPORTED_FILE_TYPE", unsupported_flat_type_message(bn, ext))
    candidate = os.path.join(os.path.realpath(os.path.abspath(scoped_dir)), bn)
    if not os.path.isfile(candidate):
        raise SqlError(
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
    raise SqlError("UNSUPPORTED_FILE_TYPE", unsupported_flat_type_message(os.path.basename(path), ext))


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


def _cell_sandbox_active() -> bool:
    try:
        from plugin.scripting.venv.venv_sandbox import sandbox_execute_active
    except ImportError:
        return False
    return sandbox_execute_active()


def resolve_duckdb_session_id(session_id: str | None = None) -> str | None:
    """Explicit id, else the current shared-kernel sandbox session (if persistable).

    Host callers (trusted SQL, tests) may name a workbook. A sandboxed cell
    must not: ``session_duckdb("calc:other")`` used to open that catalog.
    """
    current = persistable_duckdb_session_id(_sandbox_session_id())
    if session_id is None:
        return current
    requested = persistable_duckdb_session_id(session_id)
    # A foreign session id, or one that would skip the workbook catalog, is
    # ignored inside run_sandboxed_code. The cell session is used.
    if _cell_sandbox_active() and requested != current:
        return current
    return requested


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
    raw = _raw_duckdb(con)
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
    duckdb = _require_duckdb()

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
    raw = _raw_duckdb(con)
    try:
        if hasattr(raw, "unregister"):
            raw.unregister(name)
    except Exception:
        pass
    raw.register(name, rel)


# Disk/network side effects only. In-memory CREATE VIEW / TABLE / INSERT stay
# allowed so shared-kernel ``session_duckdb()`` register workflows work.
# Match only at a statement boundary ((?:^|;)\s*). A bare identifier
# anywhere would flag 'SELECT load, copy FROM t'. SET, PRAGMA, CALL,
# CREATE SECRET, and DETACH are statements too.
_BLOCKED_STMT_RE = re.compile(
    r"""(?isx)
    (?:^|;)\s*(?:\(\s*)*
    (?:
        COPY\b
        | EXPORT\b
        | ATTACH\b
        | DETACH\b
        | INSTALL\b
        | LOAD\b
        | SET\b
        | PRAGMA\b
        | CALL\b
        | CREATE\s+(?:OR\s+REPLACE\s+)?(?:(?:TEMPORARY|PERSISTENT)\s+)?SECRET\b
    )
    """
)

_DRIVE_RE = re.compile(r"^[A-Za-z]:[/\\]")
_URI_RE = re.compile(r"(?i)^(?:https?|s3|file|ftp)://")

# Division is not a path. 'price /100' must not match. Unquoted '/' is
# left out; '..', '~/', a drive letter, and a URI are escapes.
_REMAINDER_PATH_RE = re.compile(
    r"""(?ix)
    (?:
        \.\.[/\\]
        | ~/
        | [A-Za-z]:[/\\]
        | (?:https?|s3|file|ftp)://
    )
    """
)

# One left-to-right pass for strings, dollar quotes ($tag$...$tag$), and
# comments. Stripping comments first lets '--' or '/*' inside a string
# swallow the rest ("SELECT '--'; COPY t TO 'x'").
_TOKEN_RE = re.compile(
    r"""(?isx)
    (?P<line_comment> --[^\n]* )
    | (?P<block_comment> /\* .*? \*/ )
    | (?P<single_quote> ' (?: '' | [^'] )* ' )
    | (?P<double_quote> " (?: "" | [^"] )* " )
    | (?P<dollar_quote> \$ (?P<tag> [A-Za-z0-9_]* ) \$ .*? \$ (?P=tag) \$ )
    """
)


# Methods a script may call on session_duckdb() besides execute/sql/close.
# __getattr__ used to forward every connection method, and ``_con`` was public,
# so read_csv / ``con._con.execute`` skipped the read-only firewall.
_GUARDED_FORWARDED = frozenset({"register", "unregister", "df"})


class GuardedDuckDBConnection:
    """Delegate to an in-memory DuckDB connection; ``execute`` / ``sql`` use the firewall.

    Register / CREATE VIEW stay available. Raw ``import duckdb`` is allowed and
    unguarded; ``session_duckdb()`` returns this wrapper as an advisory guard.
    """

    def __init__(self, con: Any) -> None:
        self.__con = con

    def execute(self, sql: str, *args: Any, **kwargs: Any) -> Any:
        _raise_if_write_or_escape(sql)
        return self.__con.execute(sql, *args, **kwargs)

    def sql(self, sql: str, *args: Any, **kwargs: Any) -> Any:
        _raise_if_write_or_escape(sql)
        return self.__con.sql(sql, *args, **kwargs)

    def close(self) -> None:
        self.__con.close()

    def __getattr__(self, name: str) -> Any:
        if name not in _GUARDED_FORWARDED:
            raise AttributeError(f"{type(self).__name__!r} has no attribute {name!r}")
        return getattr(self.__con, name)


def _raw_duckdb(con: Any) -> Any:
    """The connection inside a guard. Module code only — not ``session_duckdb()._con``."""
    if isinstance(con, GuardedDuckDBConnection):
        return con._GuardedDuckDBConnection__con
    return con


def _strip_sql_comments_and_strings(sql: str) -> tuple[str, list[str]]:
    """Return (sql without comments/strings, inner string literals).

    Statements are judged on the remainder; path escapes live in the strings.
    Single-pass tokenization prevents string contents from being misinterpreted
    as comments or vice-versa.
    """
    strings: list[str] = []
    chunks: list[str] = []
    pos = 0
    for m in _TOKEN_RE.finditer(sql):
        chunks.append(sql[pos : m.start()])
        pos = m.end()
        if m.group("single_quote"):
            raw = m.group("single_quote")
            strings.append(raw[1:-1].replace("''", "'"))
            chunks.append(" ")
        elif m.group("double_quote"):
            raw = m.group("double_quote")
            strings.append(raw[1:-1].replace('""', '"'))
            chunks.append(" ")
        elif m.group("dollar_quote"):
            tag = m.group("tag")
            prefix_len = len(tag) + 2
            strings.append(m.group("dollar_quote")[prefix_len:-prefix_len])
            chunks.append(" ")
        else:
            # line or block comment replaced by space
            chunks.append(" ")
    chunks.append(sql[pos:])
    remainder = "".join(chunks)
    return remainder, strings


# A lone '/' or '\' is a delimiter, not an escape. startswith("/") would
# reject split_part(x, '/', 1) and replace(p, '/', '_').
def _string_looks_like_escape(literal: str) -> bool:
    text = literal.strip()
    if not text or text in ("/", "\\"):
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
    remainder, strings = _strip_sql_comments_and_strings(str(sql))
    if _BLOCKED_STMT_RE.search(remainder):
        return True
    if _REMAINDER_PATH_RE.search(remainder):
        return True
    return any(_string_looks_like_escape(item) for item in strings)


def _raise_if_write_or_escape(sql: str) -> None:
    if _looks_like_write_or_escape(sql):
        raise SqlError("READONLY_VIOLATION", _READONLY_VIOLATION_MESSAGE)


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
def _scoped_cwd(path: str) -> Iterator[None]:
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

    registered_stems: set[str] = set()
    for orig_name, data in preloaded.items():
        # CalcRange refuses ``bool()`` (ambiguous truth value). Empty list/dict
        # is still a skip; a live =PY() range must register.
        if not orig_name or data is None:
            continue
        if isinstance(data, (list, tuple, dict)) and not data:
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
                # Skip a stem that is already registered (a.csv vs a.xlsx) and
                # log the alias error. Overwriting the earlier table would
                # query the wrong file.
                if stem in preloaded or stem in registered_stems:
                    log.warning(
                        "Skipping stem alias %r for %r: name collision",
                        stem,
                        orig_name,
                    )
                else:
                    try:
                        _register_relation(con, stem, coerced.df)
                        registered_stems.add(stem)
                    except Exception as alias_err:
                        log.warning(
                            "Failed to register stem alias %r for %r: %s",
                            stem,
                            orig_name,
                            alias_err,
                        )
        except Exception as reg_err:
            log.warning("Failed to register preloaded table %s: %s", orig_name, reg_err)
            raise


def _register_flat_files(
    con: Any,
    flat_files: dict[str, str] | None,
    scoped_dir: str | None = None,
) -> None:
    if not flat_files:
        return
    # Files require scoped_dir. Every path goes through resolve_flat_file_path.
    if not scoped_dir:
        raise SqlError("MISSING_SCOPED_DIR", "scoped_dir is required for flat files")
    raw = _raw_duckdb(con)
    for name, path in flat_files.items():
        if not name or not path:
            continue
        p = resolve_flat_file_path(scoped_dir, str(path))
        try:
            rel = _read_flat_relation(raw, p)
        except SqlError:
            raise
        except Exception as flat_err:
            raise SqlError(
                "FLAT_FILE_READ_ERROR",
                f"Could not read {os.path.basename(p)!r} as table {name!r}: {flat_err}",
            ) from flat_err

        # Copy flat-file relations into memory before external access is
        # turned off, or a later query on them fails.
        escaped_name = name.replace('"', '""')
        raw.register("_wa_tmp_import", rel)
        try:
            raw.execute(f'DROP TABLE IF EXISTS "{escaped_name}"')
            raw.execute(f'CREATE TABLE "{escaped_name}" AS SELECT * FROM _wa_tmp_import')
        finally:
            try:
                raw.unregister("_wa_tmp_import")
            except Exception:
                pass

        stem = os.path.splitext(name)[0]
        if stem and stem != name and stem not in flat_files:
            escaped_stem = stem.replace('"', '""')
            try:
                raw.execute(
                    f'CREATE VIEW IF NOT EXISTS "{escaped_stem}" AS SELECT * FROM "{escaped_name}"'
                )
            except Exception as alias_err:
                log.warning("Failed to create view for stem %r: %s", stem, alias_err)


# Shared by query_folder_sql and run_sql. run_sql needs the full frame,
# not a MAX_TABLE_ROWS slice, and the original SqlError code.
def _execute(
    scoped_dir: str | None,
    sql: str,
    files: list[str] | dict[str, str] | None = None,
    preloaded: dict[str, Any] | None = None,
    flat_files: dict[str, str] | None = None,
    *,
    session_id: str | None = None,
) -> tuple[Any, list[str]]:
    """Execute SQL against preloaded tables and scoped folder files, returning (df, used_files)."""
    _require_duckdb("query_folder_sql")

    if not sql or not str(sql).strip():
        raise SqlError("INVALID_SQL", "sql is required")

    _raise_if_write_or_escape(str(sql))

    # Normalize once: str -> [str], tuple -> list. A bare string would be
    # walked character by character, and a tuple would skip the escape check.
    normalized_files: list[str] | dict[str, str] | None = None
    if isinstance(files, str):
        normalized_files = [files]
    elif isinstance(files, tuple):
        normalized_files = list(files)
    else:
        normalized_files = files

    file_specs: list[str] = []
    if isinstance(normalized_files, list):
        file_specs = [str(x) for x in normalized_files]
    elif isinstance(normalized_files, dict):
        file_specs = [str(v) for v in normalized_files.values()]

    if any(_file_spec_looks_like_escape(spec) for spec in file_specs):
        raise SqlError("READONLY_VIOLATION", _READONLY_VIOLATION_MESSAGE)

    if not scoped_dir and (normalized_files or flat_files):
        raise SqlError(
            "MISSING_SCOPED_DIR",
            "scoped_dir is required for file-based queries (resolved on host)",
        )

    base = os.path.realpath(os.path.abspath(scoped_dir)) if scoped_dir else None

    legacy_files: list[str] | None = None
    validated: list[str] = []
    resolved_flat = dict(flat_files) if flat_files else {}

    if isinstance(normalized_files, list):
        legacy_files = normalized_files
        validated = _validate_files(scoped_dir, normalized_files) if scoped_dir else []
        for p in validated:
            bn = os.path.basename(p)
            if bn not in resolved_flat:
                resolved_flat[bn] = p
            stem = os.path.splitext(bn)[0]
            if stem and stem not in resolved_flat:
                resolved_flat[stem] = p
    elif isinstance(normalized_files, dict):
        if not scoped_dir:
            raise SqlError("MISSING_SCOPED_DIR", "scoped_dir is required for file-based queries")
        for k, v in normalized_files.items():
            if k and str(v).strip():
                resolved_flat[k] = resolve_flat_file_path(scoped_dir, v)

    con, persist = _acquire_duckdb(session_id)
    raw = _raw_duckdb(con)
    try:
        _register_preloaded(con, preloaded)
        if resolved_flat and base:
            _register_flat_files(con, resolved_flat, scoped_dir=base)

        # Lock configuration and turn off external access on a connection
        # that is not kept. The tables are already in memory.
        if not persist:
            raw.execute("SET enable_external_access=false")
            raw.execute("SET lock_configuration=true")
        elif base and _duckdb_version_tuple() >= (1, 3):
            escaped_base = base.replace("'", "''")
            raw.execute(f"SET allowed_directories=['{escaped_base}']")

        if base and legacy_files:
            with _scoped_cwd(base):
                df = con.execute(sql).df()
        else:
            df = con.execute(sql).df()
    except SqlError:
        raise
    except Exception as exc:
        # A DuckDB error is a failed query. Anything else keeps the traceback.
        if _is_duckdb_error(exc):
            log.warning("SQL execution failed: %s", exc)
        else:
            log.exception("SQL execution failed unexpectedly")
        raise SqlError("DUCKDB_ERROR", str(exc)) from exc
    finally:
        if not persist:
            _close_connection(con)

    used = [os.path.basename(p) for p in validated]
    if preloaded:
        used = list(preloaded.keys()) + used
    if resolved_flat:
        used = list(resolved_flat.keys()) + used

    return df, used


def run_sql(
    sql: str,
    con: Any | None = None,
    files: list[str] | dict[str, str] | None = None,
    scoped_dir: str | None = None,
    *,
    session_id: str | None = None,
    preloaded: dict[str, Any] | None = None,
) -> Any:
    """Guarded SQL for ``=PY()`` — honesty dict, or a DataFrame for folder joins.

    Prefer this from ``=PY()`` / shared-kernel cells instead of raw
    ``import duckdb``.

    * Execute: ``run_sql(sql)`` or ``run_sql(sql, con)`` returns the same
      honesty dict as ``query_folder_sql``. No ``con`` uses ``_acquire_duckdb``
      so a persistable sandbox session reuses the catalog.
    * Folder join: a mapping as the second arg (or ``preloaded=`` / ``files`` /
      ``scoped_dir``) runs ``_execute`` and returns the full DataFrame for
      spill without the 200 row cap.
    """
    helper = "run_sql"
    folder_preloaded = preloaded
    if isinstance(con, dict):
        folder_preloaded = con
        con = None

    if folder_preloaded is not None or files is not None or scoped_dir is not None:
        # _execute returns the full frame. The row cap would truncate a folder
        # join, and wrapping SqlError would drop its code.
        df, _used = _execute(
            scoped_dir,
            sql,
            files=files,
            preloaded=folder_preloaded,
            session_id=session_id,
        )
        return df

    _require_duckdb(helper)

    if not sql or not str(sql).strip():
        return _error_result("INVALID_SQL", "sql is required", helper=helper)

    persist = False
    acquired: Any = None
    try:
        if con is None:
            acquired, persist = _acquire_duckdb(session_id)
            target_con = acquired
            if not persist:
                raw = _raw_duckdb(acquired)
                raw.execute("SET enable_external_access=false")
                raw.execute("SET lock_configuration=true")
        else:
            target_con = con

        # GuardedDuckDBConnection.execute already scans for writes. Scan here
        # only when the connection is not guarded.
        if not isinstance(target_con, GuardedDuckDBConnection):
            _raise_if_write_or_escape(sql)
        df = target_con.execute(sql).df()
        return _ok_sql_result(helper, df)
    except SqlError as exc:
        return _error_result(exc.code, str(exc), helper=helper)
    except Exception as exc:
        # A DuckDB error is a failed query. Anything else keeps the traceback.
        if _is_duckdb_error(exc):
            log.warning("run_sql failed: %s", exc)
        else:
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
    - flat_files: dict name -> full validated path for direct DuckDB reads.
    - session_id: shared-kernel workbook id. When persistable (or the current
      ``=PY()`` sandbox session is), reuse one connection and keep tables
      that this call does not re-register. Isolated / omitted: per-request.
    'data' is conventional for sheet ranges.

    Results longer than ``MAX_TABLE_ROWS`` set ``truncated=True`` and a visible
    ``warning`` / ``flags`` / ``message`` — do not treat ``rows`` as complete.
    """
    helper = "query_folder_sql"
    try:
        df, used = _execute(
            scoped_dir,
            sql,
            files=files,
            preloaded=preloaded,
            flat_files=flat_files,
            session_id=session_id,
        )
        return _ok_sql_result(helper, df, files_used=used)
    except SqlError as exc:
        return _error_result(exc.code, str(exc), helper=helper)
    except Exception as exc:
        # A DuckDB error is a failed query. Anything else keeps the traceback.
        if _is_duckdb_error(exc):
            log.warning("query_folder_sql failed: %s", exc)
        else:
            log.exception("query_folder_sql failed")
        return _error_result("DUCKDB_ERROR", str(exc), helper=helper)
