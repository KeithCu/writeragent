# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""UNO verification on a corpus-style income statement sheet."""

from __future__ import annotations

from pathlib import Path

from plugin.framework.uno_context import get_desktop
from plugin.testing_runner import native_test

_CORPUS_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "spreadsheet_import_corpus"


@native_test
def test_corpus_income_statement_verify(ctx):
    import uno

    desktop = get_desktop(ctx)
    path = _CORPUS_DIR / "3 Statement Model.xlsx"
    if not path.is_file():
        return

    hidden_prop = uno.createUnoStruct(
        "com.sun.star.beans.PropertyValue",
        Name="Hidden",
        Value=True,
    )
    file_url = uno.systemPathToFileUrl(str(path.resolve()))
    doc = desktop.loadComponentFromURL(file_url, "_blank", 0, (hidden_prop,))
    if doc is None:
        return

    try:
        from plugin.calc.spreadsheet_import.import_dialog import run_sheet_conversion

        sheets = doc.getSheets()
        if not sheets.hasByName("Income Statement"):
            return
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
            return
        pass_rate = (len(converted) - len(failed)) / len(converted)
        assert pass_rate >= 0.90, f"verify pass rate {pass_rate:.1%}: {failed[:5]}"
    finally:
        doc.close(True)
