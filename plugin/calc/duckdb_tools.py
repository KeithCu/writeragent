# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Calc DuckDB SQL tools (folder queries via trusted venv helper)."""

from __future__ import annotations

import logging
import os
import uuid
from typing import TYPE_CHECKING, Any, cast

from plugin.calc.address_utils import format_range_address, qualify_a1, split_sheet_prefix
from plugin.calc.base import ToolCalcPythonSqlBase
from plugin.calc.calc_addin_data import check_python_data_size
from plugin.doc.document_research import get_document_directory, resolve_listing_directory, open_document_for_read, close_document_research_document
from plugin.framework.errors import CalcError, ToolExecutionError, UnoObjectError, is_disposed_exception, uno_probe
from plugin.framework.queue_executor import execute_on_main_thread
from plugin.scripting.config_limits import configured_python_max_data_cells
from plugin.scripting.venv.duckdb_sql import (
    FLAT_FILE_EXTS,
    MAX_TABLE_ROWS,
    unsupported_flat_type_message,
)

if TYPE_CHECKING:
    from pathlib import Path

    from plugin.calc.ods_cache import OdsCacheEntry
    from plugin.framework.tool import ToolContext

log = logging.getLogger("writeragent.calc.duckdb")

from plugin.calc.ods_cache import EXCEL_EXTENSIONS

OFFICE_EXTS = EXCEL_EXTENSIONS + (".ods",)

