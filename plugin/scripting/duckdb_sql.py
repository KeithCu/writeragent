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

SQL_HEADER_PREFIX = header_prefix("sql")

_DEFAULT_PARAMS: dict[str, dict[str, Any]] = {
    "query_folder_sql": {"files": ["data.csv"]},
    "query_sheet_sql": {"data_range": "A1:F100"},
}

_HELPER_DESCRIPTIONS: dict[str, str] = {
    "query_folder_sql": "Run read-only SQL against CSV/Parquet/JSON files (or .xlsx via LO) in the same folder as the saved document",
    "query_sheet_sql": "Run read-only SQL on a live range from the active Calc sheet (registers as table 'data')",
}


_SQL_VENV_EXPORTS = frozenset(
    {
        "query_folder_sql",
    }
)

__getattr__ = make_getattr("duckdb_sql", _SQL_VENV_EXPORTS)


# --- Templates for Run Python Script (Calc) ---

def _template_body(helper: str, params: dict[str, Any]) -> str:
    params_json = json.dumps(params, separators=(",", ":"))
    desc = _HELPER_DESCRIPTIONS.get(helper, helper)
    if helper == "query_sheet_sql":
        return (
            f"{SQL_HEADER_PREFIX} helper={helper} params={params_json}\n"
            f"# {desc}\n"
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
        f"# Files must live beside the saved .ods/.xlsx. Edit the files list.\n"
        f"from writeragent.scripting.duckdb_sql import query_folder_sql\n\n"
        f"result = query_folder_sql(\n"
        f"    None,  # resolved by runner from document folder (or pass explicit)\n"
        f'    "SELECT ... FROM \'yourfile.csv\'",\n'
        f"    {params_json}.get('files', ['yourfile.csv']),\n"
        f")\n"
    )


def get_sql_script_templates() -> dict[str, str]:
    """Return built-in SQL helper templates for the Run Python Script picker."""
    return {helper: _template_body(helper, dict(_DEFAULT_PARAMS.get(helper, {}))) for helper in sorted(SQL_HELPER_NAMES)}


SqlScriptMeta = HelperScriptMeta


def parse_sql_script_header(code: str) -> SqlScriptMeta | None:
    """Parse machine header from SQL script template."""
    return parse_helper_script_header(code, tag="sql", helper_names=SQL_HELPER_NAMES)
