# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for Calc search tools."""

from __future__ import annotations

import types
from unittest.mock import MagicMock, patch

import pytest

from plugin.calc.search import ReplaceInSpreadsheet, SearchInSpreadsheet
from plugin.framework.send_cancellation import SendCancellation


@pytest.mark.parametrize("bad_max", [0, -1, "5", True])
def test_search_in_spreadsheet_invalid_max_results(bad_max):
    tool = SearchInSpreadsheet()
    ctx = types.SimpleNamespace(doc=MagicMock(), send_cancellation=None)
    with patch("plugin.calc.search.search_spreadsheet_cells") as mock_search:
        res = tool.execute(ctx, pattern="test", max_results=bad_max)
        assert res["status"] == "error"
        assert res["code"] == "INVALID_PARAM"
        assert "max_results" in res["message"]
        mock_search.assert_not_called()


def test_search_in_spreadsheet_valid_max_results():
    tool = SearchInSpreadsheet()
    ctx = types.SimpleNamespace(doc=MagicMock(), send_cancellation=None)
    with patch("plugin.calc.search.search_spreadsheet_cells", return_value=[{"cell": "A1", "value": "test"}]) as mock_search:
        res = tool.execute(ctx, pattern="test", max_results=5)
        assert res["status"] == "ok"
        assert res["count"] == 1
        assert res["matched_case_sensitive"] is False
        assert mock_search.call_args.kwargs["max_results"] == 5
        assert mock_search.call_args.kwargs["case_sensitive"] is False


def test_search_in_spreadsheet_forwards_cancel():
    tool = SearchInSpreadsheet()
    scope = SendCancellation()
    ctx = types.SimpleNamespace(doc=MagicMock(), send_cancellation=scope)
    with patch("plugin.calc.search.search_spreadsheet_cells", return_value=[]) as mock_search:
        res = tool.execute(ctx, pattern="test")
        assert res["status"] == "ok"
        assert res["matched_case_sensitive"] is False
        assert mock_search.call_args.kwargs["cancel"] is scope


def test_search_in_spreadsheet_case_sensitive_explicit():
    tool = SearchInSpreadsheet()
    ctx = types.SimpleNamespace(doc=MagicMock(), send_cancellation=None)
    with patch("plugin.calc.search.search_spreadsheet_cells", return_value=[]) as mock_search:
        res = tool.execute(ctx, pattern="test", case_sensitive=True)
        assert res["status"] == "ok"
        assert res["matched_case_sensitive"] is True
        assert mock_search.call_args.kwargs["case_sensitive"] is True


def test_replace_in_spreadsheet_matched_case_sensitive():
    tool = ReplaceInSpreadsheet()
    sheet = MagicMock()
    rd = MagicMock()
    sheet.createReplaceDescriptor.return_value = rd
    sheet.replaceAll.return_value = 2
    ctx = types.SimpleNamespace(doc=MagicMock())
    with patch("plugin.calc.search.iter_target_sheets", return_value=[(sheet, "Sheet1")]):
        res_default = tool.execute(ctx, search="old", replace="new")
        assert res_default["status"] == "ok"
        assert res_default["replacements"] == 2
        assert res_default["matched_case_sensitive"] is False
        assert rd.SearchCaseSensitive is False

        res_cs = tool.execute(ctx, search="old", replace="new", case_sensitive=True)
        assert res_cs["status"] == "ok"
        assert res_cs["replacements"] == 2
        assert res_cs["matched_case_sensitive"] is True
        assert rd.SearchCaseSensitive is True


def test_search_in_spreadsheet_invalid_regex():
    tool = SearchInSpreadsheet()
    ctx = types.SimpleNamespace(doc=MagicMock(), ctx=MagicMock(), send_cancellation=None)
    ctx.ctx.ServiceManager.createInstanceWithContext.return_value.searchForward.return_value.subRegExpressions = 0
    res = tool.execute_safe(ctx, pattern="([a-", regex=True)
    assert res["status"] == "error"
    assert res["code"] == "INVALID_REGEX"


def test_replace_in_spreadsheet_invalid_regex():
    tool = ReplaceInSpreadsheet()
    ctx = types.SimpleNamespace(doc=MagicMock(), ctx=MagicMock())
    ctx.ctx.ServiceManager.createInstanceWithContext.return_value.searchForward.return_value.subRegExpressions = 0
    res = tool.execute_safe(ctx, search="([a-", replace="x", regex=True)
    assert res["status"] == "error"
    assert res["code"] == "INVALID_REGEX"
