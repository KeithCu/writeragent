# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
# Copyright (c) 2026 LibreCalc AI Assistant (Calc integration features, originally MIT)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch
from xml.etree import ElementTree as ET

import pytest

from tests.harness.strip_bundle import requires_asserts

from plugin.app import get_tools
from plugin.framework.tool import prepare_tool_call
from plugin.tests.testing_utils import FakeUndoManager, TestingFactory


def _tool(name: str) -> Any:
    return get_tools().get(name)


def _only_entry(res: dict[str, Any]) -> dict[str, Any]:
    assert res["status"] == "ok"
    (entry,) = res["result"]
    return entry


def _a1_bridge(named: dict[str, str] | None = None) -> MagicMock:
    """Mock CalcBridge whose resolve_range_or_address returns a range with an A1 address."""
    from plugin.calc.address_utils import parse_range_string, split_sheet_prefix

    bridge = MagicMock()

    def _resolve(ref: str) -> MagicMock:
        target = (named or {}).get(ref, ref)
        _sheet, addr = split_sheet_prefix(target)
        parsed = parse_range_string(addr)
        if parsed is None:
            raise ValueError(f"Unknown range: {ref}")
        (c0, r0), (c1, r1) = parsed
        rng = MagicMock()
        rng.getRangeAddress.return_value = SimpleNamespace(
            Sheet=0, StartColumn=c0, StartRow=r0, EndColumn=c1, EndRow=r1
        )
        return rng

    bridge.resolve_range_or_address.side_effect = _resolve
    return bridge

# Smallest AFC-style repro for the chat cap: 9×9 = 81 cells (cap is 80).
# Full Population (1517×8) is not required; this ODS is a few KB of dummy numbers.
_MIN_RANGE_TOO_LARGE_ODS = (
    Path(__file__).resolve().parents[2]
    / "docs"
    / "eval"
    / "eval-2"
    / "afc-sample-83d10b06"
    / "fixtures"
    / "min-range-too-large.ods"
)
_TABLE_NS = "urn:oasis:names:tc:opendocument:xmlns:table:1.0"
_TABLE_ROW = f"{{{_TABLE_NS}}}table-row"
_TABLE_CELL = f"{{{_TABLE_NS}}}table-cell"
_COVERED_CELL = f"{{{_TABLE_NS}}}covered-table-cell"
_NCOLS_REP = f"{{{_TABLE_NS}}}number-columns-repeated"
_NROWS_REP = f"{{{_TABLE_NS}}}number-rows-repeated"


def _used_shape_from_ods(path: Path) -> tuple[int, int]:
    """``(rows, cols)`` of the first table in a tiny ODS. No LibreOffice."""
    with zipfile.ZipFile(path) as zf:
        root = ET.fromstring(zf.read("content.xml"))
    table = root.find(f".//{{{_TABLE_NS}}}table")
    assert table is not None, f"no table in {path}"
    rows = 0
    cols = 0
    for row in table.findall(_TABLE_ROW):
        row_repeat = int(row.get(_NROWS_REP, 1))
        row_cols = 0
        for cell in row:
            if cell.tag not in (_TABLE_CELL, _COVERED_CELL):
                continue
            row_cols += int(cell.get(_NCOLS_REP, 1))
        rows += row_repeat
        cols = max(cols, row_cols)
    return rows, cols


def test_cells_parse_color():
    from plugin.doc.visual_helpers import parse_color_to_uno_int

    assert parse_color_to_uno_int("red") == 0xFF0000
    assert parse_color_to_uno_int("RED") == 0xFF0000
    assert parse_color_to_uno_int("#00FF00") == 0x00FF00
    assert parse_color_to_uno_int("#000") == 0x000000
    assert parse_color_to_uno_int("invalid") is None
    assert parse_color_to_uno_int(None) is None
    assert parse_color_to_uno_int("") is None


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
    mock_cell.getDataArray.return_value = ((42.0,),)
    mock_cell.getFormulaArray.return_value = (("42",),)

    mock_range.getCellByPosition.return_value = mock_cell
    bridge.resolve_range_or_address.return_value = mock_range

    inspector = CellInspector(bridge)
    res = inspector.read_cell("B3")
    assert res["value"] == 42.0
    bridge.resolve_range_or_address.assert_called_with("B3")
    mock_range.getCellByPosition.assert_called_with(0, 0)


def test_inspector_formula_string_zero_stays_string():
    from plugin.calc.inspector import CellInspector

    bridge = MagicMock()
    mock_cell = MagicMock()
    del mock_cell.getRangeAddress
    mock_cell.getType.return_value = 3  # FORMULA
    mock_cell.getError.return_value = 0
    mock_cell.FormulaResultType = 2  # STRING (not VALUE)
    mock_cell.getString.return_value = "0"
    mock_cell.getValue.return_value = 0.0
    mock_cell.getFormula.return_value = '="0"'
    mock_cell.getDataArray.return_value = (("0",),)
    mock_cell.getFormulaArray.return_value = (('="0"',),)

    bridge.resolve_range_or_address.return_value = mock_cell

    inspector = CellInspector(bridge)
    res = inspector.read_cell("A1")
    assert res["value"] == "0"
    assert isinstance(res["value"], str)
    assert res["address"] == "A1"


def test_inspector_read_cell_resolved_a1_address():
    from plugin.calc.inspector import CellInspector

    bridge = MagicMock()
    cell = MagicMock()
    cell.getType.return_value = 1  # VALUE
    cell.getValue.return_value = 5.0
    cell.getFormula.return_value = ""
    cell.getRangeAddress.return_value = SimpleNamespace(
        StartColumn=1, EndColumn=1, StartRow=2, EndRow=2
    )
    bridge.resolve_range_or_address.return_value = cell

    inspector = CellInspector(bridge)
    assert inspector.read_cell("MyRange")["address"] == "B3"
    assert inspector.read_cell("Sheet1.MyRange")["address"] == "B3"


def test_read_cell_range_address_stays_value_error():
    """The single-cell ValueError must not be rewrapped as ToolExecutionError."""
    from plugin.calc.inspector import CellInspector

    bridge = MagicMock()
    mock_range = MagicMock()
    mock_range.getRangeAddress.return_value = SimpleNamespace(
        StartColumn=0, EndColumn=1, StartRow=0, EndRow=0
    )
    bridge.resolve_range_or_address.return_value = mock_range
    with pytest.raises(ValueError, match="not a single cell"):
        CellInspector(bridge).read_cell("A1:B1")


def test_inspector_default_range_read_does_not_query_formats():
    from plugin.calc.inspector import CellInspector

    addr = SimpleNamespace(StartColumn=0, EndColumn=1, StartRow=0, EndRow=0)
    cell_range = MagicMock()
    cell_range.getRangeAddress.return_value = addr
    cell_range.getDataArray.return_value = ((1.0, 2.0),)
    cell_range.getFormulaArray.return_value = (("1", "2"),)
    bridge = MagicMock()
    bridge.resolve_range_or_address.return_value = cell_range

    result = CellInspector(bridge).read_range("A1:B1")

    assert [cell["value"] for cell in result[0]] == [1.0, 2.0]
    cell_range.queryContentCells.assert_not_called()
    cell_range.getUniqueCellFormatRanges.assert_not_called()


def test_inspector_read_range_refuses_over_cap_before_get_data_array():
    from plugin.calc.inspector import CellInspector
    from plugin.framework.errors import CalcError

    addr = SimpleNamespace(StartColumn=0, EndColumn=0, StartRow=0, EndRow=1048575)
    cell_range = MagicMock()
    cell_range.getRangeAddress.return_value = addr
    bridge = MagicMock()
    bridge.resolve_range_or_address.return_value = cell_range

    with pytest.raises(CalcError, match=r"Data range has 1048576 cells; maximum is"):
        CellInspector(bridge).read_range("A1:A1048576")

    cell_range.getDataArray.assert_not_called()
    cell_range.getFormulaArray.assert_not_called()


def _make_range_bridge(
    *,
    data_array,
    formula_array,
    date_addresses=(),
    formula_addresses=(),
    format_groups=None,
    null_date=None,
    format_type=2,
    format_key=10,
):
    """Build a mocked bridge/range for include_format_info reads."""
    rows = len(data_array)
    cols = len(data_array[0]) if rows else 0
    addr = SimpleNamespace(StartColumn=0, EndColumn=max(cols - 1, 0), StartRow=0, EndRow=max(rows - 1, 0))
    cell_range = MagicMock()
    cell_range.getRangeAddress.return_value = addr
    cell_range.getDataArray.return_value = data_array
    cell_range.getFormulaArray.return_value = formula_array

    def _query_content(flags):
        result = MagicMock()
        if flags == 2:
            result.getRangeAddresses.return_value = tuple(date_addresses)
        elif flags == 16:
            result.getRangeAddresses.return_value = tuple(formula_addresses)
        else:
            result.getRangeAddresses.return_value = ()
        return result

    cell_range.queryContentCells.side_effect = _query_content

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
    bridge.doc = doc
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
        formula_addresses=(formula_addr,),
        format_groups=format_groups,
    )
    result = CellInspector(bridge).read_range("A1:B1", include_format_info=True)

    assert result[0][0]["value"] == "2026-08-03"
    assert result[0][0]["type"] == "date"
    assert result[0][0]["format_category"] == "date"
    assert [c.args for c in cell_range.queryContentCells.call_args_list] == [(16,), (2,)]
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

    def _fail_datetime_only(flags):
        if flags == 2:
            raise RuntimeError("UNO bridge glitch")
        result = MagicMock()
        result.getRangeAddresses.return_value = (formula_addr,)
        return result

    cell_range.queryContentCells.side_effect = _fail_datetime_only

    result = CellInspector(bridge).read_range("A1:B1", include_format_info=True)

    assert result[0][0]["value"] == "2026-08-03"
    assert result[0][0]["type"] == "date"
    cell_range.getUniqueCellFormatRanges.assert_called_once()
    formats.getByKey.assert_called_once_with(11)


def test_inspector_temporal_span_lookup():
    from plugin.calc.inspector import CellInspector
    rows = {0: [(0, 1, "date", "YYYY-MM-DD"), (3, 4, "time", None)]}
    assert CellInspector._temporal_span(rows, 0, 1) == ("date", "YYYY-MM-DD")
    assert CellInspector._temporal_span(rows, 0, 2) is None
    assert CellInspector._temporal_span(rows, 0, 3) == ("time", None)
    assert CellInspector._temporal_span(rows, 5, 0) is None


