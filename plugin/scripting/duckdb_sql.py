# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""DuckDB SQL helpers and Run-Python-Script templates (host / LO process).

Compute is lazy-loaded from ``plugin.scripting.venv.duckdb_sql`` via ``__getattr__``.
"""

from __future__ import annotations

import json
from typing import Any

from plugin.scripting._lazy_venv import make_getattr
from plugin.scripting.helper_domain import HelperScriptMeta, header_prefix, parse_helper_script_header

# --- Constants (host) ---

SQL_HELPER_NAMES = frozenset({"query_folder_sql", "query_sheet_sql"})
SQL_RESULT_HELPERS = frozenset({"query_folder_sql", "query_sheet_sql", "run_sql"})

SQL_HEADER_PREFIX = header_prefix("sql")

_DEFAULT_PARAMS: dict[str, dict[str, Any]] = {
    "query_folder_sql": {"files": ["data.csv"]},
    "query_sheet_sql": {"data_range": "A1:F100"},
}

_HELPER_DESCRIPTIONS: dict[str, str] = {
    "query_folder_sql": "Run read-only SQL against CSV/Parquet/JSON files (or .xlsx via LO) in the same folder as the saved document",
    "query_sheet_sql": "Run read-only SQL on a live range from the active Calc sheet (registers as table 'data')",
}

_SQL_ROW_CAP_NOTE = (
    "Results cap at 200 rows (MAX_TABLE_ROWS). truncated/warning/flags mean "
    "the table is incomplete — add LIMIT or aggregate."
)


_SQL_VENV_EXPORTS = frozenset(
    {
        "query_folder_sql",
        "run_sql",
        "session_duckdb",
        "reset_session_duckdb",
        "invalidate_session_tables",
        "persistable_duckdb_session_id",
        "GuardedDuckDBConnection",
        "ReadonlyViolation",
        "MAX_TABLE_ROWS",
    }
)

__getattr__ = make_getattr("duckdb_sql", _SQL_VENV_EXPORTS)


# --- Templates for Run Python Script (Calc) ---

def _template_body(helper: str, params: dict[str, Any]) -> str:
    import sys

    if "crosshair" in sys.modules:
        params_json = "{}"
    else:
        params_json = json.dumps(params, separators=(",", ":"))
    desc = _HELPER_DESCRIPTIONS.get(helper, helper)
    if helper == "query_sheet_sql":
        return (
            f"{SQL_HEADER_PREFIX} helper={helper} params={params_json}\n"
            f"# {desc}\n"
            f"# {_SQL_ROW_CAP_NOTE}\n"
            f"# Set the Data range in the toolbar (or select cells), then Run.\n"
            f"from writeragent.scripting.duckdb_sql import query_folder_sql\n\n"
            f"result = query_folder_sql(\n"
            f'    None,  # folder not used for sheet\n'
            f'    "SELECT ... FROM data",\n'
            f"    None,\n"
            f"    {{\"data\": data}},  # provided by Run Python Script UI from data_range\n"
            f")\n"
        )
    return (
        f"{SQL_HEADER_PREFIX} helper={helper} params={params_json}\n"
        f"# {desc}\n"
        f"# {_SQL_ROW_CAP_NOTE}\n"
        f"# Files must live beside the saved .ods/.xlsx. Edit the files list.\n"
        f"from writeragent.scripting.duckdb_sql import query_folder_sql\n\n"
        f"result = query_folder_sql(\n"
        f"    None,  # resolved by runner from document folder (or pass explicit)\n"
        f'    "SELECT ... FROM \'yourfile.csv\'",\n'
        f"    {params_json}.get('files', ['yourfile.csv']),\n"
        f")\n"
    )


from plugin.framework.deal_shim import DEAL_MAX_SOURCE, str_bounded, deal


@deal.post(lambda result: isinstance(result, dict) and "query_folder_sql" in result and "query_sheet_sql" in result)
def get_sql_script_templates() -> dict[str, str]:
    """Return built-in SQL helper templates for the Run Python Script picker."""
    return {helper: _template_body(helper, dict(_DEFAULT_PARAMS.get(helper, {}))) for helper in sorted(SQL_HELPER_NAMES)}


SqlScriptMeta = HelperScriptMeta


@deal.pre(lambda code: str_bounded(code, DEAL_MAX_SOURCE))
@deal.post(lambda result: result is None or isinstance(result, SqlScriptMeta))
def parse_sql_script_header(code: str) -> SqlScriptMeta | None:
    """Parse machine header from SQL script template."""
    # Header regex ``params=({.*})`` hangs deep check even via this thin wrapper.
    # crosshair: off
    return parse_helper_script_header(code, tag="sql", helper_names=SQL_HELPER_NAMES)


def is_sql_result(value: Any) -> bool:
    """True when *value* matches the compact DuckDB SQL helper result contract."""
    if not isinstance(value, dict):
        return False
    if "status" not in value:
        return False
    helper = value.get("helper")
    if isinstance(helper, str) and helper in SQL_RESULT_HELPERS:
        return True
    if value.get("status") == "error":
        code = str(value.get("code") or "")
        return code in ("READONLY_VIOLATION", "DUCKDB_SQL_ERROR", "DUCKDB_ERROR", "MISSING_SCOPED_DIR")
    return False


def format_sql_for_calc(result: dict[str, Any]) -> list[list[Any]]:
    """Turn a SQL helper result into a row-major grid (truncation note via tabular egress)."""
    from plugin.calc.tabular_egress import format_tabular_helper_for_calc

    return format_tabular_helper_for_calc(
        result,
        domain_label="SQL",
        default_helper="query_folder_sql",
        failed_message="SQL query failed.",
    )


def insert_sql_result_into_calc(
    doc: Any,
    uno_ctx: Any,
    result: dict[str, Any],
    *,
    start_col: int | None = None,
    start_row: int | None = None,
) -> int:
    """Write formatted SQL output starting at the selection (or given anchor)."""
    from plugin.calc.tabular_egress import insert_tabular_result_into_calc

    grid = format_sql_for_calc(result)
    return insert_tabular_result_into_calc(
        doc,
        uno_ctx,
        grid,
        start_col=start_col,
        start_row=start_row,
    )
