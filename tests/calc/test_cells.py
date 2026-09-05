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

from plugin.tests.testing_utils import TestingFactory


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
    # Type bitmask vs FormatString (observability field on temporal enrich).
    format_props.getPropertyValue.side_effect = lambda name: format_type if name == "Type" else "YYYY-MM-DD"
    formats.getByKey.return_value = format_props
    # CalcDocStub supplies getNumberFormats / getNumberFormatSettings (not a bare MagicMock doc).
    doc = TestingFactory.create_doc(
        doc_type="calc",
        number_formats=formats,
        null_date=null_date or SimpleNamespace(Year=1899, Month=12, Day=30),
    )
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
    assert result[0][0]["format_code"] == "YYYY-MM-DD"
    assert "iso8601" not in result[0][0]
    assert result[0][1]["value"] == 42.0
    assert "format_category" not in result[0][1]
    assert "format_code" not in result[0][1]
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


def _range_addr(*, start_col, end_col, start_row, end_row):
    mock_range = MagicMock()
    mock_range.getRangeAddress.return_value = MagicMock(
        StartColumn=start_col, EndColumn=end_col, StartRow=start_row, EndRow=end_row
    )
    return mock_range


def test_read_cell_range_tool_opts_into_format_info():
    from plugin.calc.cells import ReadCellRange

    ctx = SimpleNamespace(doc=MagicMock())
    with (
        patch("plugin.calc.cells.CalcBridge") as bridge_cls,
        patch("plugin.calc.cells.CellInspector") as inspector_cls,
    ):
        bridge_cls.return_value.resolve_range_or_address.return_value = _range_addr(
            start_col=0, end_col=0, start_row=0, end_row=0
        )
        inspector_cls.return_value.read_range.return_value = [[{"value": 1.0}]]
        result = ReadCellRange().execute(ctx, range=["A1"])

    assert result["status"] == "ok"
    assert not result.get("truncated")
    inspector_cls.return_value.read_range.assert_called_once_with("A1", include_format_info=True)


def test_read_cell_range_tool_truncates_large_range():
    """A1:H500 must not dump thousands of cell dicts into chat (issue 405)."""
    from plugin.calc.cells import ReadCellRange, _READ_CELL_RANGE_TRUNCATED_MSG

    ctx = SimpleNamespace(doc=MagicMock())
    sample = [[{"value": "OrderID"}] * 8]
    with (
        patch("plugin.calc.cells.CalcBridge") as bridge_cls,
        patch("plugin.calc.cells.CellInspector") as inspector_cls,
    ):
        # A1:H500 → 8 cols × 500 rows
        bridge_cls.return_value.resolve_range_or_address.return_value = _range_addr(
            start_col=0, end_col=7, start_row=0, end_row=499
        )
        inspector_cls.return_value.read_range.return_value = sample
        result = ReadCellRange().execute(ctx, range=["A1:H500"])

    assert result["status"] == "ok"
    assert result["truncated"] is True
    assert result["cells"] == 4000
    assert result["rows"] == 500
    assert result["columns"] == 8
    assert result["preview_range"] == "A1:H10"
    assert result["message"] == _READ_CELL_RANGE_TRUNCATED_MSG
    assert "overload" in result["message"]
    inspector_cls.return_value.read_range.assert_called_once_with("A1:H10", include_format_info=True)


def test_read_cell_range_tool_keeps_small_range_full():
    from plugin.calc.cells import ReadCellRange, _READ_CELL_RANGE_MAX_CELLS

    ctx = SimpleNamespace(doc=MagicMock())
    with (
        patch("plugin.calc.cells.CalcBridge") as bridge_cls,
        patch("plugin.calc.cells.CellInspector") as inspector_cls,
    ):
        # Exactly the cap (8×10) still returns the full grid.
        bridge_cls.return_value.resolve_range_or_address.return_value = _range_addr(
            start_col=0, end_col=7, start_row=0, end_row=9
        )
        inspector_cls.return_value.read_range.return_value = [[{"value": 1}]]
        result = ReadCellRange().execute(ctx, range=["A1:H10"])

    assert _READ_CELL_RANGE_MAX_CELLS == 80
    assert result["status"] == "ok"
    assert not result.get("truncated")
    inspector_cls.return_value.read_range.assert_called_once_with("A1:H10", include_format_info=True)


