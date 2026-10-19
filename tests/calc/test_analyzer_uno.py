# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
# Copyright (c) 2026 LibreCalc AI Assistant (Calc integration features, originally MIT)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import with_native_doc


@native_test
@with_native_doc("calc")
def test_analyzer_get_sheet_summary(ctx, doc):
    from plugin.calc.bridge import CalcBridge
    from plugin.calc.analyzer import SheetAnalyzer

    active_sheet = doc.getCurrentController().getActiveSheet()
    bridge = CalcBridge(doc)
    analyzer = SheetAnalyzer(bridge)
    active_sheet.getCellByPosition(0, 0).setString("Header")
    active_sheet.getCellByPosition(1, 0).setValue(2024)
    active_sheet.getCellByPosition(0, 5).setString("TestEnd")
    summary = analyzer.get_sheet_summary()

    assert summary.get("sheet_name") == active_sheet.getName(), "Sheet name mismatch"
    assert (summary.get("row_count") or 0) >= 6, f"Row count mismatch: {summary}"
    assert "2024" in summary.get("headers", []), f"Header 2024 not found in: {summary}"

