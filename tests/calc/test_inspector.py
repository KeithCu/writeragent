# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for CellInspector unified cell classification (no soffice)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from plugin.calc.calc_utils import FORMULA
from plugin.calc.inspector import CellInspector


def test_range_read_types_cells_by_formula_query():
    cell_range = MagicMock()
    cell_range.getRangeAddress.return_value = SimpleNamespace(
        StartColumn=0, EndColumn=2, StartRow=0, EndRow=0
    )
    cell_range.getDataArray.return_value = ((None, "=abc", 5.0),)
    cell_range.getFormulaArray.return_value = (("=1/0", "=abc", "{=B9:B10}"),)
    formula_addrs = (
        SimpleNamespace(StartColumn=0, EndColumn=0, StartRow=0, EndRow=0),
        SimpleNamespace(StartColumn=2, EndColumn=2, StartRow=0, EndRow=0),
    )
    cell_range.queryContentCells.return_value.getRangeAddresses.return_value = formula_addrs

    bridge = MagicMock()
    bridge.resolve_range_or_address.return_value = cell_range

    row = CellInspector(bridge).read_range("A1:C1")[0]
    assert row[0] == {"address": "A1", "value": None, "formula": "=1/0", "type": "formula"}
    assert row[1] == {"address": "B1", "value": "=abc", "formula": None, "type": "text"}
    assert row[2] == {"address": "C1", "value": 5.0, "formula": "{=B9:B10}", "type": "formula"}
    cell_range.queryContentCells.assert_called_once_with(16)


def test_single_cell_error_formula_reads_none():
    cell = MagicMock()
    del cell.getRangeAddress
    cell.getType.return_value = FORMULA
    cell.getDataArray.return_value = ((None,),)
    cell.getFormulaArray.return_value = (("=1/0",),)

    bridge = MagicMock()
    bridge.resolve_range_or_address.return_value = cell

    res = CellInspector(bridge).read_cell("A1")
    assert res == {"address": "A1", "value": None, "formula": "=1/0", "type": "formula"}