def test_inspector_read_cell_format_enrichment_and_negative_duration():
    from plugin.calc.inspector import CellInspector

    formats = MagicMock()
    props10 = MagicMock()
    props10.getPropertyValue.side_effect = lambda name: 2 if name == "Type" else "YYYY-MM-DD"
    formats.getByKey.return_value = props10

    doc = TestingFactory.create_doc(
        doc_type="calc",
        number_formats=formats,
        null_date=SimpleNamespace(Year=1899, Month=12, Day=30),
    )
    bridge = MagicMock()
    bridge.doc = doc

    cell = MagicMock()
    cell.getType.return_value = 1  # VALUE
    cell.getValue.return_value = 46237.0
    cell.getFormula.return_value = ""
    cell.getDataArray.return_value = ((46237.0,),)
    cell.getFormulaArray.return_value = (("",),)
    cell.getPropertyValue.side_effect = lambda name: 10 if name == "NumberFormat" else None
    cell.getRangeAddress.return_value = SimpleNamespace(StartColumn=0, EndColumn=0, StartRow=0, EndRow=0)

    bridge.resolve_range_or_address.return_value = cell

    res = CellInspector(bridge).read_cell("A1", include_format_info=True)
    assert res["value"] == "2026-08-03"
    assert res["type"] == "date"
    assert res["format_category"] == "date"
    assert res["format_code"] == "YYYY-MM-DD"

    # Second case: Type 4 / "[HH]:MM:SS" with -0.5
    props43 = MagicMock()
    props43.getPropertyValue.side_effect = lambda name: 4 if name == "Type" else "[HH]:MM:SS"
    formats.getByKey.return_value = props43
    cell.getValue.return_value = -0.5
    cell.getDataArray.return_value = ((-0.5,),)
    cell.getPropertyValue.side_effect = lambda name: 43 if name == "NumberFormat" else None

    res_neg = CellInspector(bridge).read_cell("A1", include_format_info=True)
    assert res_neg["value"] == -0.5
    assert "format_category" not in res_neg


def test_inspector_read_range_negative_duration_stays_raw_and_does_not_fail():
    from plugin.calc.inspector import CellInspector

    date_addr0 = SimpleNamespace(StartColumn=0, EndColumn=0, StartRow=0, EndRow=0)
    date_addr1 = SimpleNamespace(StartColumn=1, EndColumn=1, StartRow=0, EndRow=0)

    rep0 = MagicMock()
    rep0.getPropertyValue.return_value = 43
    group0 = MagicMock()
    group0.getCount.return_value = 1
    group0.getByIndex.return_value = rep0
    group0.getRangeAddresses.return_value = (date_addr0,)

    rep1 = MagicMock()
    rep1.getPropertyValue.return_value = 10
    group1 = MagicMock()
    group1.getCount.return_value = 1
    group1.getByIndex.return_value = rep1
    group1.getRangeAddresses.return_value = (date_addr1,)

    format_groups = MagicMock()
    format_groups.getCount.return_value = 2
    format_groups.getByIndex.side_effect = lambda idx: group0 if idx == 0 else group1

    bridge, _cell_range, formats = _make_range_bridge(
        data_array=((-0.5, 46237.0),),
        formula_array=(("0", "0"),),
        date_addresses=(date_addr0, date_addr1),
        format_groups=format_groups,
    )

    props43 = MagicMock()
    props43.getPropertyValue.side_effect = lambda name: 4 if name == "Type" else "[HH]:MM:SS"
    props10 = MagicMock()
    props10.getPropertyValue.side_effect = lambda name: 2 if name == "Type" else "YYYY-MM-DD"
    formats.getByKey.side_effect = lambda key: props43 if key == 43 else props10

    result = CellInspector(bridge).read_range("A1:B1", include_format_info=True)

    assert result[0][0]["value"] == -0.5
    assert "format_category" not in result[0][0]
    assert result[0][1]["value"] == "2026-08-03"
    assert result[0][1]["type"] == "date"
    assert result[0][1]["format_category"] == "date"


def test_inspector_format_group_failure_does_not_fail_other_groups():
    from plugin.calc.inspector import CellInspector

    date_addr1 = SimpleNamespace(StartColumn=1, EndColumn=1, StartRow=0, EndRow=0)

    group0 = MagicMock()
    group0.getCount.side_effect = RuntimeError("bad key")

    rep1 = MagicMock()
    rep1.getPropertyValue.return_value = 10
    group1 = MagicMock()
    group1.getCount.return_value = 1
    group1.getByIndex.return_value = rep1
    group1.getRangeAddresses.return_value = (date_addr1,)

    format_groups = MagicMock()
    format_groups.getCount.return_value = 2
    format_groups.getByIndex.side_effect = lambda idx: group0 if idx == 0 else group1

    bridge, _cell_range, formats = _make_range_bridge(
        data_array=((1.0, 46237.0),),
        formula_array=(("1", "46237"),),
        date_addresses=(date_addr1,),
        format_groups=format_groups,
        format_type=2,
        format_key=10,
    )
    props10 = MagicMock()
    props10.getPropertyValue.side_effect = lambda name: 2 if name == "Type" else "YYYY-MM-DD"
    formats.getByKey.side_effect = lambda key: props10

    result = CellInspector(bridge).read_range("A1:B1", include_format_info=True)

    assert result[0][0]["value"] == 1.0
    assert "format_category" not in result[0][0]
    assert result[0][1]["value"] == "2026-08-03"
    assert result[0][1]["type"] == "date"


def _range_addr(*, start_col, end_col, start_row, end_row):
    mock_range = MagicMock()
    mock_range.getRangeAddress.return_value = MagicMock(
        StartColumn=start_col, EndColumn=end_col, StartRow=start_row, EndRow=end_row
    )
    return mock_range


def _inspector_sample_grid(values_row: list[Any], *, row: int = 1, start_col: int = 0) -> list[list[dict[str, Any]]]:
    from plugin.calc.address_utils import index_to_column

    return [
        [
            {
                "address": f"{index_to_column(start_col + ci)}{row}",
                "value": val,
                "formula": None,
                "type": "text" if isinstance(val, str) else "value",
            }
            for ci, val in enumerate(values_row)
        ]
    ]


def test_read_cell_range_tool_opts_into_format_info():
    ctx = SimpleNamespace(doc=MagicMock())
    grid = [[{"address": "A1", "value": 1.0, "formula": None, "type": "value"}]]
    with (
        patch("plugin.calc.cells.CalcBridge") as bridge_cls,
        patch("plugin.calc.cells.CellInspector") as inspector_cls,
    ):
        bridge_cls.return_value.resolve_range_or_address.return_value = _range_addr(
            start_col=0, end_col=0, start_row=0, end_row=0
        )
        inspector_cls.return_value.read_range.return_value = grid
        result = _tool("read_cell_range").execute_safe(ctx, range=["A1"])

    entry = _only_entry(result)
    assert not entry.get("truncated")
    payload = entry["result"][0]
    assert payload["origin"] == "A1"
    assert payload["range"] == "A1"
    assert payload["rows"] == [[1.0]]
    assert "formulas" not in payload
    assert "types" not in payload
    inspector_cls.return_value.read_range.assert_called_once_with("A1", include_format_info=True)


def test_grid_to_rows_synthetic():
    from plugin.calc.inspector import grid_to_rows

    grid = [
        [
            {"address": "A1", "value": "Col1", "formula": None, "type": "text"},
            {"address": "B1", "value": None, "formula": None, "type": "empty"},
        ],
        [
            {"address": "A2", "value": "2026-08-03", "formula": None, "type": "date", "format_category": "date"},
            {"address": "B2", "value": 3.0, "formula": "=A1+1", "type": "formula"},
        ],
    ]
    payload = grid_to_rows(grid, range_name="A1:B2")
    assert payload["origin"] == "A1"
    assert payload["range"] == "A1:B2"
    assert payload["rows"] == [["Col1", None], ["2026-08-03", 3.0]]
    assert payload["formulas"] == {"B2": "=A1+1"}
    assert payload["types"] == {"A2": "date"}
    assert "format_code" not in payload


def test_read_cell_range_format_cells_returns_grid():
    ctx = SimpleNamespace(doc=MagicMock())
    grid = [[{"address": "A1", "value": 1.0, "type": "value"}]]
    with (
        patch("plugin.calc.cells.CalcBridge") as bridge_cls,
        patch("plugin.calc.cells.CellInspector") as inspector_cls,
    ):
        bridge_cls.return_value.resolve_range_or_address.return_value = _range_addr(
            start_col=0, end_col=0, start_row=0, end_row=0
        )
        inspector_cls.return_value.read_range.return_value = grid
        result = _tool("read_cell_range").execute_safe(ctx, range=["A1"], format="cells")

    assert _only_entry(result)["result"][0] is grid


def test_read_cell_range_every_range_is_one_entry():
    ctx = SimpleNamespace(doc=MagicMock())
    grid_a = [[{"address": "A1", "value": 1.0, "formula": None, "type": "value"}]]
    grid_b = [[{"address": "B2", "value": 2.0, "formula": None, "type": "value"}]]
    with (
        patch("plugin.calc.cells.CalcBridge") as bridge_cls,
        patch("plugin.calc.cells.CellInspector") as inspector_cls,
    ):
        bridge_cls.return_value.resolve_range_or_address.return_value = _range_addr(
            start_col=0, end_col=0, start_row=0, end_row=0
        )
        inspector_cls.return_value.read_range.return_value = grid_a
        one = _tool("read_cell_range").execute_safe(ctx, range=["A1"])
        assert one == {
            "status": "ok",
            "result": [
                {
                    "status": "ok",
                    "range": "A1",
                    "result": [{"origin": "A1", "range": "A1", "rows": [[1.0]]}],
                }
            ],
        }

        inspector_cls.return_value.read_range.side_effect = [grid_a, grid_b]
        two = _tool("read_cell_range").execute_safe(ctx, range=["A1", "B2"])
        assert two["status"] == "ok"
        assert [e["range"] for e in two["result"]] == ["A1", "B2"]
        assert two["result"][0]["result"][0]["rows"] == [[1.0]]
        assert two["result"][1]["result"][0]["rows"] == [[2.0]]

        inspector_cls.return_value.read_range.side_effect = None
        inspector_cls.return_value.read_range.return_value = grid_a
        cells_res = _tool("read_cell_range").execute_safe(ctx, range=["A1"], format="cells")
        assert _only_entry(cells_res)["result"][0] is grid_a


