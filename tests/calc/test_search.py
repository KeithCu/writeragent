# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for Calc search tools."""

from __future__ import annotations

import types
from unittest.mock import MagicMock, patch

import pytest

from plugin.calc.search import SearchInSpreadsheet
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
        assert mock_search.call_args.kwargs["max_results"] == 5


def test_search_in_spreadsheet_forwards_stop_checker():
    tool = SearchInSpreadsheet()
    scope = SendCancellation()
    ctx = types.SimpleNamespace(doc=MagicMock(), send_cancellation=scope)
    with patch("plugin.calc.search.search_spreadsheet_cells", return_value=[]) as mock_search:
        res = tool.execute(ctx, pattern="test")
        assert res["status"] == "ok"
        assert mock_search.call_args.kwargs["stop_checker"] == scope.is_cancelled