class QueryFolderSqlTool(ToolCalcPythonSqlBase):
    """Run read-only SQL (DuckDB) over folder files and/or live Calc table sources.

    ``tables`` catalog entries store a *stable identity* (sheet name, named /
    database range, or frozen A1) — not expanded used-range bounds. Host
    resolves bounds at read time so a later insert/append does not require
    rewriting the catalog. Sibling ``files={"sales": "budget.xlsx#Sales"}``
    is the same sheet used-range identity; the dict key is the SQL table name.
    """

    name: str | None = "query_folder_sql"
    is_mutation: bool | None = False
    description: str = (
        "Run read-only SQL (via DuckDB) against folder files and/or live Calc ranges (Phase C multi-table). "
        'Prefer stable table identity: tables={name: {sheet: "Sales_Analytics"}} (sheet used range) '
        'or {named_range: "SalesData"} (Calc named/database range). '
        'Absolute range: {range: "Sales.A1:F500"} stays frozen A1. '
        'Sibling sheet used-range: files={name: "budget.xlsx#Sales"} (dict key is the SQL table). '
        "Flat files: CSV/TSV, Parquet, JSON/JSONL/NDJSON (DuckDB read_*). "
        "Spreadsheets (.xlsx/.xls/.ods) use the LibreOffice import path. "
        'Optional tables file="budget.xlsx" reads that sibling instead of the active doc. '
        "Host prepares all UNO data + validates. "
        f"Results cap at {MAX_TABLE_ROWS} rows (MAX_TABLE_ROWS): truncated=true plus warning/flags/message "
        "when the result is incomplete. COPY/EXPORT/ATTACH/INSTALL/LOAD and path escapes fail with READONLY_VIOLATION."
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "sql": {"type": "string", "description": (f"Read-only SQL (SELECT/CTE/in-memory VIEW). COPY/EXPORT/ATTACH/INSTALL/LOAD and path escapes are rejected. Results longer than {MAX_TABLE_ROWS} rows are truncated and flagged.")},
            "files": {
                "type": "object",
                "additionalProperties": {"type": "string"},
                "description": (
                    'Folder files as name -> basename/spec. Flat: {"ledger": "ledger.parquet"}, {"events": "events.json"}. Sibling spreadsheet used-range: {"sales": "budget.xlsx#Sales"} (#SheetName is the sheet identity; the dict key is the SQL table). A list of basenames is still accepted.'
                ),
            },
            "data_range": {"type": "string", "description": "Frozen A1 on the active sheet (e.g. 'Sheet1.A1:F500'). Becomes table 'data'. Prefer tables={data: {sheet}} or {named_range} for stable identity."},
            "headers": {"type": "boolean", "description": "First row of data_range (or preloaded) contains column headers (default true)."},
            "tables": {
                "type": "object",
                "additionalProperties": {
                    "type": "object",
                    "properties": {
                        "sheet": {"type": "string", "description": "Sheet name: register that sheet's used range (resolved at read time)."},
                        "named_range": {"type": "string", "description": "Calc named range or database range; current referred bounds at read time. Resolves active sheet, then workbook, then other visible sheets; Sheet.Name forces that sheet; database range is still accepted."},
                        "range": {"type": "string", "description": "Frozen absolute A1 (e.g. 'Sales.A1:F500'). Does not grow with inserts."},
                        "file": {"type": "string", "description": "Sibling workbook basename, optionally #SheetName (budget.xlsx#Sales)."},
                        "headers": {"type": "boolean"},
                    },
                },
                "description": ('Multi-table catalog. Exactly one identity per entry: sheet, named_range, or range. e.g. {"sales": {"sheet": "Sales_Analytics"}, "costs": {"named_range": "CostData"}}. Mix with files.'),
            },
            "task_hint": {"type": "string", "description": "Optional hint for logging/context."},
        },
        "required": ["sql"],
    }
    long_running: bool = True

    def is_async(self) -> bool:
        return True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        sql = kwargs["sql"]
        assert isinstance(sql, str)
        sql = sql.strip()
        if not sql:
            return self._tool_error("sql is required")

        files_raw = kwargs.get("files") or []
        files: list[str] | dict[str, str]
        if isinstance(files_raw, (list, tuple)):
            files = [str(x) for x in files_raw if str(x).strip()]
        elif isinstance(files_raw, dict):
            files = {str(k): str(v) for k, v in files_raw.items() if str(v).strip()}
        else:
            files = []

        data_range = str(kwargs.get("data_range") or "").strip() or None
        headers = bool(kwargs.get("headers", True))

        tables_raw = kwargs.get("tables") or {}
        tables = dict(tables_raw) if isinstance(tables_raw, dict) else {}
        if data_range:
            if "data" in tables:
                return self._tool_error("data_range and tables['data'] both define table 'data'; pass only one", code="DUCKDB_SQL_ERROR")
            tables["data"] = {"range": data_range, "headers": headers}

        task_hint = str(kwargs.get("task_hint") or "") or None

        from plugin.framework.thread_guard import on_main_thread
        from plugin.scripting.client import run_folder_sql

        def _load_sql_inputs() -> dict[str, Any]:
            # UNO and folder preload only. run_folder_sql stays on the caller.
            # Prefer listing dir (handles untitled -> Work) then fall back
            scoped = resolve_listing_directory(ctx.ctx, ctx.doc) or get_document_directory(ctx.doc)

            preloaded: dict[str, Any] = {}
            flat_files: dict[str, str] = {}

            # Catalog stores identity (sheet / named_range / frozen A1), not
            # expanded used-range. Bounds are resolved at read time below.
            for tbl_name, spec in tables.items():
                if not tbl_name:
                    continue
                try:
                    parsed = parse_table_source_spec(spec, default_headers=headers)
                    grid = read_table_source_grid(ctx.ctx, ctx.doc, scoped, parsed)
                    preloaded[str(tbl_name)] = {"grid": grid, "headers": bool(parsed["headers"])}
                except CalcError as exc:
                    return self._tool_error(str(exc), code="DUCKDB_SQL_ERROR")
                except ToolExecutionError as exc:
                    return self._tool_error(f"Failed to read table '{tbl_name}': {exc}", code=getattr(exc, "code", "DUCKDB_SQL_ERROR"))

            # Separate direct DuckDB-readable files from office files that need LO import.
            # Support "file.xlsx" or "file.xlsx#SheetName" syntax for sheets.
            # files can be list (legacy) or dict for named (Phase C)
            files_input = files
            file_pairs = files_input.items() if isinstance(files_input, dict) else [(None, f) for f in (files_input or [])]

            for name_hint, fspec in file_pairs:
                spec = str(fspec).strip() if fspec else ""
                if not spec:
                    continue
                try:
                    file_spec = parse_table_source_spec({"file": spec}, default_headers=headers)
                except ToolExecutionError as exc:
                    return self._tool_error(str(exc), code=getattr(exc, "code", "DUCKDB_SQL_ERROR"))
                bn = str(file_spec.get("file") or "")

                ext = os.path.splitext(bn)[1].lower()
                if not scoped:
                    return self._tool_error("A saved document folder is required to read sibling files", code="MISSING_SCOPED_DIR")
                table_name = name_hint or bn
                if table_name in preloaded or table_name in flat_files:
                    return self._tool_error(f"Duplicate SQL table name {table_name!r} for {spec!r}; give each tables/files source a distinct name", code="DUCKDB_SQL_ERROR")
                full_path = os.path.join(scoped, bn)
                if not os.path.isfile(full_path):
                    # Used to append the basename to direct_files so DuckDB failed
                    # later with a generic IO / missing-FROM error.
                    return self._tool_error(f"Folder file {bn!r} was not found under the document folder", code="MISSING_FILE")
                if ext in OFFICE_EXTS:
                    # Office files that exist must preload or fail loud. A silent
                    # (None, None) skip used to append the basename to direct_files
                    # so SQL ran without the table and looked like a missing FROM.
                    try:
                        office_grid = _read_sibling_office_file_as_grid(ctx.ctx, full_path, file_spec)
                    except ToolExecutionError as exc:
                        return self._tool_error(str(exc), code=getattr(exc, "code", "DUCKDB_SQL_ERROR"))
                    preloaded[table_name] = {"grid": office_grid, "headers": headers}
                    continue
                if ext in FLAT_FILE_EXTS:
                    flat_files[table_name] = full_path
                    continue
                return self._tool_error(unsupported_flat_type_message(bn, ext), code="UNSUPPORTED_FILE_TYPE")

            # Enforce the same data size limit used for analysis / =PY()
            max_cells = configured_python_max_data_cells()
            for name, entry in preloaded.items():
                g = entry["grid"]
                if g:
                    size_err = check_python_data_size(g, max_cells=max_cells)
                    if size_err:
                        return self._tool_error(f"Preloaded table {name} too large for DuckDB SQL: {size_err}")

            return {"scoped": scoped, "preloaded": preloaded or None, "flat_files": flat_files or None}

        # Hop only the UNO preload when the caller is a worker; the IPC
        # stays on this thread. One function that both reads the sheets and
        # calls run_folder_sql pushes the SQL and the venv IPC onto the UI
        # thread via execute_on_main_thread and freezes Calc. Callers that
        # are already on main (tests, sync entry) run the preload inline so
        # they do not deadlock the UI thread.
        if on_main_thread():
            prepared = _load_sql_inputs()
        else:
            # execute_on_main_thread is typed Any; _load_sql_inputs returns this dict.
            prepared = cast("dict[str, Any]", execute_on_main_thread(_load_sql_inputs))

        if prepared.get("status") == "error":
            return prepared

        # Pass flat_files for named direct flat files (Phase C), preloaded for grids (ranges + office)
        result = run_folder_sql(ctx.ctx, prepared.get("scoped"), sql, None, preloaded=prepared.get("preloaded"), flat_files=prepared.get("flat_files"))

        if isinstance(result, dict):
            if task_hint:
                result = dict(result)
                result.setdefault("task_hint", task_hint)
            return result
        return {"status": "ok", "result": result}


