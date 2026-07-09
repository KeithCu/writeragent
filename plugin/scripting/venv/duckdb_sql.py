# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Trusted venv DuckDB SQL compute (folder read-only) — runs in user venv worker.

Phase A: CSV/Parquet/JSON (direct) + Phase A+: sibling .xlsx/.xls/.ods via host LO import (preloaded grids).
Host always resolves scoped_dir and validates. Read-only policy (no writes/attach/export).
"""

from __future__ import annotations

import contextlib
import logging
import os
from typing import Any

log = logging.getLogger(__name__)

# Small shared result/error shapes (duplicated from analysis for zero coupling in A)
MAX_TABLE_ROWS = 200  # generous for SQL results vs analysis 50


from plugin.scripting.venv.coerce import (
    ok_result as _ok_result,
    error_result as _error_result,
    missing_package_error as _missing_package_error,
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
    files: list[str] | dict[str, str] | None = None,
    preloaded: dict[str, Any] | None = None,
    flat_files: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Run read-only SQL against scoped folder files + preloaded tables (from sibling spreadsheets or live ranges).

    - preloaded: dict table_name -> 2D grid data (from host LO reads for ranges/office files).
    - files: list of basenames (legacy, uses chdir + filename refs) or dict name->basename for flat files.
    - flat_files: dict name -> full validated path for direct DuckDB reads (preferred for named files in Phase C+).
    'data' is conventional for sheet ranges.
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

        con = duckdb.connect()
        try:
            # Register any preloaded tables (e.g. from sibling .xlsx/.ods or live ranges)
            if preloaded:
                from plugin.scripting.venv.coerce import coerce_to_dataframe
                for orig_name, data in (preloaded or {}).items():
                    if not orig_name or not data:
                        continue
                    try:
                        if isinstance(data, dict) and "grid" in data:
                            g = data["grid"]
                            h = bool(data.get("headers", True))
                            coerced = coerce_to_dataframe(g, headers=h, sheet_hint=orig_name)
                        else:
                            coerced = coerce_to_dataframe(data, headers=True, sheet_hint=orig_name)
                        con.register(orig_name, coerced.df)
                        stem = os.path.splitext(orig_name)[0]
                        if stem and stem != orig_name:
                            try:
                                con.register(stem, coerced.df)
                            except Exception:
                                pass
                    except Exception as reg_err:
                        log.warning("Failed to register preloaded table %s: %s", orig_name, reg_err)

            # Phase C+: named flat files (preferred)
            if flat_files:
                for name, path in (flat_files or {}).items():
                    if not name or not path:
                        continue
                    try:
                        p = str(path)
                        lower = p.lower()
                        if lower.endswith(('.csv', '.tsv')):
                            rel = con.read_csv(p)
                            con.register(name, rel)
                        elif lower.endswith('.parquet'):
                            rel = con.read_parquet(p)
                            con.register(name, rel)
                        elif lower.endswith(('.json', '.jsonl')):
                            rel = con.read_json(p)
                            con.register(name, rel)
                        else:
                            rel = con.read_csv(p)
                            con.register(name, rel)
                    except Exception as flat_err:
                        log.warning("Failed to register flat file table %s from %s: %s", name, path, flat_err)

            if legacy_files or not flat_files:
                # Legacy path: chdir + filename refs in SQL (for old calls)
                if base and (validated or legacy_files):
                    with _scoped_cwd(base):
                        df = con.execute(sql).df()
                else:
                    df = con.execute(sql).df()
            else:
                # Modern path with named tables only
                df = con.execute(sql).df()

            if 'df' not in locals():
                df = con.execute(sql).df()
        finally:
            con.close()

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
