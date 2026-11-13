# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""UNO tests for CellInspector single-cell vs range read contract parity."""

from plugin.calc.bridge import CalcBridge
from plugin.calc.inspector import CellInspector
from plugin.testing_runner import native_test
from plugin.tests.testing_utils import with_native_doc


@native_test
@with_native_doc("calc")
def test_inspector_single_and_range_agree(ctx, doc):
    sheet = doc.getCurrentController().getActiveSheet()
    sheet.getCellByPosition(2, 2).setFormula("=1/0")  # C3
    sheet.getCellByPosition(2, 3).setString("=abc")  # C4
    sheet.getCellByPosition(2, 4).setFormula('=""')  # C5
    sheet.getCellByPosition(4, 2).setValue(5)  # E3
    sheet.getCellByPosition(4, 3).setString("x")  # E4
    sheet.getCellRangeByPosition(3, 2, 3, 3).setArrayFormula("=E3:E4")  # D3:D4
    sheet.getCellByPosition(3, 4).setValue(7)  # D5
    doc.calculateAll()

    inspector = CellInspector(CalcBridge(doc))
    grid = inspector.read_range("C3:D5")
    by_addr = {cell["address"]: cell for row in grid for cell in row}

    for cell in by_addr.values():
        assert cell == inspector.read_cell(cell["address"])

    assert by_addr["C3"] == {"address": "C3", "value": None, "formula": "=1/0", "type": "formula"}
    assert by_addr["C4"] == {"address": "C4", "value": "=abc", "formula": None, "type": "text"}
    assert by_addr["C5"] == {"address": "C5", "value": "", "formula": '=""', "type": "formula"}
    assert by_addr["D3"] == {"address": "D3", "value": 5.0, "formula": "{=E3:E4}", "type": "formula"}
    assert by_addr["D4"]["value"] == "x"
    assert by_addr["D4"]["type"] == "formula"
    assert by_addr["D5"] == {"address": "D5", "value": 7.0, "formula": None, "type": "value"}
