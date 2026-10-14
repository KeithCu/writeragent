# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for per-domain trusted venv dispatchers."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from plugin.scripting.venv.trusted_dispatch import (
    _require_str,
    _trusted_action_spec,
    dispatch_sql,
    dispatch_symbolic,
    dispatch_text,
    dispatch_units,
)


def test_trusted_action_spec_copies_params() -> None:
    packet = {"helper": "convert_quantity", "params": {"value": "1"}, "headers": False}
    spec = _trusted_action_spec(packet["helper"], packet["params"], packet)
    assert spec["params"] == {"value": "1"}
    assert spec["params"] is not packet["params"]
    spec["params"]["value"] = "2"
    spec["params"].pop("value")
    assert packet["params"] == {"value": "1"}
    assert spec["headers"] is False


def test_require_str_rejects_blank_and_keeps_padding() -> None:
    assert _require_str("  en-US  ", "bcp47") == "  en-US  "
    assert _require_str("  select 1  ", "sql") == "  select 1  "
    with pytest.raises(ValueError, match="text is required"):
        _require_str("   ", "text")
    with pytest.raises(ValueError, match="sql is required"):
        _require_str(None, "sql")


def test_dispatch_units_passes_data_range_through_spec_adapter() -> None:
    packet = {
        "helper": "convert_quantity",
        "params": {"value": "1", "from_unit": "m", "to_unit": "ft"},
        "data_range": [["x"]],
        "context": {"task_hint": "convert"},
        "headers": False,
        "header_row": 1,
    }
    with patch("plugin.scripting.venv.units.run_units", return_value={"status": "ok"}) as mock_run:
        result = dispatch_units(packet)
        assert result == {"status": "ok"}
        mock_run.assert_called_once()
        args, kwargs = mock_run.call_args
        assert not kwargs
        assert len(args) == 3
        spec, data_range, context = args
        assert spec["helper"] == "convert_quantity"
        assert spec["params"] == packet["params"]
        assert spec["params"] is not packet["params"]
        assert spec["headers"] is False
        assert spec["header_row"] == 1
        assert data_range == [["x"]]
        assert context == {"task_hint": "convert"}


def test_dispatch_symbolic_passes_data_range_through_spec_adapter() -> None:
    packet = {
        "helper": "symbolic_simplify",
        "params": {"expression": "x"},
        "data_range": [[1]],
        "context": {"task_hint": "simplify"},
    }
    with patch("plugin.scripting.venv.symbolic.run_symbolic", return_value={"status": "ok"}) as mock_run:
        result = dispatch_symbolic(packet)
        assert result == {"status": "ok"}
        mock_run.assert_called_once()
        args, kwargs = mock_run.call_args
        assert not kwargs
        assert len(args) == 3
        spec, data_range, context = args
        assert spec["helper"] == "symbolic_simplify"
        assert spec["params"] == packet["params"]
        assert spec["params"] is not packet["params"]
        assert "headers" not in spec
        assert data_range == [[1]]
        assert context == {"task_hint": "simplify"}


def test_dispatch_text_empty_string_does_not_fall_back() -> None:
    with patch(
        "plugin.scripting.venv.text_analytics.run_text_analytics", return_value={"status": "ok"}
    ) as mock_run:
        dispatch_text({"helper": "full", "params": {}, "text": "", "data_range": "fallback"})
        assert mock_run.call_args[0][1] == ""

        mock_run.reset_mock()
        dispatch_text({"helper": "full", "params": {}, "data_range": "fallback"})
        assert mock_run.call_args[0][1] == "fallback"

        mock_run.reset_mock()
        dispatch_text({"helper": "full", "params": {}, "text": None, "data_range": "fallback"})
        assert mock_run.call_args[0][1] == "fallback"


def test_dispatch_sql_ignores_helper_and_session_id() -> None:
    with patch(
        "plugin.scripting.venv.duckdb_sql.query_folder_sql", return_value={"status": "ok"}
    ) as mock_query:
        packet = {
            "helper": "run_sql",
            "session_id": "calc:wb",
            "scoped_dir": "/tmp/d",
            "sql": "  select 1  ",
            "files": ["a.csv"],
            "preloaded": {"t": [[1]]},
            "flat_files": {"a": "/tmp/d/a.csv"},
        }
        result = dispatch_sql(packet)
        assert result == {"status": "ok"}
        mock_query.assert_called_once_with(
            "/tmp/d",
            "  select 1  ",
            ["a.csv"],
            {"t": [[1]]},
            {"a": "/tmp/d/a.csv"},
        )

        with pytest.raises(ValueError, match="sql is required"):
            dispatch_sql({"sql": "   "})
        assert mock_query.call_count == 1
