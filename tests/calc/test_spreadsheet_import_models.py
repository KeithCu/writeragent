# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for spreadsheet import models."""

from __future__ import annotations

from plugin.calc.spreadsheet_import.models import ConversionReport, TodoCell


def test_conversion_report_distinct_total_and_rate():
    # Item 9: A1 converted, B1 pass_through and in skipped with reason
    report = ConversionReport(
        converted=["A1"],
        pass_through=["B1"],
        skipped=[TodoCell(address="B1", reason="PROMPT")],
    )
    assert report.formula_total() == 2
    assert report.conversion_rate() == 0.5
    assert report.summary().startswith("Converted 1 / 2")
    payload = report.to_dict()
    assert payload["conversion_rate"] == 0.5


def test_conversion_report_empty():
    report = ConversionReport()
    assert report.formula_total() == 0
    assert report.conversion_rate() == 0.0
    assert report.summary() == "Converted 0 / 0 formula cells (0.0%)"
    payload = report.to_dict()
    assert payload["conversion_rate"] == 0.0
