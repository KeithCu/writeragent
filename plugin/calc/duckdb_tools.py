# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Calc DuckDB SQL tools (folder queries via trusted venv helper)."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

from plugin.calc.base import ToolCalcAnalysisBase
from plugin.doc.document_research import get_document_directory, resolve_listing_directory, open_document_for_read, close_document_research_document
from plugin.framework.errors import ToolExecutionError
from plugin.framework.queue_executor import execute_on_main_thread
from plugin.scripting.config_limits import configured_python_max_data_cells
from plugin.calc.calc_addin_data import check_python_data_size

if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext

log = logging.getLogger("writeragent.calc.duckdb")


class QueryFolderSqlTool(ToolCalcAnalysisBase):
    """Run read-only SQL (DuckDB) over folder files and/or live active sheet ranges.

    Supports files (direct + LO for spreadsheets) and data_range (active sheet -> table 'data').
    Lives under analysis domain. Host performs all UNO reads and validation; worker registers tables
    and executes read-only SQL.
    """

    name = "query_folder_sql"
    description = (
        "Run read-only SQL (via DuckDB) against folder files and/or live Calc ranges (Phase C multi-table). "
        "Use tables={name: {range, headers}} for multiple named ranges from active doc. "
        "files as list or {name: spec} for folder. "
        "Tables registered by name (FROM sales etc). Host prepares all UNO data + validates."
    )
    parameters = {
        "type": "object",
        "properties": {
            "sql": {"type": "string", "description": "The SQL query. Use FROM data for active sheet range, or FROM 'file.csv' / 'budget.xlsx' for folder files."},
            "files": {
                "type": "object",
                "additionalProperties": {"type": "string"},
                "description": "Folder files as name -> basename/spec (e.g. {\"ledger\": \"ledger.parquet\"}). A list of basenames is still accepted. Office files auto-preloaded with name as table.",
            },
            "data_range": {"type": "string", "description": "A1 range on the active sheet (e.g. 'Sheet1.A1:F500' or 'A1:D100'). Becomes table 'data' (use headers param)."},
            "headers": {"type": "boolean", "description": "First row of data_range (or preloaded) contains column headers (default true)."},
            "tables": {
                "type": "object",
                "additionalProperties": {
                    "type": "object",
                    "properties": {
                        "range": {"type": "string"},
                        "headers": {"type": "boolean"}
                    }
                },
                "description": "Multi-table catalog for Phase C: named ranges from active doc. e.g. {\"sales\": {\"range\": \"Sales.A1:F500\", \"headers\": true}, \"costs\": {\"range\": \"Costs.A1:D200\"}}. Mix with files."
            },
            "task_hint": {"type": "string", "description": "Optional hint for logging/context."},
        },
        "required": ["sql"],
    }
    long_running = True

    def is_async(self) -> bool:
        return True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        sql = str(kwargs.get("sql") or "").strip()
        if not sql:
            return self._tool_error("sql is required")

        files_raw = kwargs.get("files") or []
        files: list[str] | dict[str, str]
        if isinstance(files_raw, (list, tuple)):
            files = [str(x) for x in files_raw if str(x).strip()]
        elif isinstance(files_raw, dict):
            files = {str(k): str(v) for k,v in files_raw.items() if str(v).strip()}
        else:
            files = []

        data_range = str(kwargs.get("data_range") or "").strip() or None
        headers = bool(kwargs.get("headers", True))

        tables_raw = kwargs.get("tables") or {}
        tables = dict(tables_raw) if isinstance(tables_raw, dict) else {}
        if data_range and "data" not in tables:
            tables["data"] = {"range": data_range, "headers": headers}

        task_hint = str(kwargs.get("task_hint") or "") or None

        from plugin.scripting.client import run_folder_sql

        def _run() -> dict[str, Any]:
            # Prefer listing dir (handles untitled -> Work) then fall back
            scoped = resolve_listing_directory(ctx.ctx, ctx.doc) or get_document_directory(ctx.doc)

            preloaded: dict[str, Any] = {}
            direct_files: list[str] = []
            flat_files: dict[str, str] = {}

            # Phase C: named tables from ranges on the *active/current* document (multi supported)
            for tbl_name, spec in (tables or {}).items():
                if not tbl_name:
                    continue
                rng = spec.get("range") if isinstance(spec, dict) else spec
                th = bool(spec.get("headers", headers)) if isinstance(spec, dict) else headers
                if not rng:
                    continue
                try:
                    from plugin.calc.inspector import CellInspector
                    from plugin.calc.bridge import CalcBridge
                    from plugin.calc.calc_addin_data import values_from_inspector_range
                    bridge = CalcBridge(ctx.doc)
                    inspector = CellInspector(bridge)
                    raw = inspector.read_range(str(rng))
                    grid = values_from_inspector_range(raw)
                    preloaded[tbl_name] = {"grid": grid, "headers": th}
                except Exception as e:
                    return self._tool_error(f"Failed to read table '{tbl_name}' range '{rng}': {e}")

            # Separate direct DuckDB-readable files from office files that need LO import.
            # Support "file.xlsx" or "file.xlsx#SheetName" syntax for sheets.
            # files can be list (legacy) or dict for named (Phase C)
            OFFICE_EXTS = (".xlsx", ".xls", ".ods")
            files_input = files
            file_pairs = files_input.items() if isinstance(files_input, dict) else [(None, f) for f in (files_input or [])]

            for name_hint, fspec in file_pairs:
                spec = str(fspec).strip() if fspec else ""
                if not spec:
                    continue
                # Parse optional #sheet hint
                if "#" in spec:
                    bn_part, sheet_part = spec.rsplit("#", 1)
                    sheet_hint = sheet_part.strip() or None
                    bn = os.path.basename(bn_part.strip())
                else:
                    bn = os.path.basename(spec)
                    sheet_hint = None

                ext = os.path.splitext(bn)[1].lower()
                full_path = os.path.join(scoped, bn) if scoped else bn
                if scoped and os.path.isfile(full_path):
                    if ext in OFFICE_EXTS:
                        # Office files that exist must preload or fail loud. A silent
                        # (None, None) skip used to append the basename to direct_files
                        # so SQL ran without the table and looked like a missing FROM.
                        try:
                            _tbl, office_grid = _read_sibling_office_file_as_grid(
                                ctx.ctx, full_path, sheet_hint=sheet_hint
                            )
                        except ToolExecutionError as exc:
                            return self._tool_error(str(exc), code=getattr(exc, "code", "DUCKDB_SQL_ERROR"))
                        use_name = name_hint or bn
                        preloaded[use_name] = {"grid": office_grid, "headers": headers}
                        continue
                    else:
                        # flat file -> use flat_files for named direct DuckDB read (Phase C)
                        use_name = name_hint or bn
                        flat_files[use_name] = full_path
                        continue
                direct_files.append(bn)

            # Enforce the same data size limit used for analysis / =PY()
            max_cells = configured_python_max_data_cells(ctx.ctx)
            for name, entry in list(preloaded.items()):
                g = entry.get("grid") if isinstance(entry, dict) and "grid" in entry else entry
                if g:
                    size_err = check_python_data_size(g, max_cells=max_cells)
                    if size_err:
                        return self._tool_error(f"Preloaded table {name} too large for DuckDB SQL: {size_err}")

            # Pass flat_files for named direct flat files (Phase C), preloaded for grids (ranges + office)
            return run_folder_sql(ctx.ctx, scoped, sql, direct_files or None, preloaded=preloaded or None, flat_files=flat_files or None)

        try:
            result = execute_on_main_thread(_run)
        except ToolExecutionError as exc:
            return self._tool_error(str(exc), code=getattr(exc, "code", "DUCKDB_SQL_ERROR"))
        except Exception as exc:
            log.exception("query_folder_sql execute failed")
            return self._tool_error(f"Failed to run folder SQL: {exc}")

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


