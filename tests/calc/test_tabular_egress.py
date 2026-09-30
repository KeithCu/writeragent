# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for shared Calc tabular egress."""

from __future__ import annotations

from plugin.calc.tabular_egress import format_tabular_helper_for_calc


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
