# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
# Copyright (c) 2026 LibreCalc AI Assistant (Calc integration features, originally MIT)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

from types import SimpleNamespace
from unittest.mock import MagicMock, patch


def test_cells_parse_color():
    from plugin.calc.cells import _parse_color
    assert _parse_color("red") == 0xFF0000
    assert _parse_color("RED") == 0xFF0000
    assert _parse_color("#00FF00") == 0x00FF00
    assert _parse_color("#000") == 0x000000
    assert _parse_color("invalid") is None
    assert _parse_color(2) is None
    assert _parse_color(None) is None
    assert _parse_color("") is None


def test_inspector_single_cell_range_fallback():
    from plugin.calc.inspector import CellInspector

    bridge = MagicMock()
    mock_range = MagicMock()
    mock_range.getRangeAddress.return_value = MagicMock(StartColumn=1, EndColumn=1, StartRow=2, EndRow=2)

    if hasattr(mock_range, "getType"):
        delattr(mock_range, "getType")

    mock_cell = MagicMock()
    mock_cell.getType.return_value = 1  # VALUE
    mock_cell.getValue.return_value = 42.0
    mock_cell.getFormula.return_value = "=42"

    mock_range.getCellByPosition.return_value = mock_cell
    bridge.resolve_range_or_address.return_value = mock_range

    inspector = CellInspector(bridge)
    res = inspector.read_cell("B3")
    assert res["value"] == 42.0
    bridge.resolve_range_or_address.assert_called_with("B3")
    mock_range.getCellByPosition.assert_called_with(0, 0)


def test_calc_serial_iso8601_uses_document_null_date():
    from plugin.calc.inspector import _format_category_from_type, _iso8601_from_serial

    null_date = SimpleNamespace(Year=1899, Month=12, Day=30)
    assert _iso8601_from_serial(46237.0, "date", null_date) == "2026-08-03"
    assert _iso8601_from_serial(46237.5, "datetime", null_date) == "2026-08-03T12:00:00"
    assert _iso8601_from_serial(0.5, "time", null_date) == "12:00:00"
    assert _format_category_from_type(2) == "date"
    assert _format_category_from_type(5) == "time"  # DEFINED | TIME
    assert _format_category_from_type(7) == "datetime"  # DEFINED | DATETIME
    assert _format_category_from_type(16) is None


def test_calc_serial_iso8601_rounds_float_noise_to_whole_seconds():
    from plugin.calc.inspector import _iso8601_from_serial

    null_date = SimpleNamespace(Year=1899, Month=12, Day=30)
    # 0.6s past noon rounds up to 12:00:01 (use a small serial so float has room).
    assert _iso8601_from_serial(0.5 + 0.6 / 86400.0, "time", null_date) == "12:00:01"
    assert _iso8601_from_serial(0.5 + 0.6 / 86400.0, "datetime", null_date) == "1899-12-30T12:00:01"
    # Sub-second float dust must not leak microseconds into ISO.
    dusty = 0.5 + 1e-12
    assert _iso8601_from_serial(dusty, "time", null_date) == "12:00:00"
    assert "." not in _iso8601_from_serial(dusty, "datetime", null_date).split("T", 1)[1]


def test_inspector_default_range_read_does_not_query_formats():
    from plugin.calc.inspector import CellInspector

    addr = SimpleNamespace(StartColumn=0, EndColumn=1, StartRow=0, EndRow=0)
    cell_range = MagicMock()
    cell_range.getRangeAddress.return_value = addr
    cell_range.getDataArray.return_value = ((1.0, 2.0),)
    cell_range.getFormulaArray.return_value = (("1", "2"),)
    bridge = MagicMock()
    bridge.resolve_range_or_address.return_value = cell_range
    bridge._index_to_column.side_effect = ("A", "B")

    result = CellInspector(bridge).read_range("A1:B1")

    assert [cell["value"] for cell in result[0]] == [1.0, 2.0]
    cell_range.queryContentCells.assert_not_called()
    cell_range.getUniqueCellFormatRanges.assert_not_called()
    bridge.get_active_document.assert_not_called()


