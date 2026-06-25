# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
# Copyright (c) 2026 LibreCalc AI Assistant (Calc integration features, originally MIT)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

import unittest


def test_cells_parse_color():
    from plugin.calc.cells import _parse_color
    assert _parse_color("red") == 0xFF0000
    assert _parse_color("RED") == 0xFF0000
    assert _parse_color("#00FF00") == 0x00FF00
    assert _parse_color("#000") == 0x000000
    assert _parse_color("invalid") is None


def test_inspector_single_cell_range_fallback():
    from unittest.mock import MagicMock
    from plugin.calc.inspector import CellInspector
    
    bridge = MagicMock()
    mock_range = MagicMock()
    mock_range.getRangeAddress.return_value = MagicMock(StartColumn=1, EndColumn=1, StartRow=2, EndRow=2)
    
    if hasattr(mock_range, "getType"):
        delattr(mock_range, "getType")
        
    mock_cell = MagicMock()
    mock_cell.getType.return_value = 1 # VALUE
    mock_cell.getValue.return_value = 42.0
    mock_cell.getFormula.return_value = "=42"
    
    mock_range.getCellByPosition.return_value = mock_cell
    bridge.resolve_range_or_address.return_value = mock_range
    
    inspector = CellInspector(bridge)
    res = inspector.read_cell("B3")
    assert res["value"] == 42.0
    bridge.resolve_range_or_address.assert_called_with("B3")
    mock_range.getCellByPosition.assert_called_with(0, 0)