def _grid_has_usable_values(grid: list[list[Any]] | None) -> bool:
    """True when *grid* has at least one non-empty cell (0 / False count as data)."""
    if not grid:
        return False
    for row in grid:
        if not row:
            continue
        for cell in row:
            if cell is not None and cell != "":
                return True
    return False


def parse_table_source_spec(spec: Any, *, default_headers: bool = True) -> dict[str, Any]:
    """Normalize a ``tables`` catalog entry to a source identity.

    The catalog stores identity — sheet name, named/database range, or frozen
    A1 — not the expanded used-range. Read-time helpers resolve current bounds
    so callers can re-query after rows/columns are inserted without rewriting
    A1 in the catalog.

    Exactly one of ``sheet``, ``named_range``, or ``range`` is required, except
    a sibling ``file`` alone (or ``file.xlsx#Sheet``) which means that sheet's
    used range.
    """
    if isinstance(spec, str):
        spec = {"range": spec}
    if not isinstance(spec, dict):
        raise ToolExecutionError(f"Table spec must be a string range or an object with sheet, named_range, or range; got {type(spec).__name__}", code="DUCKDB_SQL_ERROR")

    headers = bool(spec.get("headers", default_headers))
    file_spec = str(spec.get("file") or "").strip() or None
    sheet = str(spec.get("sheet") or "").strip() or None
    named_range = str(spec.get("named_range") or "").strip() or None
    range_a1 = str(spec.get("range") or "").strip() or None

    # Sibling shorthand: budget.xlsx#Sales is sheet used-range identity.
    if file_spec and "#" in file_spec:
        file_part, sheet_part = file_spec.rsplit("#", 1)
        file_spec = os.path.basename(file_part.strip()) or None
        hash_sheet = sheet_part.strip() or None
        if hash_sheet:
            if sheet and sheet != hash_sheet:
                raise ToolExecutionError(f"Table spec file {file_spec!r}# sheet {hash_sheet!r} disagrees with sheet {sheet!r}", code="DUCKDB_SQL_ERROR")
            sheet = hash_sheet
    elif file_spec:
        file_spec = os.path.basename(file_spec)

    present: list[str] = [k for k, v in (("sheet", sheet), ("named_range", named_range), ("range", range_a1)) if v]

    if len(present) > 1:
        raise ToolExecutionError(f"Table spec must use exactly one of sheet, named_range, or range (got {', '.join(present)})", code="DUCKDB_SQL_ERROR")
    if not present:
        if file_spec:
            kind = "sheet"
        else:
            raise ToolExecutionError("Table spec needs sheet (used range), named_range, or range (absolute A1)", code="DUCKDB_SQL_ERROR")
    else:
        kind = present[0]

    return {"kind": kind, "sheet": sheet, "named_range": named_range, "range": range_a1, "file": file_spec, "headers": headers}