def _make_range_bridge(*, data_array, formula_array, date_addresses=(), format_groups=None, null_date=None, format_type=2, format_key=10):
    """Build a mocked bridge/range for include_format_info reads."""
    rows = len(data_array)
    cols = len(data_array[0]) if rows else 0
    addr = SimpleNamespace(StartColumn=0, EndColumn=max(cols - 1, 0), StartRow=0, EndRow=max(rows - 1, 0))
    cell_range = MagicMock()
    cell_range.getRangeAddress.return_value = addr
    cell_range.getDataArray.return_value = data_array
    cell_range.getFormulaArray.return_value = formula_array
    cell_range.queryContentCells.return_value.getRangeAddresses.return_value = tuple(date_addresses)

    if format_groups is None:
        format_groups = MagicMock()
        format_groups.getCount.return_value = 0
    cell_range.getUniqueCellFormatRanges.return_value = format_groups

    formats = MagicMock()
    format_props = MagicMock()
    format_props.getPropertyValue.return_value = format_type
    formats.getByKey.return_value = format_props
    doc = MagicMock()
    doc.getNumberFormats.return_value = formats
    doc.getNumberFormatSettings.return_value.getPropertyValue.return_value = null_date or SimpleNamespace(Year=1899, Month=12, Day=30)
    bridge = MagicMock()
    bridge.resolve_range_or_address.return_value = cell_range
    bridge.get_active_document.return_value = doc
    letters = tuple(chr(ord("A") + i) for i in range(max(cols, 1)))
    bridge._index_to_column.side_effect = letters
    return bridge, cell_range, formats


def test_inspector_enriches_range_once_per_unique_format_group():
    from plugin.calc.inspector import CellInspector

    date_addr = SimpleNamespace(StartColumn=0, EndColumn=0, StartRow=0, EndRow=0)
    representative = MagicMock()
    representative.getPropertyValue.return_value = 10
    date_group = MagicMock()
    date_group.getCount.return_value = 1
    date_group.getByIndex.return_value = representative
    date_group.getRangeAddresses.return_value = (date_addr,)
    format_groups = MagicMock()
    format_groups.getCount.return_value = 1
    format_groups.getByIndex.return_value = date_group

    bridge, cell_range, formats = _make_range_bridge(
        data_array=((46237.0, 42.0),),
        formula_array=(("46237", "42"),),
        date_addresses=(date_addr,),
        format_groups=format_groups,
    )

    result = CellInspector(bridge).read_range("A1:B1", include_format_info=True)

    assert result[0][0]["value"] == "2026-08-03"
    assert result[0][0]["type"] == "date"
    assert result[0][0]["format_category"] == "date"
    assert "iso8601" not in result[0][0]
    assert result[0][1]["value"] == 42.0
    assert "format_category" not in result[0][1]
    formats.getByKey.assert_called_once_with(10)
    # Date constants present: skip the formula walk and go straight to format groups.
    cell_range.getUniqueCellFormatRanges.assert_called_once()


def test_inspector_format_info_skips_format_groups_when_no_dates_or_formulas():
    from plugin.calc.inspector import CellInspector

    bridge, cell_range, _formats = _make_range_bridge(
        data_array=((1.0, 2.0),),
        formula_array=(("1", "2"),),
        date_addresses=(),
    )

    result = CellInspector(bridge).read_range("A1:B1", include_format_info=True)

    assert [cell["value"] for cell in result[0]] == [1.0, 2.0]
    cell_range.queryContentCells.assert_called_once()
    cell_range.getUniqueCellFormatRanges.assert_not_called()


def test_inspector_enriches_elapsed_format_as_duration():
    """Elapsed [HH]:MM:SS → PT30H wire, not clock 06:00:00."""
    from plugin.calc.inspector import CellInspector

    date_addr = SimpleNamespace(StartColumn=0, EndColumn=0, StartRow=0, EndRow=0)
    representative = MagicMock()
    representative.getPropertyValue.return_value = 43
    elapsed_group = MagicMock()
    elapsed_group.getCount.return_value = 1
    elapsed_group.getByIndex.return_value = representative
    elapsed_group.getRangeAddresses.return_value = (date_addr,)
    format_groups = MagicMock()
    format_groups.getCount.return_value = 1
    format_groups.getByIndex.return_value = elapsed_group

    # Two columns so read_range takes the batch path (single-cell uses read_cell).
    bridge, cell_range, formats = _make_range_bridge(
        data_array=((1.25, 42.0),),
        formula_array=(("1.25", "42"),),
        date_addresses=(date_addr,),
        format_groups=format_groups,
        format_type=4,  # TIME
    )
    format_props = formats.getByKey.return_value
    format_props.getPropertyValue.side_effect = lambda name: 4 if name == "Type" else "[HH]:MM:SS"

    result = CellInspector(bridge).read_range("A1:B1", include_format_info=True)

    assert result[0][0]["value"] == "PT30H"
    assert result[0][0]["type"] == "duration"
    assert result[0][0]["format_category"] == "duration"
    assert result[0][1]["value"] == 42.0


