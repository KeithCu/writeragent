# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""DuckDB SQL helpers and Run-Python-Script templates (host / LO process).

Compute is lazy-loaded from ``plugin.scripting.venv.duckdb_sql`` via ``__getattr__``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from plugin.scripting._lazy_venv import make_getattr

# --- Constants (host) ---

SQL_HELPER_NAMES = frozenset({"query_folder_sql"})

SQL_HEADER_PREFIX = "# writeragent:sql"
_SQL_HEADER_RE = re.compile(
    r"^\s*#\s*writeragent:sql\s+helper=(\w+)\s+params=(\{.*\})\s*$",
    re.MULTILINE,
)

_DEFAULT_PARAMS: dict[str, dict[str, Any]] = {
    "query_folder_sql": {"files": ["data.csv"]},
}

_HELPER_DESCRIPTIONS: dict[str, str] = {
    "query_folder_sql": "Run read-only SQL against CSV/Parquet/JSON files in the same folder as the saved document",
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


@dataclass
class SqlScriptMeta:
    helper: str
    params: dict[str, Any]


def parse_sql_script_header(code: str) -> SqlScriptMeta | None:
    """Parse machine header from SQL script template."""
    if not code or SQL_HEADER_PREFIX not in code:
        return None
    match = _SQL_HEADER_RE.search(code)
    if not match:
        return None
    helper = match.group(1)
    if helper not in SQL_HELPER_NAMES:
        return None
    try:
        params = json.loads(match.group(2))
    except Exception:
        params = {}
    if not isinstance(params, dict):
        params = {}
    return SqlScriptMeta(helper=helper, params=params)