def _sheet_qualified_a1(sheet_name: str, range_str: str) -> str:
    """Qualify an A1 range so CellInspector hits *sheet_name* without setActiveSheet.

    Hidden sibling opens often lack a usable controller; a sheet prefix is the
    same resolve path live-range tools already use (``CalcBridge.resolve``).
    """
    if any(ch in sheet_name for ch in " .!'"):
        return f"'{sheet_name}'.{range_str}"
    return f"{sheet_name}.{range_str}"


def _sibling_office_error(full_path: str, message: str, *, sheet: str | None = None, code: str = "DUCKDB_SQL_ERROR") -> ToolExecutionError:
    bn = os.path.basename(full_path)
    if sheet:
        return ToolExecutionError(f"Sibling spreadsheet {bn!r} sheet {sheet!r}: {message}", code=code)
    return ToolExecutionError(f"Sibling spreadsheet {bn!r}: {message}", code=code)


def _read_sibling_office_file_as_grid(ctx: Any, full_path: str, sheet_hint: str | None = None) -> tuple[str, list[list[Any]]]:
    """Open a sibling .xlsx/.ods hidden+readonly and read the sheet used range.

    *sheet_hint* is the optional ``#SheetName`` from the files spec.
    Returns ``(table_name, grid)``. Raises ``ToolExecutionError`` on open,
    missing sheet, or empty/unusable used-range — never ``(None, None)``.

    Used-range uses the same ``createCursor`` + ``gotoStart/EndOfUsedArea``
    path as ``SheetAnalyzer`` / ``ingest._used_range_address``. The previous
    hardcoded ``A1:AK2000`` / ``A1:AZ5000`` fallback padded most tables with
    thousands of empty cells and hid open/sheet failures as a silent skip.
    """
    from plugin.calc.bridge import CalcBridge
    from plugin.calc.calc_addin_data import values_from_inspector_range
    from plugin.calc.inspector import CellInspector
    from plugin.calc.spreadsheet_import.ingest import _used_range_address, used_range_string_from_address

    model = None
    opened_flag = False
    try:
        model, doc_type, err, opened_flag = open_document_for_read(ctx, full_path)
        if err or model is None:
            raise _sibling_office_error(full_path, err or "LibreOffice failed to open the file")
        if doc_type != "calc":
            raise _sibling_office_error(full_path, f"not a spreadsheet (type={doc_type!r})")

        bridge = CalcBridge(model)
        sheets = model.getSheets()
        if sheet_hint:
            # CalcBridge.get_sheet lists available names — do not fall back to sheet 0.
            try:
                target_sheet = bridge.get_sheet(sheet_hint)
            except ValueError as exc:
                raise _sibling_office_error(full_path, str(exc), sheet=sheet_hint) from exc
        else:
            if sheets.getCount() < 1:
                raise _sibling_office_error(full_path, "workbook has no sheets")
            target_sheet = sheets.getByIndex(0)

        sheet_name = str(target_sheet.getName())
        addr = _used_range_address(target_sheet)
        range_str = used_range_string_from_address(addr)
        qualified = _sheet_qualified_a1(sheet_name, range_str)

        inspector = CellInspector(bridge)
        raw = inspector.read_range(qualified)
        grid = values_from_inspector_range(raw)
        if not _grid_has_usable_values(grid):
            raise _sibling_office_error(
                full_path,
                f"used range {range_str} is empty; nothing to register for SQL",
                sheet=sheet_name,
            )

        tbl_name = os.path.splitext(os.path.basename(full_path))[0]
        tbl_name = "".join(c if c.isalnum() or c in "_$" else "_" for c in tbl_name)
        if not tbl_name or tbl_name[0].isdigit():
            tbl_name = "sheet_" + tbl_name

        return tbl_name, grid
    except ToolExecutionError:
        raise
    except Exception as exc:
        log.exception("Failed to read sibling office file %s for DuckDB", full_path)
        raise _sibling_office_error(full_path, f"failed to read used range: {exc}") from exc
    finally:
        if model is not None and opened_flag:
            try:
                close_document_research_document(model, opened_for_document_research=opened_flag)
            except Exception:
                pass
