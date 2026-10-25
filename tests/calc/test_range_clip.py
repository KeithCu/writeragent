# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for whole-axis range clipping in spreadsheet import."""

from __future__ import annotations

from plugin.calc.spreadsheet_import.range_clip import clip_workbook_data_ranges


def test_clip_workbook_data_ranges_quoted_and_bare_sheets():
    bounds = {"ACTUAL": (5, 54), "DASHBOARD FINISHED": (5, 54), "O'BRIEN": (5, 54)}
    ranges = [
        "Actual.F:F",
        "'Dashboard Finished'.C:C",
        "'O''Brien'.F:F",
        "Actual!F:F",
        "$Actual.$F:$F",
    ]
    clipped = clip_workbook_data_ranges(ranges, sheet_bounds=bounds)
    assert clipped == [
        "Actual.F1:F55",
        "'Dashboard Finished'.C1:C55",
        "'O''Brien'.F1:F55",
        "Actual!F1:F55",
        "Actual.F1:F55",
    ]


def test_clip_workbook_data_ranges_special_names():
    bounds = {"$PRICES": (5, 54), "2024": (5, 54), "Q1-SALES": (5, 54)}
    ranges = [
        "'$Prices'.F:F",
        "2024.F:F",
        "Q1-Sales.F:F",
    ]
    clipped = clip_workbook_data_ranges(ranges, sheet_bounds=bounds)
    assert clipped == [
        "'$Prices'.F1:F55",
        "'2024'.F1:F55",
        "'Q1-Sales'.F1:F55",
    ]


def test_clip_workbook_data_ranges_local_and_lowercase_bounds():
    bounds_lower = {"actual": (5, 54)}
    assert clip_workbook_data_ranges(["Actual.F:F"], sheet_bounds=bounds_lower) == ["Actual.F1:F55"]

    bounds = {"DASHBOARD FINISHED": (5, 54)}
    assert clip_workbook_data_ranges(["F:F"], sheet_bounds=bounds, current_sheet="Dashboard Finished") == ["F1:F55"]
    assert clip_workbook_data_ranges(["F:F"], sheet_bounds=bounds, current_sheet=None) == ["F:F"]


def test_clip_workbook_data_ranges_none_or_unknown_sheet():
    original = ["Actual.F:F", "Unknown.F:F"]
    res_none = clip_workbook_data_ranges(original, sheet_bounds=None)
    assert res_none == original
    assert res_none is not original

    bounds = {"ACTUAL": (5, 54)}
    assert clip_workbook_data_ranges(["Unknown.F:F"], sheet_bounds=bounds) == ["Unknown.F:F"]


def test_clip_workbook_data_ranges_non_axis_unchanged():
    non_axis = ["A1:A5", "A1", "Sales"]
    bounds = {"ACTUAL": (5, 54)}
    assert clip_workbook_data_ranges(non_axis, sheet_bounds=bounds) == non_axis


def test_clip_multi_column_and_row_ranges():
    bounds = {"S": (5, 54), "ACTUAL": (5, 54), "Q1.2024": (5, 54)}
    assert clip_workbook_data_ranges(["A:C"], sheet_bounds=bounds, current_sheet="S") == ["A1:C55"]
    assert clip_workbook_data_ranges(["1:3"], sheet_bounds=bounds, current_sheet="S") == ["A1:F3"]
    assert clip_workbook_data_ranges(["Actual.A:C"], sheet_bounds=bounds) == ["Actual.A1:C55"]
    assert clip_workbook_data_ranges(["Actual.1:3"], sheet_bounds=bounds) == ["Actual.A1:F3"]
    assert clip_workbook_data_ranges(["'Q1.2024'.A:C"], sheet_bounds=bounds) == ["'Q1.2024'.A1:C55"]