def test_inspector_format_info_uses_format_groups_for_formula_only_ranges():
    from plugin.calc.inspector import CellInspector

    formula_addr = SimpleNamespace(StartColumn=0, EndColumn=0, StartRow=0, EndRow=0)
    representative = MagicMock()
    representative.getPropertyValue.return_value = 11
    date_group = MagicMock()
    date_group.getCount.return_value = 1
    date_group.getByIndex.return_value = representative
    date_group.getRangeAddresses.return_value = (formula_addr,)
    format_groups = MagicMock()
    format_groups.getCount.return_value = 1
    format_groups.getByIndex.return_value = date_group

    # Formulas are not DATETIME content cells, so the preflight is empty and we must
    # fall through via the formula scan before consulting format groups.
    bridge, cell_range, formats = _make_range_bridge(
        data_array=((46237.0, 1.0),),
        formula_array=(("=TODAY()", "1"),),
        date_addresses=(),
        format_groups=format_groups,
    )
    result = CellInspector(bridge).read_range("A1:B1", include_format_info=True)

    assert result[0][0]["value"] == "2026-08-03"
    assert result[0][0]["type"] == "date"
    assert result[0][0]["format_category"] == "date"
    cell_range.queryContentCells.assert_called_once()
    cell_range.getUniqueCellFormatRanges.assert_called_once()
    formats.getByKey.assert_called_once_with(11)


def test_inspector_format_info_survives_queryContentCells_failure():
    from plugin.calc.inspector import CellInspector

    formula_addr = SimpleNamespace(StartColumn=0, EndColumn=0, StartRow=0, EndRow=0)
    representative = MagicMock()
    representative.getPropertyValue.return_value = 11
    date_group = MagicMock()
    date_group.getCount.return_value = 1
    date_group.getByIndex.return_value = representative
    date_group.getRangeAddresses.return_value = (formula_addr,)
    format_groups = MagicMock()
    format_groups.getCount.return_value = 1
    format_groups.getByIndex.return_value = date_group

    bridge, cell_range, formats = _make_range_bridge(
        data_array=((46237.0, 1.0),),
        formula_array=(("=TODAY()", "1"),),
        date_addresses=(),
        format_groups=format_groups,
    )
    cell_range.queryContentCells.side_effect = RuntimeError("UNO bridge glitch")

    result = CellInspector(bridge).read_range("A1:B1", include_format_info=True)

    assert result[0][0]["value"] == "2026-08-03"
    assert result[0][0]["type"] == "date"
    cell_range.getUniqueCellFormatRanges.assert_called_once()
    formats.getByKey.assert_called_once_with(11)


def test_read_cell_range_tool_opts_into_format_info():
    from plugin.calc.cells import ReadCellRange

    ctx = SimpleNamespace(doc=MagicMock())
    with patch("plugin.calc.cells.CellInspector") as inspector_cls:
        inspector_cls.return_value.read_range.return_value = [[{"value": 1.0}]]
        result = ReadCellRange().execute(ctx, range_name=["A1"])

    assert result["status"] == "ok"
    inspector_cls.return_value.read_range.assert_called_once_with("A1", include_format_info=True)