def test_read_cell_range_tool_truncates_large_range():
    """A1:H500 must not dump thousands of cell dicts into chat (issue 405)."""
    from plugin.calc.cells import _READ_CELL_RANGE_TRUNCATED_MSG

    ctx = SimpleNamespace(doc=MagicMock())
    sample = _inspector_sample_grid(["OrderID"] * 8)
    with (
        patch("plugin.calc.cells.CalcBridge") as bridge_cls,
        patch("plugin.calc.cells.CellInspector") as inspector_cls,
    ):
        # A1:H500 → 8 cols × 500 rows
        bridge_cls.return_value.resolve_range_or_address.return_value = _range_addr(
            start_col=0, end_col=7, start_row=0, end_row=499
        )
        inspector_cls.return_value.read_range.return_value = sample
        result = _tool("read_cell_range").execute_safe(ctx, range=["A1:H500"])

    entry = _only_entry(result)
    assert entry["truncated"] is True
    assert entry["cell_count"] == 4000
    assert entry["row_count"] == 500
    assert entry["column_count"] == 8
    assert entry["preview_range"] == "A1:H10"
    assert entry["message"] == _READ_CELL_RANGE_TRUNCATED_MSG.format(
        rows=500, columns=8, cells=4000
    )
    assert "overload" in entry["message"]
    assert "500 rows" in entry["message"]
    assert "8 columns" in entry["message"]
    assert "4000 cells" in entry["message"]
    assert "peek only" in entry["message"]
    assert "pass this A1 address to =PY instead of re-reading" not in entry["message"]
    assert "write_formula_range" in entry["message"]
    assert "fill-down" in entry["message"]
    assert "A1:H10" in entry["hint"]
    assert "write_formula_range" in entry["hint"]
    assert "next_range" not in entry
    inspector_cls.return_value.read_range.assert_called_once_with("A1:H10", include_format_info=True)


def test_read_cell_range_min_over_cap_steers_fill_down_not_hard_py():
    """9×9 fixture (81 cells) is the smallest range-too-large repro.

    Chat `read_cell_range` caps at 80 cells. Message names size + peek and
    steers row-wise work to ordinary fill-down, not a hard =PY funnel.
    """
    from plugin.calc.cells import (
        _READ_CELL_RANGE_MAX_CELLS,
        _READ_CELL_RANGE_TRUNCATED_MSG,
    )

    from tests.harness.strip_bundle import skip_if_release_build

    skip_if_release_build("docs/ not in stripped release tree")
    assert _MIN_RANGE_TOO_LARGE_ODS.is_file(), _MIN_RANGE_TOO_LARGE_ODS
    rows, cols = _used_shape_from_ods(_MIN_RANGE_TOO_LARGE_ODS)
    cells = rows * cols
    assert cells > _READ_CELL_RANGE_MAX_CELLS
    assert (rows, cols, cells) == (9, 9, 81)

    ctx = SimpleNamespace(doc=MagicMock())
    sample = _inspector_sample_grid(["ID"] * cols)
    range_name = "A1:I9"
    with (
        patch("plugin.calc.cells.CalcBridge") as bridge_cls,
        patch("plugin.calc.cells.CellInspector") as inspector_cls,
    ):
        # 9×9 used range in min-range-too-large.ods (A1:I9).
        bridge_cls.return_value.resolve_range_or_address.return_value = _range_addr(
            start_col=0, end_col=cols - 1, start_row=0, end_row=rows - 1
        )
        inspector_cls.return_value.read_range.return_value = sample
        result = _tool("read_cell_range").execute_safe(ctx, range=[range_name])

    entry = _only_entry(result)
    assert entry["truncated"] is True
    assert entry["cell_count"] == 81
    assert entry["row_count"] == 9
    assert entry["column_count"] == 9
    assert entry["range"] == range_name
    assert entry["message"] == _READ_CELL_RANGE_TRUNCATED_MSG.format(
        rows=9, columns=9, cells=81
    )
    assert "9 rows" in entry["message"]
    assert "9 columns" in entry["message"]
    assert "81 cells" in entry["message"]
    assert "peek only" in entry["message"]
    assert "pass this A1 address to =PY instead of re-reading" not in entry["message"]
    assert "write_formula_range" in entry["message"]
    assert "fill-down" in entry["message"]
    assert entry["message"].index("write_formula_range") < entry["message"].index("=PY")
    # Preview clips rows to 10 and columns to 8; 9 data rows stay 9 but columns become 8 (A1:H9).
    assert entry["preview_range"] == "A1:H9"
    inspector_cls.return_value.read_range.assert_called_once_with("A1:H9", include_format_info=True)


def test_read_cell_range_tool_keeps_small_range_full():
    from plugin.calc.cells import _READ_CELL_RANGE_MAX_CELLS

    ctx = SimpleNamespace(doc=MagicMock())
    with (
        patch("plugin.calc.cells.CalcBridge") as bridge_cls,
        patch("plugin.calc.cells.CellInspector") as inspector_cls,
    ):
        # Exactly the cap (8×10) still returns the full grid.
        bridge_cls.return_value.resolve_range_or_address.return_value = _range_addr(
            start_col=0, end_col=7, start_row=0, end_row=9
        )
        inspector_cls.return_value.read_range.return_value = _inspector_sample_grid([1])
        result = _tool("read_cell_range").execute_safe(ctx, range=["A1:H10"])

    assert _READ_CELL_RANGE_MAX_CELLS == 80
    assert not _only_entry(result).get("truncated")
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


def test_preview_if_large_caps_columns_and_adds_sheet_name():
    from plugin.calc.cells import _preview_if_large

    bridge = MagicMock()
    # Mock A1:AMJ1 (1 row, 1024 columns)
    cell_range = _range_addr(start_col=0, end_col=1023, start_row=0, end_row=0)
    sheet_mock = MagicMock()
    sheet_mock.getName.return_value = "My Sheet"
    cell_range.getSpreadsheet = MagicMock(return_value=sheet_mock)
    bridge.resolve_range_or_address.return_value = cell_range

    # range_name lacks sheet name
    preview = _preview_if_large(bridge, "MyNamedRange")

    assert preview is not None
    # Caps to 8 columns max, row remains 1. End column is H (index 7).
    assert preview["preview_range"] == "'My Sheet'.A1:H1"


def test_preview_if_large_quotes_non_plain_identifier_sheet_name():
    from plugin.calc.cells import _preview_if_large

    bridge = MagicMock()
    cell_range = _range_addr(start_col=0, end_col=1023, start_row=0, end_row=0)
    sheet_mock = MagicMock()
    sheet_mock.getName.return_value = "Q1.Sales"
    cell_range.getSpreadsheet = MagicMock(return_value=sheet_mock)
    bridge.resolve_range_or_address.return_value = cell_range

    preview = _preview_if_large(bridge, "MyNamedRange")
    assert preview is not None
    assert preview["preview_range"] == "'Q1.Sales'.A1:H1"


def test_column_distinct_peek_lists_low_cardinality_and_skips_phone_book():
    from plugin.calc.cells import (
        _DISTINCT_PEEK_HIGH_CARDINALITY,
        _column_distinct_peek,
    )

    rows = [("Country", "Phone")]
    for i in range(_DISTINCT_PEEK_HIGH_CARDINALITY + 5):
        # Two countries, unique phones — phone book must not be listed.
        rows.append(("US" if i % 2 == 0 else "UK", f"555-{i:04d}"))
    peek = _column_distinct_peek(tuple(rows), start_column=0)
    assert peek[0]["column"] == "Country"
    assert peek[0]["unique_count"] == 2
    assert peek[0]["values"] == ["US", "UK"]
    assert peek[1]["column"] == "Phone"
    assert peek[1]["unique_count"] == f"{_DISTINCT_PEEK_HIGH_CARDINALITY}+"
    assert peek[1]["skipped"] == "high cardinality"
    assert "values" not in peek[1]

    # Repeats of a mid-size set must not inflate the count past the true cardinality.
    mid = [("Code",)] + [(f"C{i % 20}",) for i in range(200)]
    mid_peek = _column_distinct_peek(tuple(mid), start_column=2)
    assert mid_peek[0]["column"] == "Code"
    assert mid_peek[0]["unique_count"] == 20
    assert mid_peek[0]["skipped"] == "high cardinality"
    assert "values" not in mid_peek[0]


def test_read_cell_range_truncated_attaches_column_distincts():

    ctx = SimpleNamespace(doc=MagicMock())
    sample = _inspector_sample_grid(["Country", "Country"])
    data = (
        ("Country", "Code"),
        ("US", "1"),
        ("UK", "44"),
        ("US", "1"),
    )
    mock_range = _range_addr(start_col=0, end_col=1, start_row=0, end_row=99)
    mock_range.getDataArray.return_value = data
    with (
        patch("plugin.calc.cells.CalcBridge") as bridge_cls,
        patch("plugin.calc.cells.CellInspector") as inspector_cls,
    ):
        # 2×100 is over the 80-cell cap.
        bridge_cls.return_value.resolve_range_or_address.return_value = mock_range
        inspector_cls.return_value.read_range.return_value = sample
        result = _tool("read_cell_range").execute_safe(ctx, range=["A1:B100"])

    entry = _only_entry(result)
    assert entry["truncated"] is True
    assert entry["column_distincts"] == [
        {"column": "Country", "unique_count": 2, "values": ["US", "UK"]},
        {"column": "Code", "unique_count": 2, "values": ["1", "44"]},
    ]


def test_read_cell_range_description_not_hard_py_first():
    from plugin.calc.cells import ReadCellRange

    desc = ReadCellRange.description
    assert "write =PY(..., DataRange) instead of reading the block" not in desc
    assert "write_formula_range (fill-down)" in desc
    assert desc.index("write_formula_range") < desc.index("=PY")