def test_preview_if_large_keeps_sheet_prefix():
    from plugin.calc.cells import _preview_if_large

    bridge = MagicMock()
    bridge.resolve_range_or_address.return_value = _range_addr(
        start_col=0, end_col=7, start_row=0, end_row=499
    )
    preview = _preview_if_large(bridge, "'Data Sheet'!A1:H500")
    assert preview is not None
    assert preview["preview_range"] == "'Data Sheet'!A1:H10"


def test_set_style_rejects_mistyped_bold():
    """Sloppy LLM bool strings must error, not silently no-op as success."""
    from plugin.calc.cells import SetCellStyle

    ctx = SimpleNamespace(doc=MagicMock())
    with patch("plugin.calc.cells.CellManipulator") as manip_cls:
        result = SetCellStyle().execute(ctx, range=["A1"], bold="true")

    assert result["status"] == "error"
    assert "bold" in result["message"]
    manip_cls.return_value.set_cell_style.assert_not_called()


def test_set_style_rejects_mistyped_font_size():
    from plugin.calc.cells import SetCellStyle

    ctx = SimpleNamespace(doc=MagicMock())
    with patch("plugin.calc.cells.CellManipulator") as manip_cls:
        result = SetCellStyle().execute(ctx, range=["A1"], font_size="12")

    assert result["status"] == "error"
    assert "font_size" in result["message"]
    manip_cls.return_value.set_cell_style.assert_not_called()


def test_set_style_accepts_typed_bold_and_font_size():
    from plugin.calc.cells import SetCellStyle

    ctx = SimpleNamespace(doc=MagicMock())
    with patch("plugin.calc.cells.CellManipulator") as manip_cls:
        result = SetCellStyle().execute(ctx, range=["A1"], bold=True, font_size=12)

    assert result["status"] == "ok"
    manip_cls.return_value.set_cell_style.assert_called_once()
    kwargs = manip_cls.return_value.set_cell_style.call_args.kwargs
    assert kwargs["bold"] is True
    assert kwargs["font_size"] == 12.0


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

    doc = TestingFactory.create_doc(
        doc_type="calc",
        number_formats=formats,
        props={"CharLocale": SimpleNamespace(Language="en", Country="US", Variant="")},
    )

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

    doc = TestingFactory.create_doc(
        doc_type="calc",
        number_formats=formats,
        props={"CharLocale": SimpleNamespace(Language="en", Country="US", Variant="")},
    )

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


def test_make_number_formatter_unwraps_guarded_doc():
    """attachNumberFormatsSupplier must get the raw doc, not a Layer-A proxy.

    Release OXT stubs omit ``_UnoThreadGuardProxy``; patch ``_unwrap_uno`` so the
    test exercises the call site under both full and stub thread_guard modules.
    """
    from plugin.calc.manipulator import CellManipulator

    raw_doc = MagicMock(name="raw_doc")
    proxied_doc = object()
    raw_ctx = MagicMock(name="raw_ctx")
    proxied_ctx = object()
    smgr = MagicMock()
    formatter = MagicMock()
    raw_ctx.getServiceManager.return_value = smgr
    smgr.createInstanceWithContext.return_value = formatter

    def fake_unwrap(obj):
        if obj is proxied_doc:
            return raw_doc
        if obj is proxied_ctx:
            return raw_ctx
        return obj

    manip = CellManipulator(MagicMock())
    with patch("plugin.calc.manipulator.get_ctx", return_value=proxied_ctx):
        with patch("plugin.framework.thread_guard._unwrap_uno", side_effect=fake_unwrap):
            out = manip._make_number_formatter(proxied_doc)

    assert out is formatter
    formatter.attachNumberFormatsSupplier.assert_called_once_with(raw_doc)
    smgr.createInstanceWithContext.assert_called_once_with("com.sun.star.util.NumberFormatter", raw_ctx)