def test_write_formula_range_s30_format_pass_warning():
    """S30: format-pass failure keeps value-commit success and warns in the message."""
    from plugin.calc.manipulator import CellManipulator

    addr = SimpleNamespace(StartColumn=0, EndColumn=0, StartRow=0, EndRow=0)
    cell_range = MagicMock()
    cell_range.getRangeAddress.return_value = addr
    sheet = MagicMock()
    cell_range.getSpreadsheet.return_value = sheet
    sheet.getCellRangeByPosition.return_value = cell_range

    cell = MagicMock()
    cell.getPropertyValue.return_value = 0  # General
    sheet.getCellByPosition.return_value = cell

    formats = MagicMock()
    formats.getStandardIndex.return_value = 1
    format_props = MagicMock()
    format_props.getPropertyValue.return_value = 0  # non-temporal Type
    formats.getByKey.return_value = format_props

    doc = MagicMock()
    doc.getNumberFormats.return_value = formats
    doc.getPropertyValue.return_value = SimpleNamespace(Language="en", Country="US", Variant="")

    bridge = MagicMock()
    bridge.resolve_range_or_address.return_value = cell_range
    bridge.get_active_document.return_value = doc

    formatter = MagicMock()
    formatter.detectNumberFormat.return_value = 37
    formatter.convertStringToNumber.return_value = 46242.0

    manip = CellManipulator(bridge)
    with patch.object(manip, "_make_number_formatter", return_value=formatter):
        with patch.object(manip, "_apply_temporal_format_runs", side_effect=RuntimeError("format boom")):
            msg = manip.write_formula_range("A1", "2026-08-08")

    assert "Range A1 filled with 1 value (1 date)" in msg
    assert "could not apply date/time formats to 1 cells in A1" in msg
    cell_range.setDataArray.assert_called_once()


def test_write_formula_range_s30_warning_counts_apply_only():
    """S30: warning counts M1 apply cells, not preserve-only temporals."""
    from plugin.calc.manipulator import CellManipulator

    addr = SimpleNamespace(StartColumn=0, EndColumn=1, StartRow=0, EndRow=0)
    cell_range = MagicMock()
    cell_range.getRangeAddress.return_value = addr
    sheet = MagicMock()
    cell_range.getSpreadsheet.return_value = sheet
    sheet.getCellRangeByPosition.return_value = cell_range

    general_cell = MagicMock()
    general_cell.getPropertyValue.return_value = 0  # General key
    date_cell = MagicMock()
    date_cell.getPropertyValue.return_value = 37  # existing date key
    sheet.getCellByPosition.side_effect = lambda col, row: general_cell if col == 0 else date_cell

    formats = MagicMock()
    formats.getStandardIndex.return_value = 1

    def _props_for_key(key):
        props = MagicMock()
        # Type 0 = non-temporal; Type 2 = DATE → preserve for date input
        props.getPropertyValue.return_value = 0 if int(key) == 0 else 2
        return props

    formats.getByKey.side_effect = _props_for_key

    doc = MagicMock()
    doc.getNumberFormats.return_value = formats
    doc.getPropertyValue.return_value = SimpleNamespace(Language="en", Country="US", Variant="")

    bridge = MagicMock()
    bridge.resolve_range_or_address.return_value = cell_range
    bridge.get_active_document.return_value = doc

    formatter = MagicMock()
    formatter.detectNumberFormat.return_value = 37
    formatter.convertStringToNumber.return_value = 46242.0

    manip = CellManipulator(bridge)
    with patch.object(manip, "_make_number_formatter", return_value=formatter):
        with patch.object(manip, "_apply_temporal_format_runs", side_effect=RuntimeError("format boom")):
            msg = manip.write_formula_range("A1:B1", '["2026-08-08", "2026-08-09"]')

    assert "2 dates" in msg
    assert "could not apply date/time formats to 1 cells in A1:B1" in msg
    assert "to 2 cells" not in msg


def test_apply_temporal_format_runs_vertically_merges_homogeneous_column():
    """Homogeneous apply column → one getCellRangeByPosition covering all rows."""
    from plugin.calc.manipulator import CellManipulator

    sheet = MagicMock()
    target = MagicMock()
    sheet.getCellRangeByPosition.return_value = target
    manip = CellManipulator(MagicMock())
    apply = ("apply", 42)
    decisions = [[apply], [apply], [apply]]

    applied = manip._apply_temporal_format_runs(sheet, start=(0, 10), decisions=decisions)

    assert applied == 3
    sheet.getCellRangeByPosition.assert_called_once_with(0, 10, 0, 12)
    target.setPropertyValue.assert_called_once_with("NumberFormat", 42)