def _container_element_names(container: Any) -> list[str]:
    if container is None or not hasattr(container, "getElementNames"):
        return []
    return uno_probe(lambda: [str(n) for n in container.getElementNames()], [])


def _available_named_sources(doc: Any) -> str:
    named = _container_element_names(getattr(doc, "NamedRanges", None))
    db_ranges = _container_element_names(getattr(doc, "DatabaseRanges", None))
    parts: list[str] = []
    if named:
        parts.append(", ".join(named))
    if db_ranges:
        parts.append(", ".join(f"{n} (database)" for n in db_ranges))
    return "; ".join(parts)


def _lookup_database_range_object(doc: Any, name: str) -> Any | None:
    dbrs = getattr(doc, "DatabaseRanges", None)
    if dbrs is None or not hasattr(dbrs, "hasByName"):
        return None
    bare = split_sheet_prefix(name)[1]
    for candidate in (name, bare):
        if candidate and dbrs.hasByName(candidate):
            return dbrs.getByName(candidate)
    return None


def _range_address_to_qualified_a1(doc: Any, addr: Any) -> str:
    sheet_idx = int(getattr(addr, "Sheet", 0))
    sheet_name = str(doc.getSheets().getByIndex(sheet_idx).getName())
    return qualify_a1(sheet_name, format_range_address(addr, collapse_single=True))


def used_range_qualified_a1(sheet: Any) -> str:
    """Sheet used-range as sheet-qualified A1 (resolved now, not stored)."""
    from plugin.calc.spreadsheet_import.ingest import _used_range_address

    addr = _used_range_address(sheet)
    return qualify_a1(str(sheet.getName()), format_range_address(addr, collapse_single=True))


def _named_object_to_qualified_a1(doc: Any, obj: Any, *, label: str) -> str:
    addr = None
    if hasattr(obj, "getReferredCells"):
        cells = uno_probe(obj.getReferredCells, None)
        if cells is not None and hasattr(cells, "getRangeAddress"):
            addr = cells.getRangeAddress()
    if addr is None and hasattr(obj, "getDataArea"):
        addr = uno_probe(obj.getDataArea, None)
    if addr is None:
        raise ToolExecutionError(f"{label} does not refer to a cell range", code="DUCKDB_SQL_ERROR")
    return _range_address_to_qualified_a1(doc, addr)


def resolve_table_source_a1(model: Any, parsed: dict[str, Any]) -> str:
    """Resolve a parsed identity to sheet-qualified A1 at read time.

    Sheet → current used range. named_range → current NamedRanges /
    DatabaseRanges bounds. range → the frozen A1 the caller stored.
    """
    from plugin.calc.bridge import CalcBridge, find_named_range

    kind = parsed.get("kind")
    if kind == "sheet":
        bridge = CalcBridge(model)
        sheet_name = parsed.get("sheet")
        if sheet_name:
            try:
                target = bridge.get_sheet(str(sheet_name))
            except UnoObjectError as exc:
                raise ToolExecutionError(str(exc), code="DUCKDB_SQL_ERROR") from exc
        else:
            sheets = model.getSheets()
            if sheets.getCount() < 1:
                raise ToolExecutionError("workbook has no sheets", code="DUCKDB_SQL_ERROR")
            target = sheets.getByIndex(0)
        return used_range_qualified_a1(target)

    if kind == "named_range":
        name = str(parsed.get("named_range") or "")
        hit = find_named_range(model, name)
        if hit is not None:
            return _named_object_to_qualified_a1(model, hit.named_range, label=f"Named range {name!r}")
        dbr = _lookup_database_range_object(model, name)
        if dbr is not None:
            return _named_object_to_qualified_a1(model, dbr, label=f"Database range {name!r}")
        available = _available_named_sources(model)
        extra = f" Available: {available}" if available else ""
        raise ToolExecutionError(f"No named range or database range named {name!r}.{extra}", code="DUCKDB_SQL_ERROR")

    if kind == "range":
        return str(parsed.get("range") or "")

    raise ToolExecutionError(f"Unknown table identity {kind!r}", code="DUCKDB_SQL_ERROR")


