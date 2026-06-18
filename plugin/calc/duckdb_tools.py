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

logger = logging.getLogger("writeragent.calc.duckdb")


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
                "type": ["array", "object"],
                "items": {"type": "string"},
                "additionalProperties": {"type": "string"},
                "description": "Folder files. List of basenames (legacy) or dict name -> basename/spec (e.g. {\"ledger\": \"ledger.parquet\"}). Office files auto-preloaded with name as table.",
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
                        tbl, grid = _read_sibling_office_file_as_grid(ctx.ctx, full_path, sheet_hint=sheet_hint)
                        if tbl and grid:
                            use_name = name_hint or bn
                            preloaded[use_name] = {"grid": grid, "headers": headers}
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
            logger.exception("query_folder_sql execute failed")
            return self._tool_error(f"Failed to run folder SQL: {exc}")

        if isinstance(result, dict):
            if task_hint:
                result = dict(result)
                result.setdefault("task_hint", task_hint)
            return result
        return {"status": "ok", "result": result}


def _read_sibling_office_file_as_grid(ctx: Any, full_path: str, sheet_hint: str | None = None) -> tuple[str | None, list[list[Any]] | None]:
    """Open a sibling .xlsx/.ods hidden+readonly, read a sheet into a grid of values.

    sheet_hint: optional sheet name (e.g. from "file.xlsx#Sales").
    Returns (table_name, grid) or (None, None) on failure.
    Table name is the basename without extension (safe for SQL).
    """
    model = None
    opened_flag = False
    try:
        model, doc_type, err, opened_flag = open_document_for_read(ctx, full_path)
        if err or model is None or doc_type != "calc":
            return None, None

        from plugin.calc.bridge import CalcBridge
        from plugin.calc.inspector import CellInspector
        from plugin.calc.calc_addin_data import values_from_inspector_range

        bridge = CalcBridge(model)

        # Try to activate the desired sheet so inspector.read_range uses it
        target_sheet = None
        sheets = model.getSheets()
        if sheet_hint:
            try:
                target_sheet = sheets.getByName(sheet_hint)
            except Exception:
                pass
        if target_sheet is None:
            # default to first sheet
            target_sheet = sheets.getByIndex(0)

        # For hidden docs, try to set active sheet on controller if possible
        try:
            controller = model.getCurrentController()
            if controller and hasattr(controller, "setActiveSheet") and target_sheet:
                controller.setActiveSheet(target_sheet)
        except Exception:
            pass  # hidden docs sometimes lack full controller; fall back to first anyway

        inspector = CellInspector(bridge)

        # Use a large but reasonable range. For production this could compute actual used area.
        # "A1:AK2000" covers most practical tables (columns A-AK = 37 cols).
        range_str = "A1:AK2000"
        try:
            raw = inspector.read_range(range_str)
            grid = values_from_inspector_range(raw)
        except Exception:
            raw = inspector.read_range("A1:AZ5000")
            grid = values_from_inspector_range(raw)

        tbl_name = os.path.splitext(os.path.basename(full_path))[0]
        tbl_name = "".join(c if c.isalnum() or c in "_$" else "_" for c in tbl_name)
        if not tbl_name or tbl_name[0].isdigit():
            tbl_name = "sheet_" + tbl_name

        return tbl_name, grid
    except Exception as e:
        logger.warning("Failed to read sibling office file %s for DuckDB: %s", full_path, e)
        return None, None
    finally:
        if model is not None and opened_flag:
            try:
                close_document_research_document(model, opened_for_document_research=opened_flag)
            except Exception:
                pass