def test_set_style_accepts_typed_bold_and_font_size():

    ctx = SimpleNamespace(doc=MagicMock())
    with patch("plugin.calc.cells.CellManipulator") as manip_cls:
        result = _tool("set_style").execute_safe(ctx, range=["A1"], bold=True, font_size=12)

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
    bridge.doc = doc

    formatter = MagicMock()
    formatter.detectNumberFormat.return_value = 37
    formatter.convertStringToNumber.return_value = 46242.0

    manip = CellManipulator(bridge)
    with patch.object(manip, "_make_number_formatter", return_value=formatter):
        with patch.object(manip, "_apply_temporal_format_runs", side_effect=RuntimeError("format boom")):
            msg = manip.write_formula_range("A1", "2026-08-08")["message"]

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
    bridge.doc = doc

    formatter = MagicMock()
    formatter.detectNumberFormat.return_value = 37
    formatter.convertStringToNumber.return_value = 46242.0

    manip = CellManipulator(bridge)
    with patch.object(manip, "_make_number_formatter", return_value=formatter):
        with patch.object(manip, "_apply_temporal_format_runs", side_effect=RuntimeError("format boom")):
            msg = manip.write_formula_range("A1:B1", '["2026-08-08", "2026-08-09"]')["message"]

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

    manip._apply_temporal_format_runs(sheet, start=(0, 10), decisions=decisions)

    sheet.getCellRangeByPosition.assert_called_once_with(0, 10, 0, 12)
    target.setPropertyValue.assert_called_once_with("NumberFormat", 42)


