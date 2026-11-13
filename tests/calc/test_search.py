# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for Calc search tools."""

from __future__ import annotations

import types
from unittest.mock import MagicMock, patch

import pytest

from plugin.app import get_tools
from plugin.framework.send_cancellation import SendCancellation
from plugin.framework.tool import prepare_tool_call


def _tool(name: str):
    return get_tools().get(name)


@pytest.mark.parametrize("bad_max", [0, -1])
def test_search_in_spreadsheet_invalid_max_results(bad_max):
    tool = _tool("search_cells")
    ctx = types.SimpleNamespace(doc=MagicMock(), send_cancellation=None)
    with patch("plugin.calc.search.search_spreadsheet_cells") as mock_search:
        res = tool.execute_safe(ctx, pattern="test", max_results=bad_max)
        assert res["status"] == "error"
        assert res["code"] == "INVALID_PARAM"
        assert "max_results" in res["message"]
        mock_search.assert_not_called()


@pytest.mark.parametrize("bad_max", ["5", True])
def test_search_cells_schema_rejects_non_int_max_results(bad_max):
    tool = _tool("search_cells")
    prep = prepare_tool_call(
        tool,
        types.SimpleNamespace(doc_type="calc"),
        "search_cells",
        {"pattern": "test", "max_results": bad_max},
    )
    assert prep.error is not None
    assert prep.error["code"] == "VALIDATION_ERROR"


def test_search_in_spreadsheet_valid_max_results():
    tool = _tool("search_cells")
    ctx = types.SimpleNamespace(doc=MagicMock(), send_cancellation=None)
    with patch("plugin.calc.search.search_spreadsheet_cells", return_value=[{"cell": "A1", "value": "test"}]) as mock_search:
        res = tool.execute_safe(ctx, pattern="test", max_results=5)
        assert res["status"] == "ok"
        assert res["count"] == 1
        assert res["matched_case_sensitive"] is False
        assert mock_search.call_args.kwargs["max_results"] == 5
        assert mock_search.call_args.kwargs["case_sensitive"] is False


def test_search_in_spreadsheet_forwards_cancel():
    tool = _tool("search_cells")
    scope = SendCancellation()
    ctx = types.SimpleNamespace(doc=MagicMock(), send_cancellation=scope)
    with patch("plugin.calc.search.search_spreadsheet_cells", return_value=[]) as mock_search:
        res = tool.execute_safe(ctx, pattern="test")
        assert res["status"] == "ok"
        assert res["matched_case_sensitive"] is False
        assert mock_search.call_args.kwargs["cancel"] is scope


def test_search_in_spreadsheet_case_sensitive_explicit():
    tool = _tool("search_cells")
    ctx = types.SimpleNamespace(doc=MagicMock(), send_cancellation=None)
    with patch("plugin.calc.search.search_spreadsheet_cells", return_value=[]) as mock_search:
        res = tool.execute_safe(ctx, pattern="test", case_sensitive=True)
        assert res["status"] == "ok"
        assert res["matched_case_sensitive"] is True
        assert mock_search.call_args.kwargs["case_sensitive"] is True


def test_replace_in_spreadsheet_matched_case_sensitive():
    tool = _tool("search_replace")
    sheet = MagicMock()
    rd = MagicMock()
    sheet.createReplaceDescriptor.return_value = rd
    sheet.replaceAll.return_value = 2
    ctx = types.SimpleNamespace(doc=MagicMock())
    with patch("plugin.calc.search.iter_target_sheets", return_value=[(sheet, "Sheet1")]):
        res_default = tool.execute_safe(ctx, search="old", replace="new")
        assert res_default["status"] == "ok"
        assert res_default["replacements"] == 2
        assert res_default["matched_case_sensitive"] is False
        assert rd.SearchCaseSensitive is False

        res_cs = tool.execute_safe(ctx, search="old", replace="new", case_sensitive=True)
        assert res_cs["status"] == "ok"
        assert res_cs["replacements"] == 2
        assert res_cs["matched_case_sensitive"] is True
        assert rd.SearchCaseSensitive is True


def test_search_in_spreadsheet_invalid_regex():
    tool = _tool("search_cells")
    ctx = types.SimpleNamespace(doc=MagicMock(), ctx=MagicMock(), send_cancellation=None)
    ctx.ctx.ServiceManager.createInstanceWithContext.return_value.searchForward.return_value.subRegExpressions = 0
    res = tool.execute_safe(ctx, pattern="([a-", regex=True)
    assert res["status"] == "error"
    assert res["code"] == "INVALID_REGEX"


def test_replace_in_spreadsheet_invalid_regex():
    tool = _tool("search_replace")
    ctx = types.SimpleNamespace(doc=MagicMock(), ctx=MagicMock())
    ctx.ctx.ServiceManager.createInstanceWithContext.return_value.searchForward.return_value.subRegExpressions = 0
    res = tool.execute_safe(ctx, search="([a-", replace="x", regex=True)
    assert res["status"] == "error"
    assert res["code"] == "INVALID_REGEX"
