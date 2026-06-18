# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Trusted venv DuckDB SQL compute (folder read-only) — runs in user venv worker.

Phase A: CSV/Parquet/JSON under a host-supplied scoped_dir only.
Never accepts raw paths from LLM. Read-only policy (no writes/attach/export).
"""

from __future__ import annotations

import contextlib
import logging
import os
from typing import Any

log = logging.getLogger(__name__)

# Small shared result/error shapes (duplicated from analysis for zero coupling in A)
MAX_TABLE_ROWS = 200  # generous for SQL results vs analysis 50


def _ok_result(helper: str, **payload: Any) -> dict[str, Any]:
    return {"status": "ok", "helper": helper, **payload}


def _error_result(code: str, message: str, *, helper: str | None = None, details: dict[str, Any] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"status": "error", "code": code, "message": message}
    if helper:
        out["helper"] = helper
    if details:
        out["details"] = details
    return out


def _missing_package_error(helper: str, package: str) -> dict[str, Any]:
    return _error_result(
        "MISSING_PACKAGE",
        f"{package} is required for {helper}.",
        helper=helper,
    )


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


def query_folder_sql(
    scoped_dir: str | None,
    sql: str,
    files: list[str] | None = None,
) -> dict[str, Any]:
    """Run read-only SQL against files in a single scoped folder (CSV/Parquet/JSON).

    - scoped_dir: host-resolved absolute dir (required).
    - sql: user/LLM SQL; only basenames from *files* may be referenced (relative ok after chdir).
    - files: optional allow-list of basenames; if omitted all regular files? No — require explicit for safety.
    """
    helper = "query_folder_sql"
    try:
        import duckdb  # type: ignore[import-not-found]
    except ImportError:
        return _missing_package_error(helper, "duckdb")

    if not sql or not str(sql).strip():
        return _error_result("INVALID_SQL", "sql is required", helper=helper)

    if _looks_like_write_or_escape(str(sql)):
        return _error_result("READONLY_VIOLATION", "SQL contains write, attach, or path escape", helper=helper)

    if not scoped_dir:
        return _error_result("MISSING_SCOPED_DIR", "scoped_dir is required (resolved on host)", helper=helper)

    try:
        validated = _validate_files(scoped_dir, files)
        if not validated:
            return _error_result("NO_ALLOWED_FILES", "No valid files under scoped_dir (pass explicit 'files' list of basenames)", helper=helper)

        base = os.path.realpath(os.path.abspath(scoped_dir))
        con = duckdb.connect()
        try:
            with _scoped_cwd(base):
                # Execute inside the folder so 'filename.csv' in FROM works
                df = con.execute(sql).df()
        finally:
            con.close()

        total = int(len(df))
        limited = df.head(MAX_TABLE_ROWS)
        rows = limited.where(limited.notna(), None).values.tolist()
        cols = [str(c) for c in limited.columns]

        return _ok_result(
            helper,
            columns=cols,
            rows=rows,
            truncated=total > MAX_TABLE_ROWS,
            total_rows=total,
            files_used=[os.path.basename(p) for p in validated],
        )
    except Exception as exc:  # broad: duckdb errors, IO, etc. surface message
        log.exception("query_folder_sql failed")
        return _error_result("DUCKDB_ERROR", str(exc), helper=helper)
