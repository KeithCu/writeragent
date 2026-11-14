# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Verification for helper_domain and duckdb_sql."""

from __future__ import annotations

from plugin.scripting.helper_domain import ScriptFacts


def test_is_sql_result_matches_is_status_helper_result():
    from plugin.scripting.duckdb_sql import SQL_RESULT_ERROR_CODES, SQL_RESULT_HELPERS, is_sql_result
    from plugin.scripting.helper_domain import is_status_helper_result

    cases = [
        ({"status": "ok", "helper": "run_sql"}, True),
        ({"status": "ok", "helper": "other"}, False),
        ({"status": "error", "code": "READONLY_VIOLATION"}, True),
        ({"status": "error", "code": "DUCKDB_ERROR", "helper": "nope"}, True),
        ({"status": "error", "code": "NOT_A_CODE"}, False),
        ({"status": "error"}, False),
        ({"helper": "run_sql"}, False),
        ("not a dict", False),
        (None, False),
    ]
    for value, expected in cases:
        assert is_sql_result(value) is expected, value
        assert is_status_helper_result(value, SQL_RESULT_HELPERS, SQL_RESULT_ERROR_CODES) is expected, value


def test_parse_run_import_call_spec_literal() -> None:
    code1 = 'run_sql({"helper": "query_folder_sql", "params": {"files": ["a.csv"]}})'
    spec1 = ScriptFacts.from_source(code1).first_run_spec("run_sql")
    assert isinstance(spec1, dict)
    assert spec1.get("helper") == "query_folder_sql"
    assert spec1.get("params") == {"files": ["a.csv"]}
