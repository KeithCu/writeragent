# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.calc.duckdb_tools (QueryFolderSqlTool)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from plugin.calc.duckdb_tools import QueryFolderSqlTool


def _mk_ctx():
    return SimpleNamespace(ctx=object(), doc=object(), doc_type="calc", active_domain="analysis")


def test_query_folder_sql_tool_basic_schema():
    t = QueryFolderSqlTool()
    assert t.name == "query_folder_sql"
    p = t.parameters
    assert "sql" in p["properties"]
    assert "sql" in p.get("required", [])


def test_query_folder_sql_requires_sql():
    t = QueryFolderSqlTool()
    ctx = _mk_ctx()
    res = t.execute(ctx, sql="")
    assert res["status"] == "error"


@patch("plugin.calc.duckdb_tools.execute_on_main_thread")
@patch("plugin.scripting.client.run_folder_sql")
@patch("plugin.calc.duckdb_tools.resolve_listing_directory")
def test_query_folder_sql_calls_host_with_resolved_dir(mock_resolve, mock_run, mock_exec):
    mock_resolve.return_value = "/tmp/project"
    mock_run.return_value = {"status": "ok", "helper": "query_folder_sql", "total_rows": 3}
    mock_exec.side_effect = lambda fn: fn()

    t = QueryFolderSqlTool()
    ctx = _mk_ctx()
    res = t.execute(ctx, sql="SELECT 1", files=["a.csv"])

    assert res["status"] == "ok"
    mock_resolve.assert_called()
    mock_run.assert_called_once()
    args = mock_run.call_args[0]
    assert args[1] == "/tmp/project"
    assert "SELECT 1" in args[2]
