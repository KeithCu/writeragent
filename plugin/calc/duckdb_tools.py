# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Calc DuckDB SQL tools (folder queries via trusted venv helper)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from plugin.calc.base import ToolCalcAnalysisBase
from plugin.doc.document_research import get_document_directory, resolve_listing_directory
from plugin.framework.errors import ToolExecutionError
from plugin.framework.queue_executor import execute_on_main_thread

if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext

logger = logging.getLogger("writeragent.calc.duckdb")


class QueryFolderSqlTool(ToolCalcAnalysisBase):
    """Run read-only SQL (DuckDB) over CSV/Parquet/JSON files next to the Calc document.

    Lives under the analysis specialized domain. Host resolves scoped_dir; worker
    enforces the prefix + read-only policy.
    """

    name = "query_folder_sql"
    description = (
        "Run read-only SQL (via DuckDB) against tabular files (CSV, Parquet, JSON) in the same folder "
        "as the saved document. Supply sql and optional files (basenames). The host supplies the "
        "document folder securely; do not pass absolute or ../ paths."
    )
    parameters = {
        "type": "object",
        "properties": {
            "sql": {"type": "string", "description": "The SQL query. Reference files by basename e.g. FROM 'sales.csv'."},
            "files": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional list of allowed basenames to reference (host validates they are under the document folder).",
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
        else:
            files = []

        task_hint = str(kwargs.get("task_hint") or "") or None

        from plugin.scripting.client import run_folder_sql

        def _run() -> dict[str, Any]:
            # Prefer listing dir (handles untitled -> Work) then fall back
            scoped = resolve_listing_directory(ctx.ctx, ctx.doc) or get_document_directory(ctx.doc)
            return run_folder_sql(ctx.ctx, scoped, sql, files or None)

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