def test_make_number_formatter_unwraps_guarded_doc():
    """attachNumberFormatsSupplier must get the raw doc, not a Layer-A proxy.

    The stub defines ``_UnoThreadGuardProxy`` and never constructs it
    (``_wrap_uno`` returns the raw object); patch ``unwrap_uno`` so the
    test does not depend on which module is loaded.
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
        with patch("plugin.calc.manipulator.unwrap_uno", side_effect=fake_unwrap):
            out = manip._make_number_formatter(proxied_doc)

    assert out is formatter
    formatter.attachNumberFormatsSupplier.assert_called_once_with(raw_doc)
    smgr.createInstanceWithContext.assert_called_once_with("com.sun.star.util.NumberFormatter", raw_ctx)


def test_write_formula_range_empty_uno_error_uses_type_name():
    """Blank UNO str(e) must not surface as an empty CalcError."""
    from plugin.calc.manipulator import CellManipulator
    from plugin.framework.errors import CalcError

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

    class _BlankUno(Exception):
        def __str__(self):
            return "UNO write failure"

    doc = MagicMock()
    ctx = MagicMock()
    ctx.doc = doc

    bridge = MagicMock()
    bridge.resolve_range_or_address.side_effect = _BlankUno()

    tool = _tool("write_formula_range")
    with patch("plugin.calc.cells.CalcBridge", return_value=bridge):
        res = tool.execute(ctx, range=["A1"], values="123")
    assert res["status"] == "error"
    assert "UNO write failure" in res["message"]


def test_json_array_value_count_and_a1_shape():
    from plugin.calc.address_utils import range_shape
    from plugin.calc.manipulator import _parse_formula_or_values_string

    assert len(_parse_formula_or_values_string('["a", "b", "c", "d"]')) == 4
    assert _parse_formula_or_values_string("=B2+1") is None
    assert _parse_formula_or_values_string("") is None
    assert _parse_formula_or_values_string("[]") == []
    assert range_shape("C2:C9") == (8, 8, 1)
    assert range_shape("Sheet1.C2:C9") == (8, 8, 1)
    assert range_shape("A1:B1") == (2, 1, 2)
    assert range_shape("SalesData") is None


def test_write_formula_range_source_is_stripped_and_blank_falls_to_values():
    ctx = SimpleNamespace(doc=MagicMock())
    with (
        patch("plugin.calc.cells.CalcBridge"),
        patch("plugin.calc.cells.CellManipulator") as manip_cls,
    ):
        manip = manip_cls.return_value
        manip.copy_formula_range.return_value = {"message": "copied", "rows_copied": 3, "cols_copied": 3}
        res_copy = _tool("write_formula_range").execute_safe(ctx, range=["D1:F3"], source="  Sheet1.A1:C3  ")
        assert res_copy["status"] == "ok"
        manip.copy_formula_range.assert_called_once_with("Sheet1.A1:C3", "D1:F3")

        manip.reset_mock()
        manip.commit_write.return_value = {"message": "wrote"}
        res_val = _tool("write_formula_range").execute_safe(ctx, range=["A1"], source="   ", values="1")
        assert res_val["status"] == "ok"
        manip.copy_formula_range.assert_not_called()
        manip.plan_write.assert_called_once()


def test_write_formula_range_rejects_semicolon_json_mismatch():
    from plugin.calc.manipulator import CellManipulator

    ctx = SimpleNamespace(doc=MagicMock())
    with (
        patch("plugin.calc.cells.CalcBridge", return_value=_a1_bridge()),
        patch.object(CellManipulator, "commit_write") as commit,
    ):
        result = _tool("write_formula_range").execute_safe(
            ctx, range=["A1:A3"], values='["a"; "b"]'
        )

    assert result["status"] == "error"
    assert "2 values" in result["message"]
    assert "3 cells" in result["message"]
    assert "one value per row" in result["message"]
    commit.assert_not_called()


def test_write_formula_range_rejects_json_length_mismatch():
    """4 values into an 8-cell column must fail loud — no silent zip/pad."""
    from plugin.calc.manipulator import CellManipulator

    ctx = SimpleNamespace(doc=MagicMock())
    with (
        patch("plugin.calc.cells.CalcBridge", return_value=_a1_bridge()),
        patch.object(CellManipulator, "commit_write") as commit,
    ):
        result = _tool("write_formula_range").execute_safe(
            ctx, range=["C2:C9"], values='["a", "b", "c", "d"]'
        )

    assert result["status"] == "error"
    assert "4 values" in result["message"]
    assert "8 cells" in result["message"]
    assert "one value per row" in result["message"]
    commit.assert_not_called()


def test_write_formula_range_rejects_list_length_mismatch():
    from plugin.calc.manipulator import CellManipulator

    ctx = SimpleNamespace(doc=MagicMock())
    with (
        patch("plugin.calc.cells.CalcBridge", return_value=_a1_bridge()),
        patch.object(CellManipulator, "commit_write") as commit,
    ):
        result = _tool("write_formula_range").execute_safe(
            ctx, range=["A1:B4"], values=["a", "b", "c", "d"]
        )

    assert result["status"] == "error"
    assert "4 values" in result["message"]
    assert "8 cells" in result["message"]
    commit.assert_not_called()


def test_write_formula_range_accepts_matching_json_and_scalar_fill():

    ctx = SimpleNamespace(doc=MagicMock())
    with (
        patch("plugin.calc.cells.CalcBridge"),
        patch("plugin.calc.cells.CellManipulator") as manip_cls,
    ):
        manip_cls.return_value.commit_write.return_value = {"message": "wrote"}
        matched = _tool("write_formula_range").execute_safe(ctx, range=["A1:A2"], values='["x", "y"]')
        filled = _tool("write_formula_range").execute_safe(ctx, range=["A1:A8"], values="=B2+1")
        cleared = _tool("write_formula_range").execute_safe(ctx, range=["A1:A8"], values="")

    assert matched["status"] == "ok"
    assert filled["status"] == "ok"
    assert cleared["status"] == "ok"
    assert manip_cls.return_value.commit_write.call_count == 3


def test_write_formula_range_source_and_values_errors():
    """source + values is forbidden — no silent write or copy."""

    ctx = SimpleNamespace(doc=MagicMock())
    with (
        patch("plugin.calc.cells.CalcBridge"),
        patch("plugin.calc.cells.CellManipulator") as manip_cls,
    ):
        result = _tool("write_formula_range").execute_safe(
            ctx, range=["Sample.A1"], source="Sheet1.A1:C3", values="=A1"
        )

    assert result["status"] == "error"
    assert "do not pass values" in result["message"].lower()
    manip_cls.return_value.commit_write.assert_not_called()
    manip_cls.return_value.copy_formula_range.assert_not_called()


def test_write_formula_range_source_and_empty_values_errors():

    ctx = SimpleNamespace(doc=MagicMock())
    with (
        patch("plugin.calc.cells.CalcBridge"),
        patch("plugin.calc.cells.CellManipulator") as manip_cls,
    ):
        result = _tool("write_formula_range").execute_safe(
            ctx, range=["Sample.A1"], source="Sheet1.A1:C3", values=""
        )

    assert result["status"] == "error"
    assert "do not pass values" in result["message"].lower()
    manip_cls.return_value.copy_formula_range.assert_not_called()


def test_write_formula_range_source_only_copies_block_size():

    ctx = SimpleNamespace(doc=MagicMock())
    with (
        patch("plugin.calc.cells.CalcBridge"),
        patch("plugin.calc.cells.CellManipulator") as manip_cls,
    ):
        manip_cls.return_value.copy_formula_range.return_value = {
            "message": "Copied 3×3 from Sheet1.A1:C3 onto Sample.A1.",
            "rows_copied": 3,
            "cols_copied": 3,
        }
        result = _tool("write_formula_range").execute_safe(
            ctx, range=["Sample.A1"], source="Sheet1.A1:C3"
        )
        result2 = _tool("write_formula_range").execute_safe(
            ctx, range=["Sample.A1"], source="Sheet1.A1:C3", values=None
        )

    assert result["status"] == "ok"
    assert result["rows_copied"] == 3
    assert result["cols_copied"] == 3
    assert "3×3" in result["message"]
    assert result2["status"] == "ok"
    manip_cls.return_value.commit_write.assert_not_called()
    assert manip_cls.return_value.copy_formula_range.call_count == 2
    manip_cls.return_value.copy_formula_range.assert_called_with(
        "Sheet1.A1:C3", "Sample.A1"
    )


def test_write_formula_range_missing_values_without_source_errors():

    ctx = SimpleNamespace(doc=MagicMock())
    with (
        patch("plugin.calc.cells.CalcBridge"),
        patch("plugin.calc.cells.CellManipulator") as manip_cls,
    ):
        result = _tool("write_formula_range").execute_safe(ctx, range=["A1"])

    assert result["status"] == "error"
    assert "values is required" in result["message"]
    manip_cls.return_value.commit_write.assert_not_called()
    manip_cls.return_value.copy_formula_range.assert_not_called()


def test_write_formula_range_description_teaches_source_copy():
    from plugin.calc.cells import WriteCellRange

    desc = WriteCellRange.description
    assert "DO: to copy a block onto another sheet or place, pass source and dest range" in desc
    assert "do not pass values" in desc
    assert WriteCellRange.parameters["required"] == ["range"]
    assert "source" in WriteCellRange.parameters["properties"]


def test_write_formula_range_values_teaches_fill_down_not_json_pin():
    from plugin.calc.cells import WriteCellRange

    values = WriteCellRange.parameters["properties"]["values"]["description"]
    assert "Required unless source is set" in values
    assert "One ordinary formula into a multi-cell" in values
    assert "fill-down/across" in values
    assert "relative A1 refs" in values
    assert "rate or tax column" in values
    assert "inner array per row" in values
    assert "pins every row to the first ref" in values
    assert "one formula string over the whole column" in values
    assert "FILTER/SORT/UNIQUE" in values
    assert "array formula" in values
    assert "occupied cells" in values
    assert "=PY() is still the spill path" in values
    # Fixture names stay out of tool text (eval tax/sort sheets).
    assert "Banana" not in values
    assert "Product" not in values
    assert "Revenue" not in values
    assert WriteCellRange.parameters["properties"]["values"]["type"] == ["string", "array", "null"]


def test_write_formula_range_passes_native_2d_grid_to_manipulator():

    grid = [["a", 1], ["b", None]]
    ctx = SimpleNamespace(doc=MagicMock())
    with (
        patch("plugin.calc.cells.CalcBridge"),
        patch("plugin.calc.cells.CellManipulator") as manip_cls,
    ):
        manip_cls.return_value.commit_write.return_value = {"message": "wrote"}
        result = _tool("write_formula_range").execute_safe(ctx, range=["A1:B2"], values=grid)

    assert result["status"] == "ok"
    manip_cls.return_value.plan_write.assert_called_once()
    assert manip_cls.return_value.plan_write.call_args[0][1] == grid


def test_write_formula_range_passes_bool_cells_in_grid():

    grid = [[True, False]]
    ctx = SimpleNamespace(doc=MagicMock())
    with (
        patch("plugin.calc.cells.CalcBridge"),
        patch("plugin.calc.cells.CellManipulator") as manip_cls,
    ):
        manip_cls.return_value.commit_write.return_value = {"message": "wrote"}
        result = _tool("write_formula_range").execute_safe(ctx, range=["A1:B1"], values=grid)

    assert result["status"] == "ok"
    assert manip_cls.return_value.plan_write.call_args[0][1] == grid


def test_write_formula_range_rejects_2d_grid_shape_mismatch():
    from plugin.calc.manipulator import CellManipulator

    ctx = SimpleNamespace(doc=MagicMock())
    with (
        patch("plugin.calc.cells.CalcBridge", return_value=_a1_bridge()),
        patch.object(CellManipulator, "commit_write") as commit,
    ):
        result = _tool("write_formula_range").execute_safe(
            ctx, range=["A1:C2"], values=[["a", "b"], ["c", "d"]]
        )

    assert result["status"] == "error"
    assert "2x2" in result["message"]
    assert "2x3" in result["message"]
    commit.assert_not_called()


def test_write_formula_range_rejects_ragged_rows_before_manipulator():
    from plugin.calc.manipulator import CellManipulator

    ctx = SimpleNamespace(doc=MagicMock())
    with (
        patch("plugin.calc.cells.CalcBridge", return_value=_a1_bridge()),
        patch.object(CellManipulator, "commit_write") as commit,
    ):
        result = _tool("write_formula_range").execute_safe(
            ctx, range=["A1:B2"], values=[["a"], ["b", "c"]]
        )

    assert result["status"] == "error"
    commit.assert_not_called()


def test_write_formula_range_rejects_dict_cell_before_manipulator():
    from plugin.calc.manipulator import CellManipulator

    ctx = SimpleNamespace(doc=MagicMock())
    with (
        patch("plugin.calc.cells.CalcBridge", return_value=_a1_bridge()),
        patch.object(CellManipulator, "commit_write") as commit,
    ):
        result = _tool("write_formula_range").execute_safe(ctx, range=["A1"], values=[{"x": 1}])

    assert result["status"] == "error"
    assert "boolean, or null" in result["message"]
    commit.assert_not_called()


def test_write_formula_range_rejects_mixed_grid_rows_before_manipulator():
    from plugin.calc.manipulator import CellManipulator

    ctx = SimpleNamespace(doc=MagicMock())
    with (
        patch("plugin.calc.cells.CalcBridge", return_value=_a1_bridge()),
        patch.object(CellManipulator, "commit_write") as commit,
    ):
        result = _tool("write_formula_range").execute_safe(ctx, range=["A1"], values=[["a"], "b"])

    assert result["status"] == "error"
    assert "boolean, or null" in result["message"]
    commit.assert_not_called()


def test_sort_range_description_teaches_reorder_key_and_direction():
    from plugin.calc.cells import SortRange

    desc = SortRange.description
    assert "Do call range_sort to reorder rows" in desc
    assert "not rewrite the block with write_formula_range" in desc
    assert "labels mid-table" in desc
    assert "Pick sort_column for the metric" in desc
    assert "ascending=false when largest values should come first" in desc
    sort_col = SortRange.parameters["properties"]["sort_column"]["description"]
    assert "0 = leftmost" in sort_col
    assert "numeric/metric column" in sort_col
    assert "not the label column" in sort_col
    ascending = SortRange.parameters["properties"]["ascending"]["description"]
    assert "largest/highest first" in ascending
    assert "smallest first" in ascending
    assert "Banana" not in desc
    assert "Product" not in desc
    assert "Revenue" not in desc


def test_preview_if_large_escapes_doubled_apostrophes():
    from plugin.calc.cells import _preview_if_large

    bridge = MagicMock()
    bridge.resolve_range_or_address.return_value = _range_addr(
        start_col=0, end_col=7, start_row=0, end_row=499
    )
    preview = _preview_if_large(bridge, "'O''Brien'!A1:H500")
    assert preview is not None
    assert preview["preview_range"] == "'O''Brien'!A1:H10"
    assert preview["sheet_prefix"] == "'O''Brien'!"


def test_read_cell_range_oversized_unqualified_named_range_clips_on_resolved_sheet():

    ctx = SimpleNamespace(doc=MagicMock())
    cell_range = _range_addr(start_col=0, end_col=1, start_row=0, end_row=99)
    sheet_mock = MagicMock()
    sheet_mock.getName.return_value = "O'Brien"
    cell_range.getSpreadsheet = MagicMock(return_value=sheet_mock)
    cell_range.getDataArray.return_value = (("Col1", "Col2"), (1, 2))

    with (
        patch("plugin.calc.cells.CalcBridge") as bridge_cls,
        patch("plugin.calc.cells.CellInspector") as inspector_cls,
    ):
        bridge = bridge_cls.return_value
        bridge.resolve_range_or_address.return_value = cell_range
        inspector_cls.return_value.read_range.return_value = _inspector_sample_grid([1])

        _tool("read_cell_range").execute_safe(ctx, range=["Sales"])

        calls = bridge.resolve_range_or_address.call_args_list
        assert len(calls) == 2
        assert calls[0].args[0] == "Sales"
        assert calls[1].args[0] == "'O''Brien'.A1:B100"


def test_read_cell_range_oversized_unqualified_named_range_clips_on_identifier_sheet():

    ctx = SimpleNamespace(doc=MagicMock())
    cell_range = _range_addr(start_col=0, end_col=1, start_row=0, end_row=99)
    sheet_mock = MagicMock()
    sheet_mock.getName.return_value = "Other"
    cell_range.getSpreadsheet = MagicMock(return_value=sheet_mock)
    cell_range.getDataArray.return_value = (("Col1", "Col2"), (1, 2))

    with (
        patch("plugin.calc.cells.CalcBridge") as bridge_cls,
        patch("plugin.calc.cells.CellInspector") as inspector_cls,
    ):
        bridge = bridge_cls.return_value
        bridge.resolve_range_or_address.return_value = cell_range
        inspector_cls.return_value.read_range.return_value = _inspector_sample_grid([1])

        _tool("read_cell_range").execute_safe(ctx, range=["Sales"])

        calls = bridge.resolve_range_or_address.call_args_list
        assert len(calls) == 2
        assert calls[0].args[0] == "Sales"
        assert calls[1].args[0] == "Other.A1:B100"


def test_sheet_prefix_for_clip_quoting_differs_from_quote_sheet_name():
    from plugin.calc.address_utils import quote_sheet_name
    from plugin.calc.cells import _sheet_prefix_for_clip

    # 1. 2024.A1:H500 -> '2024'. vs 2024.
    assert _sheet_prefix_for_clip("2024.A1:H500", None) == "'2024'."
    assert quote_sheet_name("2024") + "." == "2024."

    # 2. Q1-Sales.A1:H500 -> 'Q1-Sales'. vs Q1-Sales.
    assert _sheet_prefix_for_clip("Q1-Sales.A1:H500", None) == "'Q1-Sales'."
    assert quote_sheet_name("Q1-Sales") + "." == "Q1-Sales."

    # 3. 'Sheet1'.A1:H500 -> 'Sheet1'. vs Sheet1.
    assert _sheet_prefix_for_clip("'Sheet1'.A1:H500", None) == "'Sheet1'."
    assert quote_sheet_name("Sheet1") + "." == "Sheet1."

    # 4. A+B.A1 -> 'A+B'. vs A+B.
    assert _sheet_prefix_for_clip("A+B.A1", None) == "'A+B'."
    assert quote_sheet_name("A+B") + "." == "A+B."

    # 5. '$Prices'.$A$1 -> '$Prices'. vs '$Prices'.
    assert _sheet_prefix_for_clip("'$Prices'.$A$1", None) == "'$Prices'."
    assert quote_sheet_name("$Prices") + "." == "'$Prices'."

    # 6. 'Foo!Bar'.A1:H10 -> 'Foo!Bar'! vs 'Foo!Bar'. (bang anywhere in range_name causes '!')
    assert _sheet_prefix_for_clip("'Foo!Bar'.A1:H10", None) == "'Foo!Bar'!"
    assert quote_sheet_name("Foo!Bar") + "." == "'Foo!Bar'."

    # Unqualified case: 'Sales' resolved to sheet 2024 uses quote_sheet_name fallback -> 2024.
    resolved = MagicMock()
    resolved.getSpreadsheet.return_value.getName.return_value = "2024"
    assert _sheet_prefix_for_clip("Sales", resolved) == "2024."


def test_column_distinct_peek_counts_numeric_first_row_as_data():
    from plugin.calc.cells import _column_distinct_peek

    peek = _column_distinct_peek(((1, 2), (1, 3)), start_column=0)
    assert len(peek) == 2
    assert peek[0]["column"] == "A"
    assert peek[0]["unique_count"] == 1
    assert peek[0]["values"] == ["1"]
    assert peek[1]["column"] == "B"
    assert peek[1]["unique_count"] == 2
    assert peek[1]["values"] == ["2", "3"]


def test_try_column_distinct_peek_sets_scanned_rows_over_cap():
    from plugin.calc.cells import _try_column_distinct_peek

    bridge = MagicMock()
    mock_range = MagicMock()
    mock_range.getDataArray.return_value = (("A",), ("B",))
    bridge.resolve_range_or_address.return_value = mock_range

    preview_large = {
        "start_column": 0,
        "start_row": 0,
        "columns": 1,
        "rows": 2001,
        "sheet_prefix": "",
    }
    peek_large = _try_column_distinct_peek(bridge, "A1:A2001", preview_large)
    assert peek_large is not None
    assert peek_large[0]["scanned_rows"] == 2000

    preview_small = {
        "start_column": 0,
        "start_row": 0,
        "columns": 1,
        "rows": 10,
        "sheet_prefix": "",
    }
    peek_small = _try_column_distinct_peek(bridge, "A1:A10", preview_small)
    assert peek_small is not None
    assert "scanned_rows" not in peek_small[0]


def test_read_cell_range_partial_failure():

    ctx = SimpleNamespace(doc=MagicMock())
    with (
        patch("plugin.calc.cells.CalcBridge") as bridge_cls,
        patch("plugin.calc.cells.CellInspector") as inspector_cls,
    ):
        bridge = bridge_cls.return_value
        bridge.resolve_range_or_address.return_value = _range_addr(start_col=0, end_col=0, start_row=0, end_row=0)
        grid1 = [[{"address": "A1", "value": "ok", "formula": None, "type": "text"}]]
        inspector = inspector_cls.return_value
        inspector.read_range.side_effect = [grid1, RuntimeError("bad range")]

        res = _tool("read_cell_range").execute_safe(ctx, range=["A1", "B2"])
        assert res["status"] == "error"
        assert "Failed to read 1 of 2 ranges" in res["message"]
        assert "B2" in res["message"]
        assert len(res["result"]) == 2
        assert res["result"][0]["status"] == "ok"
        assert res["result"][0]["result"][0]["rows"] == [["ok"]]
        assert res["result"][1]["status"] == "error"
        assert res["result"][1]["range"] == "B2"
        assert "bad range" in res["result"][1]["message"]

        inspector.read_range.side_effect = RuntimeError("single fail")
        res_single = _tool("read_cell_range").execute_safe(ctx, range=["C3"])
        assert res_single["status"] == "error"
        assert res_single["range"] == "C3"
        assert "single fail" in res_single["message"]


def test_write_cell_range_multi_range_preserves_array_payload():
    ctx = SimpleNamespace(doc=MagicMock())
    dict_res = {
        "message": "Array formula entered on A1:A2 (2 x 1).",
        "array_range": "A1:A2",
        "rows": 2,
        "cols": 1,
        "preview": [[1], [2]],
        "warning": "cut",
    }
    with patch("plugin.calc.cells.CellManipulator") as manipulator_cls:
        manipulator = manipulator_cls.return_value
        manipulator.commit_write.side_effect = [dict_res, {"message": "wrote"}]

        res = _tool("write_formula_range").execute_safe(ctx, range=["A1:A2", "B1:B2"], values="=SEQUENCE(2)")
        assert res["status"] == "ok"
        assert res["message"] == "Wrote to 2 ranges"
        assert len(res["ranges"]) == 2
        assert res["ranges"][0]["warning"] == "cut"
        assert res["ranges"][0]["array_range"] == "A1:A2"
        assert res["ranges"][0]["range"] == "A1:A2"
        assert res["ranges"][1] == {"range": "B1:B2", "message": "wrote"}


def test_write_cell_range_multi_range_rolls_back_on_error():
    from plugin.framework.errors import CalcError

    fake_undo = FakeUndoManager()
    doc = MagicMock()
    doc.getUndoManager.return_value = fake_undo
    ctx = SimpleNamespace(doc=doc)

    with patch("plugin.calc.cells.CellManipulator") as manipulator_cls:
        manipulator = manipulator_cls.return_value
        manipulator.commit_write.side_effect = [{"message": "wrote"}, CalcError("occupied")]

        res = _tool("write_formula_range").execute_safe(ctx, range=["A1", "B1"], values="1")
        assert res["status"] == "error"
        assert res["code"] == "CALC_ERROR"
        assert "Nothing was kept." in res["message"]
        assert "B1" in res["message"]
        assert fake_undo.undone == 1


def test_set_cell_style_multi_range_rolls_back_on_error():
    from plugin.framework.errors import CalcError

    fake_undo = FakeUndoManager()
    doc = MagicMock()
    doc.getUndoManager.return_value = fake_undo
    ctx = SimpleNamespace(doc=doc)

    with patch("plugin.calc.cells.CellManipulator") as manipulator_cls:
        manipulator = manipulator_cls.return_value
        manipulator.set_cell_style.side_effect = [None, CalcError("style failed")]

        res = _tool("set_style").execute_safe(ctx, range=["A1", "B1"], bold=True)
        assert res["status"] == "error"
        assert res["code"] == "CALC_ERROR"
        assert "Nothing was kept." in res["message"]
        assert "B1" in res["message"]
        assert fake_undo.undone == 1


def test_set_cell_style_disposal_propagates():

    class DisposedException(Exception):
        pass

    fake_undo = FakeUndoManager()
    doc = MagicMock()
    doc.getUndoManager.return_value = fake_undo
    ctx = SimpleNamespace(doc=doc)

    with patch("plugin.calc.cells.CellManipulator") as manipulator_cls:
        manipulator = manipulator_cls.return_value
        manipulator.set_cell_style.side_effect = DisposedException("gone")

        tool = _tool("set_style")
        with pytest.raises(DisposedException):
            tool.execute(ctx, range=["A1"], bold=True)
        assert fake_undo.undone == 0

        res = tool.execute_safe(ctx, range=["A1"], bold=True)
        assert res["status"] == "error"
        assert res["code"] == "DOCUMENT_DISPOSED"


def test_set_cell_style_runtime_exception_still_rolls_back():

    class RuntimeException(Exception):
        pass

    fake_undo = FakeUndoManager()
    doc = MagicMock()
    doc.getUndoManager.return_value = fake_undo
    ctx = SimpleNamespace(doc=doc)

    with patch("plugin.calc.cells.CellManipulator") as manipulator_cls:
        manipulator = manipulator_cls.return_value
        manipulator.set_cell_style.side_effect = [None, RuntimeException("style failed")]

        res = _tool("set_style").execute_safe(ctx, range=["A1", "B1"], bold=True)
        assert res["status"] == "error"
        assert "Nothing was kept." in res["message"]
        assert fake_undo.undone == 1
        assert res.get("code") == "TOOL_EXECUTION_ERROR"


def test_write_formula_range_disposal_propagates():

    class DisposedException(Exception):
        pass

    fake_undo = FakeUndoManager()
    doc = MagicMock()
    doc.getUndoManager.return_value = fake_undo
    ctx = SimpleNamespace(doc=doc)

    with patch("plugin.calc.cells.CellManipulator") as manipulator_cls:
        manipulator = manipulator_cls.return_value
        manipulator.commit_write.side_effect = DisposedException("gone")

        tool = _tool("write_formula_range")
        with pytest.raises(DisposedException):
            tool.execute(ctx, range=["A1"], values="1")
        assert fake_undo.undone == 0

        manipulator.copy_formula_range.side_effect = DisposedException("gone")
        with pytest.raises(DisposedException):
            tool.execute(ctx, range=["B1"], source="A1")
        assert fake_undo.undone == 0


def test_empty_new_group_does_not_undo_older_action():
    from plugin.framework.errors import CalcError

    fake_undo = FakeUndoManager(titles=["WriterAgent: Write formulas"], locked=True)
    doc = MagicMock()
    doc.getUndoManager.return_value = fake_undo
    ctx = SimpleNamespace(doc=doc)

    with patch("plugin.calc.cells.CellManipulator") as manipulator_cls:
        manipulator = manipulator_cls.return_value
        manipulator.commit_write.side_effect = CalcError("fail")

        res = _tool("write_formula_range").execute_safe(ctx, range=["A1"], values="1")
        assert res["status"] == "error"
        assert fake_undo.undone == 0


def test_read_cell_range_sizing_failure_fails_closed():

    ctx = SimpleNamespace(doc=MagicMock())
    cell_range = MagicMock()
    cell_range.getRangeAddress.side_effect = RuntimeError("sizing failed")

    with (
        patch("plugin.calc.cells.CalcBridge") as bridge_cls,
        patch("plugin.calc.cells.CellInspector") as inspector_cls,
    ):
        bridge_cls.return_value.resolve_range_or_address.return_value = cell_range
        res = _tool("read_cell_range").execute_safe(ctx, range=["A1:H10"])

    assert res["status"] == "error"
    assert "Could not size range" in res["message"]
    inspector_cls.return_value.read_range.assert_not_called()


def test_two_range_array_write_measures_once():

    ctx = SimpleNamespace(doc=MagicMock())
    with patch("plugin.calc.cells.CellManipulator") as manip_cls:
        manipulator = manip_cls.return_value
        manipulator.plan_write.side_effect = [SimpleNamespace(array_size=(2, 2)), SimpleNamespace(array_size=(2, 2))]
        manipulator.commit_write.return_value = {"message": "ok"}

        res = _tool("write_formula_range").execute_safe(ctx, range=["A1:B2", "C1:D2"], values="=SEQUENCE(2, 2)")
        assert res["status"] == "ok"
        assert manipulator.plan_write.call_count == 2
        calls = manipulator.plan_write.call_args_list
        assert calls[0].kwargs.get("premeasured_array_size") is None
        assert calls[1].kwargs.get("premeasured_array_size") == (2, 2)


def test_write_formula_range_refuses_named_range_mismatch_before_any_write():
    from plugin.calc.manipulator import CellManipulator

    fake_undo = FakeUndoManager()
    doc = MagicMock()
    doc.getUndoManager.return_value = fake_undo
    ctx = SimpleNamespace(doc=doc)
    with (
        patch("plugin.calc.cells.CalcBridge", return_value=_a1_bridge({"Totals": "B1:B3"})),
        patch.object(CellManipulator, "commit_write") as commit,
    ):
        res = _tool("write_formula_range").execute_safe(ctx, range=["A1:A2", "Totals"], values=["x", "y"])

    assert res["status"] == "error"
    assert "Array has 2 values but range Totals has 3 cells (3×1)" in res["message"]
    assert "Nothing was kept" not in res["message"]
    commit.assert_not_called()
    assert fake_undo.entered == []


def test_write_formula_range_clear_refuses_unknown_range_before_any_write():
    from plugin.calc.manipulator import CellManipulator

    fake_undo = FakeUndoManager()
    doc = MagicMock()
    doc.getUndoManager.return_value = fake_undo
    ctx = SimpleNamespace(doc=doc)
    with (
        patch("plugin.calc.cells.CalcBridge", return_value=_a1_bridge()),
        patch.object(CellManipulator, "commit_write") as commit,
    ):
        res = _tool("write_formula_range").execute_safe(ctx, range=["A1:A2", "Nope"], values="")

    assert res["status"] == "error"
    assert "NOPE" in res["message"]
    assert "Nothing was kept" not in res["message"]
    commit.assert_not_called()
    assert fake_undo.entered == []


def test_write_formula_range_comma_prose_to_one_cell_ranges_is_text():
    from plugin.calc.manipulator import CellManipulator

    ctx = SimpleNamespace(doc=MagicMock())
    with (
        patch("plugin.calc.cells.CalcBridge", return_value=_a1_bridge()),
        patch.object(CellManipulator, "commit_write", return_value={"message": "ok"}) as commit,
    ):
        res = _tool("write_formula_range").execute_safe(ctx, range=["A1", "C1"], values="Hello, world")

    assert res["status"] == "ok"
    assert commit.call_count == 2
    assert [c.args[0].values for c in commit.call_args_list] == [["Hello, world"], ["Hello, world"]]

    with (
        patch("plugin.calc.cells.CalcBridge", return_value=_a1_bridge({"Note": "B2"})),
        patch.object(CellManipulator, "commit_write", return_value={"message": "ok"}) as commit_named,
    ):
        res_named = _tool("write_formula_range").execute_safe(ctx, range=["Note"], values="Hello, world")

    assert res_named["status"] == "ok"
    commit_named.assert_called_once()
    assert commit_named.call_args.args[0].values == ["Hello, world"]


def test_delete_structure_schema_start_type_is_integer():
    from plugin.calc.cells import DeleteStructure

    assert DeleteStructure.parameters is not None
    start_prop = DeleteStructure.parameters["properties"]["start"]
    assert start_prop["type"] == ["integer", "string"]
    assert "e.g. 5" in start_prop["description"]


def test_delete_structure_disposal_propagates():

    class DisposedException(Exception):
        pass

    ctx = SimpleNamespace(doc=MagicMock())
    with patch("plugin.calc.cells.CellManipulator") as manipulator_cls:
        manipulator_cls.return_value.delete_structure.side_effect = DisposedException("gone")
        tool = _tool("delete_structure")
        with pytest.raises(DisposedException):
            tool.execute(ctx, structure_type="rows", start=2, count=1)

        res = tool.execute_safe(ctx, structure_type="rows", start=2, count=1)
        assert res["status"] == "error"
        assert res["code"] == "DOCUMENT_DISPOSED"


def test_delete_structure_runtime_exception_stays_tool_error():
    class RuntimeException(Exception):
        pass

    ctx = SimpleNamespace(doc=MagicMock())
    with patch("plugin.calc.cells.CellManipulator") as manipulator_cls:
        manipulator_cls.return_value.delete_structure.side_effect = RuntimeException("bad row")
        tool = _tool("delete_structure")
        res = tool.execute(ctx, structure_type="rows", start=2, count=1)
        assert res["status"] == "error"
        assert "bad row" in res["message"]
        assert res.get("code") == "TOOL_EXECUTION_ERROR"


def test_set_cell_style_validation():
    ctx = SimpleNamespace(doc=MagicMock())
    with patch("plugin.calc.cells.CellManipulator") as manipulator_cls:
        manipulator = manipulator_cls.return_value

        res = _tool("set_style").execute_safe(ctx, range=["A1"])
        assert res["status"] == "error"
        assert "No style properties" in res["message"]
        manipulator.set_cell_style.assert_not_called()

        res_empty = _tool("set_style").execute_safe(ctx, range=["A1"], bg_color="", bold=True)
        assert res_empty["status"] == "ok"
        manipulator.set_cell_style.assert_called_once()
        assert manipulator.set_cell_style.call_args.kwargs["bg_color"] is None
        assert manipulator.set_cell_style.call_args.kwargs["bold"] is True

        manipulator.reset_mock()
        res_fmt = _tool("set_style").execute_safe(ctx, range=["A1"], number_format="#,##0.00 ")
        assert res_fmt["status"] == "ok"
        assert manipulator.set_cell_style.call_args.kwargs["number_format"] == "#,##0.00 "


def test_merge_cells_validation():
    ctx = SimpleNamespace(doc=MagicMock())
    with patch("plugin.calc.cells.CellManipulator") as manipulator_cls:
        manipulator = manipulator_cls.return_value

        res_ok = _tool("merge_cells").execute_safe(ctx, range=["A1:B1"])
        assert res_ok["status"] == "ok"
        manipulator.merge_cells.assert_called_once_with("A1:B1", center=True, unmerge=False)


def test_merge_cells_unmerge_passes_flag():

    ctx = SimpleNamespace(doc=MagicMock())
    with patch("plugin.calc.cells.CellManipulator") as manipulator_cls:
        manipulator = manipulator_cls.return_value
        res = _tool("merge_cells").execute_safe(ctx, range=["A1:B1"], unmerge=True)
        assert res["status"] == "ok"
        manipulator.merge_cells.assert_called_once_with("A1:B1", center=True, unmerge=True)


def test_sort_range_validation():
    ctx = SimpleNamespace(doc=MagicMock())
    with patch("plugin.calc.cells.CellManipulator") as manipulator_cls:
        manipulator = manipulator_cls.return_value

        res_sc_neg = _tool("range_sort").execute_safe(ctx, range=["A1:B2"], has_header=True, sort_column=-1)
        assert res_sc_neg["status"] == "error"
        assert "sort_column" in res_sc_neg["message"]
        manipulator.range_sort.assert_not_called()

        manipulator.range_sort.return_value = "Sorted"
        res_ok = _tool("range_sort").execute_safe(ctx, range=["A1:B2"], has_header=True)
        assert res_ok["status"] == "ok"
        manipulator.range_sort.assert_called_once_with("A1:B2", sort_column=0, ascending=True, has_header=True)

        manipulator.range_sort.reset_mock()
        manipulator.range_sort.side_effect = ["A done.", "B done."]
        res_multi = _tool("range_sort").execute_safe(ctx, range=["A1:B2", "D1:E2"], has_header=True)
        assert res_multi["status"] == "ok"
        assert res_multi["message"] == "A done. B done."


def test_delete_structure_validation():
    ctx = SimpleNamespace(doc=MagicMock())
    with patch("plugin.calc.cells.CellManipulator") as manipulator_cls:
        manipulator = manipulator_cls.return_value
        manipulator.delete_structure.return_value = "Deleted"

        for bad_row in ("²", "5x", 0, -1):
            res = _tool("delete_structure").execute_safe(ctx, structure_type="rows", start=bad_row)
            assert res["status"] == "error", f"Expected error for rows start={bad_row!r}"
            assert "start for rows" in res["message"]
            manipulator.delete_structure.assert_not_called()

        for good_row in ("5", 5):
            res = _tool("delete_structure").execute_safe(ctx, structure_type="rows", start=good_row)
            assert res["status"] == "ok"
            manipulator.delete_structure.assert_called_with("rows", 5, count=1, sheet=None)
        manipulator.delete_structure.reset_mock()

        for bad_col in ("3", 3):
            res = _tool("delete_structure").execute_safe(ctx, structure_type="columns", start=bad_col)
            assert res["status"] == "error", f"Expected error for columns start={bad_col!r}"
            assert "start for columns" in res["message"]
            manipulator.delete_structure.assert_not_called()

        res_c = _tool("delete_structure").execute_safe(ctx, structure_type="columns", start="c")
        assert res_c["status"] == "ok"
        manipulator.delete_structure.assert_called_with("columns", "C", count=1, sheet=None)
        manipulator.delete_structure.reset_mock()

        for bad_count in (0, -1):
            res = _tool("delete_structure").execute_safe(ctx, structure_type="rows", start=5, count=bad_count)
            assert res["status"] == "error", f"Expected error for count={bad_count!r}"
            assert "count" in res["message"]
            manipulator.delete_structure.assert_not_called()


def test_insert_structure_validation_matches_delete():
    ctx = SimpleNamespace(doc=MagicMock())
    with patch("plugin.calc.cells.CellManipulator") as manipulator_cls:
        manipulator = manipulator_cls.return_value
        manipulator.insert_structure.return_value = "Inserted"

        for bad_row in ("²", "5x", 0, -1):
            res = _tool("insert_structure").execute_safe(ctx, structure_type="rows", start=bad_row)
            assert res["status"] == "error"
            manipulator.insert_structure.assert_not_called()

        _tool("insert_structure").execute_safe(ctx, structure_type="rows", start=5)
        manipulator.insert_structure.assert_called_with("rows", 5, count=1, sheet=None)
        manipulator.insert_structure.reset_mock()

        for bad_count in (0, -1):
            res = _tool("insert_structure").execute_safe(ctx, structure_type="rows", start=5, count=bad_count)
            assert res["status"] == "error"
            manipulator.insert_structure.assert_not_called()


def test_delete_structure_sheet_prefix_passes_sheet():

    ctx = SimpleNamespace(doc=MagicMock())
    sheet = MagicMock()
    with (
        patch("plugin.calc.cells.CalcBridge") as bridge_cls,
        patch("plugin.calc.cells.CellManipulator") as manipulator_cls,
    ):
        bridge_cls.return_value.get_sheet.return_value = sheet
        manipulator = manipulator_cls.return_value
        manipulator.delete_structure.return_value = "ok"
        _tool("delete_structure").execute_safe(ctx, structure_type="rows", start="Sheet2.5")
        manipulator.delete_structure.assert_called_once_with("rows", 5, count=1, sheet=sheet)


def test_delete_structure_rejects_internal_sheet_prefix():

    ctx = SimpleNamespace(doc=MagicMock())
    with patch("plugin.calc.cells.CellManipulator") as manipulator_cls:
        manipulator = manipulator_cls.return_value
        res = _tool("delete_structure").execute_safe(ctx, structure_type="rows", start="_hidden.5")
        assert res["status"] == "error"
        assert "internal" in res["message"]
        manipulator.delete_structure.assert_not_called()


def test_delete_structure_missing_sheet_is_uno_object_error():
    from plugin.framework.errors import UnoObjectError

    ctx = SimpleNamespace(doc=MagicMock())
    with (
        patch("plugin.calc.cells.CalcBridge") as bridge_cls,
        patch("plugin.calc.cells.CellManipulator"),
    ):
        bridge_cls.return_value.get_sheet.side_effect = UnoObjectError(
            "No sheet named 'Missing'.",
            details={"available": ["Sheet1"]},
        )
        res = _tool("delete_structure").execute_safe(ctx, structure_type="rows", start="Missing.5")
    assert res["status"] == "error"
    assert res["code"] == "UNO_OBJECT_ERROR"
    assert res["details"]["available"] == ["Sheet1"]


def test_delete_structure_rejects_cell_address_as_row_start():

    ctx = SimpleNamespace(doc=MagicMock())
    with patch("plugin.calc.cells.CellManipulator") as manipulator_cls:
        manipulator = manipulator_cls.return_value
        res = _tool("delete_structure").execute_safe(ctx, structure_type="rows", start="A1")
        assert res["status"] == "error"
        manipulator.delete_structure.assert_not_called()


def test_insert_cell_html_execution_and_validation():
    from plugin.calc.cells import InsertCellHtml

    ctx = SimpleNamespace(doc=MagicMock(), ctx=MagicMock(), services=None)
    with patch("plugin.calc.rich_html.insert_cell_html_rich") as mock_rich:
        res = InsertCellHtml().execute(ctx, cell="A1", html="<b>bold</b>")
        assert res["status"] == "ok"
        mock_rich.assert_called_once_with(ctx.doc, ctx.ctx, "A1", "<b>bold</b>")

    # Empty cell address after strip
    res_empty = InsertCellHtml().execute(ctx, cell="   ", html="<b>bold</b>")
    assert res_empty["status"] == "error"
    assert "cell is required" in res_empty["message"]

    # Invalid cell address
    res_inv = InsertCellHtml().execute(ctx, cell="invalid_cell_addr", html="<b>bold</b>")
    assert res_inv["status"] == "error"
    assert "Invalid cell address" in res_inv["message"]

    # Missing required params raise KeyError
    with pytest.raises(KeyError):
        InsertCellHtml().execute(ctx, cell="A1")
    with pytest.raises(KeyError):
        InsertCellHtml().execute(ctx, html="<b>bold</b>")


@requires_asserts
def test_insert_cell_html_non_string_assertion():
    from plugin.calc.cells import InsertCellHtml

    ctx = SimpleNamespace(doc=MagicMock())
    with pytest.raises(AssertionError):
        InsertCellHtml().execute(ctx, cell="A1", html=123)


@pytest.mark.parametrize(
    ("name", "kwargs"),
    [
        ("merge_cells", {"range": ["A1:B1"], "center": "invalid"}),
        ("merge_cells", {"range": ["A1:B1"], "center": "true"}),
        ("range_sort", {"range": ["A1:B2"], "has_header": True, "ascending": "invalid"}),
        ("range_sort", {"range": ["A1:B2"], "has_header": True, "ascending": "false"}),
        ("range_sort", {"range": ["A1:B2"], "has_header": True, "sort_column": True}),
        ("range_sort", {"range": ["A1:B2"], "has_header": "true"}),
        ("range_sort", {"range": ["A1:B2"]}),
        ("delete_structure", {"structure_type": "rows", "start": 5, "count": True}),
        ("delete_structure", {"structure_type": "rows", "start": 5, "count": "2"}),
        ("delete_structure", {"structure_type": "invalid", "start": 1}),
        ("delete_structure", {"start": 1}),
        ("insert_structure", {"structure_type": "rows", "start": 5, "count": True}),
        ("insert_structure", {"structure_type": "rows", "start": 5, "count": "2"}),
        ("insert_structure", {"structure_type": "invalid", "start": 1}),
        ("insert_structure", {"start": 1}),
        ("read_cell_range", {"range": ["A1"], "format": "csv"}),
        ("read_cell_range", {"range": ["A1"], "format": "grid"}),
        ("read_cell_range", {}),
        ("write_formula_range", {"range": ["A1"], "values": True}),
        ("write_formula_range", {"range": ["A1"], "values": "1", "array": "false"}),
        ("write_formula_range", {"range": ["A1"], "source": 5}),
        ("write_formula_range", {"values": "1"}),
        ("set_style", {"range": ["A1"], "bold": "notabool"}),
        ("set_style", {"range": ["A1"], "font_size": "12"}),
        ("set_style", {"range": ["A1"], "bg_color": 255}),
        ("set_style", {"bold": True}),
    ],
)
def test_cells_schema_refuses_mistyped_args(name: str, kwargs: dict[str, Any]):
    prepared = prepare_tool_call(_tool(name), SimpleNamespace(doc_type="calc"), name, kwargs)
    assert prepared.error is not None
    assert prepared.error["code"] == "VALIDATION_ERROR"


def _missing_sheet_doc() -> MagicMock:
    doc = MagicMock()
    sheets = doc.getSheets.return_value
    sheets.hasByName.side_effect = lambda name: name in ("Sheet1", "Data")
    sheets.getElementNames.return_value = ("Sheet1", "Data")
    sheets.getCount.return_value = 0
    doc.NamedRanges.hasByName.return_value = False
    active = doc.getCurrentController.return_value.getActiveSheet.return_value
    active.NamedRanges.hasByName.return_value = False
    return doc


def test_write_formula_range_missing_sheet_is_uno_object_error():
    doc = _missing_sheet_doc()
    fake_undo = FakeUndoManager()
    doc.getUndoManager.return_value = fake_undo
    ctx = SimpleNamespace(doc=doc)
    res = _tool("write_formula_range").execute_safe(ctx, range="Nope.A1", values="1")

    assert res["status"] == "error"
    assert res["code"] == "UNO_OBJECT_ERROR"
    assert "Available: Sheet1, Data" in res["message"]
    assert "sheet_list" in res["details"]["hint"]
    assert fake_undo.entered == []


def test_mutate_ranges_keeps_exception_code_after_rollback():
    from plugin.framework.errors import UnoObjectError

    fake_undo = FakeUndoManager()
    doc = MagicMock()
    doc.getUndoManager.return_value = fake_undo
    ctx = SimpleNamespace(doc=doc)

    with patch("plugin.calc.cells.CellManipulator") as manipulator_cls:
        manipulator = manipulator_cls.return_value
        manipulator.commit_write.side_effect = [
            {"message": "wrote"},
            UnoObjectError("No sheet named 'B'. Available: Sheet1", details={"hint": "h"}),
        ]

        res = _tool("write_formula_range").execute_safe(ctx, range=["A1", "B1"], values="1")

    assert res["status"] == "error"
    assert res["code"] == "UNO_OBJECT_ERROR"
    assert res["message"].endswith("Nothing was kept.")
    assert res["details"]["hint"] == "h"
    assert fake_undo.undone == 1


def test_mutate_ranges_rolls_back_at_full_undo_stack():
    from plugin.calc.cells import WriteCellRange
    from plugin.framework.errors import CalcError

    fake_undo = FakeUndoManager(titles=["x"] * 3, limit=3)
    doc = MagicMock()
    doc.getUndoManager.return_value = fake_undo
    ctx = SimpleNamespace(doc=doc)

    with patch("plugin.calc.cells.CellManipulator") as manipulator_cls:
        manipulator = manipulator_cls.return_value
        manipulator.commit_write.side_effect = ["wrote", CalcError("occupied")]

        res = WriteCellRange().execute(ctx, range=["A1", "B1"], values="1")

    assert res["status"] == "error"
    assert "Nothing was kept." in res["message"]
    assert fake_undo.undone == 1


def test_mutate_ranges_empty_group_keeps_older_same_title():
    from plugin.calc.cells import WriteCellRange
    from plugin.framework.errors import CalcError

    fake_undo = FakeUndoManager(titles=["WriterAgent: Write formulas"], drop_empty=True)
    doc = MagicMock()
    doc.getUndoManager.return_value = fake_undo
    ctx = SimpleNamespace(doc=doc)

    with patch("plugin.calc.cells.CellManipulator") as manipulator_cls:
        manipulator_cls.return_value.commit_write.side_effect = CalcError("fail")
        res = WriteCellRange().execute(ctx, range=["A1"], values="1")

    assert res["status"] == "error"
    assert "Nothing was kept." not in res["message"]
    assert fake_undo.undone == 0
    assert fake_undo.titles == ["WriterAgent: Write formulas"]


def test_read_cell_range_missing_sheet_is_uno_object_error():
    doc = _missing_sheet_doc()
    ctx = SimpleNamespace(doc=doc)
    res = _tool("read_cell_range").execute_safe(ctx, range="Nope.A1")

    assert res["status"] == "error"
    assert res["code"] == "UNO_OBJECT_ERROR"
    assert res["range"] == "Nope.A1"
    assert "Available: Sheet1, Data" in res["message"]
    assert "sheet_list" in res["details"]["hint"]


def test_cells_registry_refuses_bad_args_as_invalid_param():
    ctx = SimpleNamespace(doc=MagicMock())
    with patch("plugin.calc.cells.CellManipulator") as mock_manip:
        res_write = _tool("write_formula_range").execute_safe(ctx, range=["A1"])
    assert res_write["status"] == "error"
    assert res_write["code"] == "INVALID_PARAM"
    mock_manip.return_value.plan_write.assert_not_called()