def read_model_table_grid(model: Any, parsed: dict[str, Any]) -> list[list[Any]]:
    """Read values for a parsed identity from an already-open Calc model."""
    from plugin.calc.bridge import CalcBridge
    from plugin.calc.calc_addin_data import values_from_inspector_range
    from plugin.calc.inspector import CellInspector

    qualified = resolve_table_source_a1(model, parsed)
    inspector = CellInspector(CalcBridge(model))
    raw = inspector.read_range(qualified)
    return values_from_inspector_range(raw)


def read_table_source_grid(ctx: Any, active_doc: Any, scoped_dir: str | None, parsed: dict[str, Any]) -> list[list[Any]]:
    """Read a catalog entry: active doc, or sibling ``file`` opened hidden."""
    file_bn = parsed.get("file")
    if not file_bn:
        grid = read_model_table_grid(active_doc, parsed)
        if parsed.get("kind") in ("sheet", "named_range") and not _grid_has_usable_values(grid):
            raise ToolExecutionError(f"Table identity {parsed.get('kind')} resolved to an empty range; nothing to register for SQL", code="DUCKDB_SQL_ERROR")
        return grid

    full_path = os.path.join(scoped_dir, str(file_bn)) if scoped_dir else str(file_bn)
    if not os.path.isfile(full_path):
        raise ToolExecutionError(f"Sibling spreadsheet {file_bn!r} not found under the document folder", code="DUCKDB_SQL_ERROR")
    return _read_sibling_office_file_as_grid(ctx, full_path, parsed)


def _sibling_office_error(full_path: str, message: str, *, sheet: str | None = None, code: str = "DUCKDB_SQL_ERROR") -> ToolExecutionError:
    bn = os.path.basename(full_path)
    where = f" sheet {sheet!r}" if sheet else ""
    return ToolExecutionError(f"Sibling spreadsheet {bn!r}{where}: {message}", code=code)


def _source_is_open_workbook(ctx: Any, full_path: str) -> bool:
    """True when *full_path* is already loaded on the desktop.

    The live workbook must not go through the ODS cache: unsaved edits would
    be skipped, and ``storeToURL`` would export a user-visible document.
    """
    import uno

    from plugin.framework.uno_context import resolve_document_by_url

    url = uno.systemPathToFileUrl(os.path.normpath(os.path.abspath(full_path)))
    return uno_probe(lambda: resolve_document_by_url(ctx, url)[0] is not None, False)


def _export_model_to_cached_ods(model: Any, dest: Path) -> None:
    """Save As ODS to *dest* (atomic replace via a ``.partial`` sibling)."""
    import uno

    from plugin.writer.format import create_property_value

    store = getattr(model, "storeToURL", None)
    if not callable(store):
        raise TypeError("document cannot storeToURL")
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Unique per writer: a fixed ".partial" name let two cache writes unlink
    # each other's in-progress export.
    tmp = dest.parent / f"{dest.name}.{uuid.uuid4().hex}.partial"
    try:
        url = uno.systemPathToFileUrl(str(tmp.resolve()))
        props = (create_property_value("FilterName", "calc8"), create_property_value("Overwrite", True))
        store(url, props)
        # Bugfix: a storeToURL that wrote nothing used to be published as a hit.
        if not tmp.is_file() or tmp.stat().st_size == 0:
            raise OSError(f"storeToURL wrote no data to {tmp}")
        os.replace(str(tmp), str(dest))
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _resolve_sibling_open_path(ctx: Any, full_path: str) -> tuple[str, bool, OdsCacheEntry | None]:
    """Return ``(path_to_open, cache_hit, entry)`` for a sibling spreadsheet.

    Cache only ``.xlsx``/``.xls`` that are not already open. Native ``.ods``
    and the live workbook always open the source path.
    """
    from plugin.calc.ods_cache import cache_entry, is_cacheable_office_source, lookup_cached_ods, ods_cache_enabled

    if not is_cacheable_office_source(full_path) or not ods_cache_enabled():
        return full_path, False, None
    if _source_is_open_workbook(ctx, full_path):
        return full_path, False, None
    hit = lookup_cached_ods(full_path)
    if hit is not None:
        return str(hit), True, None
    return full_path, False, cache_entry(full_path)


