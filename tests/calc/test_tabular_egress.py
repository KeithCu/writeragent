# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for shared Calc tabular egress."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from plugin.calc.tabular_egress import format_tabular_helper_for_calc, insert_tabular_result_into_calc


def test_truncated_table_shows_total_on_the_sheet():
    grid = format_tabular_helper_for_calc(
        {
            "status": "ok",
            "helper": "clean_and_prepare",
            "tables": [
                {
                    "name": "cleaned_data",
                    "columns": ["x"],
                    "rows": [[1]],
                    "truncated": True,
                    "total_rows": 250,
                }
            ],
        },
        domain_label="Analysis",
        default_helper="analysis",
        failed_message="failed",
    )
    flat = [str(cell) for row in grid for cell in row]
    assert any("250 total" in cell for cell in flat)


def test_error_result_is_error_grid():
    grid = format_tabular_helper_for_calc(
        {"status": "error", "code": "X", "message": "boom"},
        domain_label="Analysis",
        default_helper="analysis",
        failed_message="failed",
    )
    assert grid == [["Analysis error (X)"], ["boom"]]


def test_missing_context_uses_helper_as_title():
    grid = format_tabular_helper_for_calc(
        {"status": "ok", "helper": "describe_data"},
        domain_label="Analysis",
        default_helper="analysis",
        failed_message="failed",
    )
    assert grid[0] == ["describe_data"]


def test_metrics_flags_and_tables_keep_blank_row_separation():
    grid = format_tabular_helper_for_calc(
        {
            "status": "ok",
            "helper": "describe_data",
            "metrics": {"mean": 10},
            "flags": ["high_variance"],
            "tables": [
                {
                    "name": "table",
                    "columns": ["col"],
                    "rows": [[1]],
                }
            ],
        },
        domain_label="Analysis",
        default_helper="analysis",
        failed_message="failed",
    )
    expected = [
        ["describe_data"],
        [],
        ["Metrics"],
        ["Key", "Value"],
        ["mean", 10],
        [],
        ["Flags"],
        ["high_variance"],
        [],
        ["table"],
        ["col"],
        [1],
    ]
    assert grid == expected


def test_multiple_tables_and_truncation_note_without_total():
    grid = format_tabular_helper_for_calc(
        {
            "status": "ok",
            "helper": "inspect_tables",
            "tables": [
                {
                    "name": "t1",
                    "columns": ["a"],
                    "rows": [[1]],
                    "truncated": True,
                    "total_rows": 5,
                },
                {
                    "name": "t2",
                    "columns": ["b"],
                    "rows": [[2]],
                    "truncated": True,
                },
            ],
        },
        domain_label="Analysis",
        default_helper="analysis",
        failed_message="failed",
    )
    assert ["t1"] in grid
    assert ["(showing first rows; 5 total)"] in grid
    assert [] in grid
    assert ["t2"] in grid
    assert ["(truncated)"] in grid
    t1_note_idx = grid.index(["(showing first rows; 5 total)"])
    t2_idx = grid.index(["t2"])
    assert t2_idx == t1_note_idx + 2
    assert grid[t1_note_idx + 1] == []


def test_non_list_row_becomes_one_cell():
    grid = format_tabular_helper_for_calc(
        {
            "status": "ok",
            "helper": "describe_data",
            "tables": [
                {
                    "name": "t",
                    "columns": ["c"],
                    "rows": [["a"], "b"],
                }
            ],
        },
        domain_label="Analysis",
        default_helper="analysis",
        failed_message="failed",
    )
    assert ["a"] in grid
    assert ["b"] in grid


def test_no_tabular_output_placeholder():
    grid = format_tabular_helper_for_calc(
        {"status": "ok"},
        domain_label="Analysis",
        default_helper="analysis",
        failed_message="failed",
    )
    assert grid[-1] == ["(no tabular output)"]


@patch("plugin.calc.tabular_egress.CellManipulator")
@patch("plugin.calc.tabular_egress.CalcBridge")
def test_insert_pads_ragged_grid_and_uses_data_mode(mock_bridge, mock_manipulator_cls):
    manipulator = MagicMock()
    mock_manipulator_cls.return_value = manipulator
    doc = MagicMock()
    ctx = MagicMock()
    grid = [["title"], [], ["a", "b"]]
    rows = insert_tabular_result_into_calc(doc, ctx, grid, start_col=0, start_row=0)
    assert rows == 3
    manipulator.write_formula_range.assert_called_once()
    call_args = manipulator.write_formula_range.call_args
    assert call_args.args[1] == [["title", ""], ["", ""], ["a", "b"]]
    assert call_args.kwargs["data_text"] is True


@patch("plugin.calc.tabular_egress.CellManipulator")
@patch("plugin.calc.tabular_egress.CalcBridge")
def test_insert_quotes_sheet_name(mock_bridge, mock_manipulator_cls):
    manipulator = MagicMock()
    mock_manipulator_cls.return_value = manipulator
    doc = MagicMock()
    ctx = MagicMock()
    grid = [["a"]]
    insert_tabular_result_into_calc(doc, ctx, grid, sheet_name="O'Brien", start_col=0, start_row=0)
    manipulator.write_formula_range.assert_called_once()
    addr = manipulator.write_formula_range.call_args.args[0]
    assert addr == "'O''Brien'.A1"
