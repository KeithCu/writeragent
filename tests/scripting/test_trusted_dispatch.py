# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for per-domain trusted venv dispatchers."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from plugin.scripting.venv.trusted_dispatch import (
    _packet_parts,
    _require_str,
    dispatch_spec_domain,
    dispatch_sql,
    dispatch_text,
    parse_trusted_spec,
)


def test_trusted_action_spec_copies_params() -> None:
    packet = {"helper": "convert_quantity", "params": {"value": "1"}, "headers": False}
    spec, _data_range, _context = _packet_parts(packet)
    assert spec["params"] == {"value": "1"}
    assert spec["params"] is not packet["params"]
    spec["params"]["value"] = "2"
    spec["params"].pop("value")
    assert packet["params"] == {"value": "1"}
    assert spec["headers"] is False


def test_trusted_action_spec_rejects_non_dict_params() -> None:
    with pytest.raises(ValueError, match="params must be a dict"):
        _packet_parts({"helper": "convert_quantity", "params": ["value"]})
    with pytest.raises(ValueError, match="params must be a dict"):
        dispatch_text({"helper": "full", "params": "x", "text": "t"})
    spec, _data_range, _context = _packet_parts({"helper": "convert_quantity", "params": None})
    assert spec["params"] == {}


def test_require_str_rejects_blank_and_keeps_padding() -> None:
    assert _require_str("  en-US  ", "bcp47") == "  en-US  "
    assert _require_str("  select 1  ", "sql") == "  select 1  "
    with pytest.raises(ValueError, match="text is required"):
        _require_str("   ", "text")
    with pytest.raises(ValueError, match="sql is required"):
        _require_str(None, "sql")


def test_dispatch_units_passes_data_range_through_spec_adapter() -> None:
    packet = {
        "domain": "units",
        "helper": "convert_quantity",
        "params": {"value": "1", "from_unit": "m", "to_unit": "ft"},
        "data_range": [["x"]],
        "context": {"task_hint": "convert"},
        "headers": False,
        "header_row": 1,
    }
    with patch("plugin.scripting.venv.units.run_units", return_value={"status": "ok"}) as mock_run:
        result = dispatch_spec_domain(packet)
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
        "domain": "symbolic",
        "helper": "symbolic_simplify",
        "params": {"expression": "x"},
        "data_range": [[1]],
        "context": {"task_hint": "simplify"},
    }
    with patch("plugin.scripting.venv.symbolic.run_symbolic", return_value={"status": "ok"}) as mock_run:
        result = dispatch_spec_domain(packet)
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


def test_parse_trusted_spec_layout_errors():
    names = frozenset({"describe_data"})

    # header_row: None, "abc", True, and 1.5 each return code=="INVALID_SPEC" and do not raise
    for bad in (None, "abc", True, 1.5):
        spec = {"helper": "describe_data", "header_row": bad}
        res = parse_trusted_spec(spec, helper_names=names)
        assert isinstance(res, dict)
        assert res["status"] == "error"
        assert res["code"] == "INVALID_SPEC"

    # "header_row": "2" returns tuple index 3 equal to 2
    spec_str_row = {"helper": "describe_data", "header_row": "2"}
    res_str_row = parse_trusted_spec(spec_str_row, helper_names=names)
    assert not isinstance(res_str_row, dict)
    assert res_str_row[3] == 2

    # Missing header_row is 0
    spec_missing_row = {"helper": "describe_data"}
    res_missing_row = parse_trusted_spec(spec_missing_row, helper_names=names)
    assert not isinstance(res_missing_row, dict)
    assert res_missing_row[3] == 0

    # headers: "false" returns tuple index 2 False
    spec_false_str = {"helper": "describe_data", "headers": "false"}
    res_false_str = parse_trusted_spec(spec_false_str, helper_names=names)
    assert not isinstance(res_false_str, dict)
    assert res_false_str[2] is False

    # headers: "true" is True
    spec_true_str = {"helper": "describe_data", "headers": "true"}
    res_true_str = parse_trusted_spec(spec_true_str, helper_names=names)
    assert not isinstance(res_true_str, dict)
    assert res_true_str[2] is True

    # Missing headers is True
    spec_missing_hdr = {"helper": "describe_data"}
    res_missing_hdr = parse_trusted_spec(spec_missing_hdr, helper_names=names)
    assert not isinstance(res_missing_hdr, dict)
    assert res_missing_hdr[2] is True

    # headers: None is True
    spec_none_hdr = {"helper": "describe_data", "headers": None}
    res_none_hdr = parse_trusted_spec(spec_none_hdr, helper_names=names)
    assert not isinstance(res_none_hdr, dict)
    assert res_none_hdr[2] is True

    # headers: 2 is INVALID_SPEC
    spec_bad_hdr = {"helper": "describe_data", "headers": 2}
    res_bad_hdr = parse_trusted_spec(spec_bad_hdr, helper_names=names)
    assert isinstance(res_bad_hdr, dict)
    assert res_bad_hdr["status"] == "error"
    assert res_bad_hdr["code"] == "INVALID_SPEC"

    # A bad headers value is reported even if header_row is also bad (headers checked first)
    spec_both_bad = {"helper": "describe_data", "headers": 2, "header_row": "abc"}
    res_both_bad = parse_trusted_spec(spec_both_bad, helper_names=names)
    assert isinstance(res_both_bad, dict)
    assert res_both_bad["status"] == "error"
    assert res_both_bad["code"] == "INVALID_SPEC"
    assert "headers" in res_both_bad["message"]