def _maybe_write_ods_cache(model: Any, full_path: str, *, opened_flag: bool, entry: OdsCacheEntry | None) -> None:
    """On a conversion miss, Save As the imported XLSX/XLS into the folder cache.

    ``opened_flag`` is the live-workbook guard: False means we reused a
    desktop document, so we must not ``storeToURL`` it. Do not call
    ``_source_is_open_workbook`` here — after a hidden open the source
    *is* loaded, which would skip every miss write.
    """
    from plugin.calc.ods_cache import cache_entry, write_sidecar_meta

    if not opened_flag or entry is None:
        return
    try:
        _export_model_to_cached_ods(model, entry.ods)
        if cache_entry(full_path) != entry:
            # Bugfix: the source changed while it was imported; this ODS holds
            # the old content, so a sidecar would make the next lookup stale.
            log.debug("ODS cache write skipped: source changed during import: %s", full_path)
            try:
                entry.ods.unlink(missing_ok=True)
            except OSError:
                pass
            return
        write_sidecar_meta(entry.meta, entry.abs_path, entry.mtime_ns, entry.size)
    except Exception:
        log.exception("Failed to write ODS cache for %s", full_path)


def _read_sibling_office_file_as_grid(ctx: Any, full_path: str, parsed: dict[str, Any]) -> list[list[Any]]:
    """Open a sibling .xlsx/.ods hidden+readonly and read a table identity.

    Default / ``#SheetName`` is that sheet's used range (same
    ``createCursor`` + ``gotoStart/EndOfUsedArea`` path as ``SheetAnalyzer``).
    ``parsed`` is a ``parse_table_source_spec`` dict. Returns ``grid``.
    Raises ``ToolExecutionError`` on open, missing sheet/name, or
    empty/unusable used-range — never ``(None, None)``.

    Sibling ``.xlsx``/``.xls`` reuse ``writeragent_ods_cache/`` when the
    source mtime/size still match. Native ``.ods`` and the live workbook
    skip the cache. Table name stays the *source* stem so a cache-hit open
    does not register a hash filename.
    """
    open_path, cache_hit, entry = _resolve_sibling_open_path(ctx, full_path)
    model = None
    opened_flag = False
    try:
        model, doc_type, err, opened_flag = open_document_for_read(ctx, open_path)
        if cache_hit and (err or model is None):
            # Stale or unreadable cache file: fall back to the source XLSX/XLS
            # and rewrite the cache after a successful read.
            log.warning("Cached ODS unreadable for %s; re-importing source", full_path)
            if model is not None and opened_flag:
                close_document_research_document(model, opened_for_document_research=opened_flag)
            model = None
            opened_flag = False
            from plugin.calc.ods_cache import cache_entry

            entry = cache_entry(full_path)
            model, doc_type, err, opened_flag = open_document_for_read(ctx, full_path)
        if err or model is None:
            raise _sibling_office_error(full_path, err or "LibreOffice failed to open the file")
        if doc_type != "calc":
            raise _sibling_office_error(full_path, f"not a spreadsheet (type={doc_type!r})")

        try:
            grid = read_model_table_grid(model, parsed)
        except ToolExecutionError as exc:
            identity = parsed.get("sheet") or parsed.get("named_range")
            raise _sibling_office_error(full_path, str(exc), sheet=str(identity) if identity else None) from exc
        if parsed["kind"] in ("sheet", "named_range") and not _grid_has_usable_values(grid):
            identity = parsed.get("named_range") or parsed.get("sheet") or os.path.basename(full_path)
            raise _sibling_office_error(full_path, "used range is empty; nothing to register for SQL", sheet=str(identity) if identity else None)

        _maybe_write_ods_cache(model, full_path, opened_flag=opened_flag, entry=entry)

        return grid
    except ToolExecutionError:
        raise
    except Exception as exc:
        if is_disposed_exception(exc):
            raise
        raise _sibling_office_error(full_path, f"failed to read used range: {exc}") from exc
    finally:
        if model is not None and opened_flag:
            close_document_research_document(model, opened_for_document_research=opened_flag)