def test_write_formula_range_empty_uno_error_uses_type_name():
    """Blank UNO str(e) must not surface as an empty CalcError."""
    from plugin.calc.manipulator import CellManipulator
    from plugin.calc import CalcError

    class _BlankUno(Exception):
        def __str__(self):
            return ""

    bridge = MagicMock()
    bridge.resolve_range_or_address.side_effect = _BlankUno()
    manip = CellManipulator(bridge)
    try:
        manip.write_formula_range("A1", "2026-08-08")
        raise AssertionError("expected CalcError")
    except CalcError as e:
        assert str(e) == "_BlankUno"


def test_write_cell_range_tool_error_return():
    """WriteCellRange.execute returns _tool_error when manipulator fails."""
    from plugin.calc.cells import WriteCellRange

    class _BlankUno(Exception):
        def __str__(self):
            return "UNO write failure"

    doc = MagicMock()
    ctx = MagicMock()
    ctx.doc = doc

    bridge = MagicMock()
    bridge.resolve_range_or_address.side_effect = _BlankUno()

    tool = WriteCellRange()
    with patch("plugin.calc.cells.CalcBridge", return_value=bridge):
        res = tool.execute(ctx, range=["A1"], values="123")
    assert res["status"] == "error"
    assert "UNO write failure" in res["message"]


def test_json_array_value_count_and_a1_shape():
    from plugin.calc.cells import _a1_range_shape, _json_array_value_count

    assert _json_array_value_count('["a", "b", "c", "d"]') == 4
    assert _json_array_value_count(["a", "b", "c", "d"]) == 4
    assert _json_array_value_count([["a"], ["b"], ["c"], ["d"]]) == 4
    assert _json_array_value_count("=B2+1") is None
    assert _json_array_value_count("") is None
    assert _json_array_value_count("[]") is None
    assert _a1_range_shape("C2:C9") == (8, 8, 1)
    assert _a1_range_shape("Sheet1.C2:C9") == (8, 8, 1)
    assert _a1_range_shape("A1:B1") == (2, 1, 2)
    assert _a1_range_shape("SalesData") is None


def test_write_formula_range_rejects_json_length_mismatch():
    """4 values into an 8-cell column must fail loud — no silent zip/pad."""
    from plugin.calc.cells import WriteCellRange

    ctx = SimpleNamespace(doc=MagicMock())
    with (
        patch("plugin.calc.cells.CalcBridge"),
        patch("plugin.calc.cells.CellManipulator") as manip_cls,
    ):
        result = WriteCellRange().execute(
            ctx, range=["C2:C9"], values='["a", "b", "c", "d"]'
        )

    assert result["status"] == "error"
    assert "4 values" in result["message"]
    assert "8 cells" in result["message"]
    assert "one value per row" in result["message"]
    manip_cls.return_value.write_formula_range.assert_not_called()


def test_write_formula_range_rejects_list_length_mismatch():
    from plugin.calc.cells import WriteCellRange

    ctx = SimpleNamespace(doc=MagicMock())
    with (
        patch("plugin.calc.cells.CalcBridge"),
        patch("plugin.calc.cells.CellManipulator") as manip_cls,
    ):
        result = WriteCellRange().execute(
            ctx, range=["A1:B4"], values=["a", "b", "c", "d"]
        )

    assert result["status"] == "error"
    assert "4 values" in result["message"]
    assert "8 cells" in result["message"]
    manip_cls.return_value.write_formula_range.assert_not_called()


def test_write_formula_range_accepts_matching_json_and_scalar_fill():
    from plugin.calc.cells import WriteCellRange

    ctx = SimpleNamespace(doc=MagicMock())
    with (
        patch("plugin.calc.cells.CalcBridge"),
        patch("plugin.calc.cells.CellManipulator") as manip_cls,
        patch("plugin.writer.edit_review.WriterCompoundUndo"),
    ):
        manip_cls.return_value.write_formula_range.return_value = "wrote"
        matched = WriteCellRange().execute(ctx, range=["A1:A2"], values='["x", "y"]')
        filled = WriteCellRange().execute(ctx, range=["A1:A8"], values="=B2+1")
        cleared = WriteCellRange().execute(ctx, range=["A1:A8"], values="")

    assert matched["status"] == "ok"
    assert filled["status"] == "ok"
    assert cleared["status"] == "ok"
    assert manip_cls.return_value.write_formula_range.call_count == 3
