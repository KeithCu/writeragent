# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""UNO verification on a corpus-style income statement sheet."""

from __future__ import annotations

import unittest
from pathlib import Path

from plugin.framework.uno_context import get_desktop
from plugin.testing_runner import native_test

_CORPUS_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "spreadsheet_import_corpus"


@native_test
def test_corpus_income_statement_verify(ctx):
    import uno

    desktop = get_desktop(ctx)
    path = _CORPUS_DIR / "3 Statement Model.xlsx"
    assert path.is_file(), f"Missing corpus fixture: {path}"

    hidden_prop = uno.createUnoStruct(
        "com.sun.star.beans.PropertyValue",
        Name="Hidden",
        Value=True,
    )
    file_url = uno.systemPathToFileUrl(str(path.resolve()))
    doc = desktop.loadComponentFromURL(file_url, "_blank", 0, (hidden_prop,))
    assert doc is not None, f"Failed to load corpus workbook: {path}"

    try:
        from plugin.calc.spreadsheet_import.pipeline import run_sheet_conversion

        sheets = doc.getSheets()
        assert sheets.hasByName("Income Statement"), f"Missing 'Income Statement' sheet in {path}"
        sheet = sheets.getByName("Income Statement")
        res = run_sheet_conversion(
            doc,
            sheet,
            scope="sheet",
            output_mode="new_sheet",
            vectorize=True,
            verify=True,
        )
        report = res.get("report", {})
        converted = report.get("converted", [])
        failed = res.get("failed_verifications", [])
        if not converted:
            raise unittest.SkipTest(f"No formulas converted in {path} (skipped={len(report.get('skipped', []))})")
        pass_rate = (len(converted) - len(failed)) / len(converted)
        assert pass_rate >= 0.90, f"verify pass rate {pass_rate:.1%}: {failed[:5]}"
    finally:
        doc.close(True)
