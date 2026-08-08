# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
# Copyright (c) 2026 LibreCalc AI Assistant (Calc integration features, originally MIT)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

from plugin.framework.uno_context import get_desktop
from plugin.testing_runner import setup, teardown, native_test

_test_doc = None
_test_ctx = None

@setup
def setup_calc_tests(ctx):
    global _test_doc, _test_ctx
    _test_ctx = ctx
    desktop = get_desktop(ctx)
    import uno
    hidden_prop = uno.createUnoStruct(
        "com.sun.star.beans.PropertyValue",
        Name="Hidden",
        Value=True,
    )
    _test_doc = desktop.loadComponentFromURL("private:factory/scalc", "_blank", 0, (hidden_prop,))

@teardown
def teardown_calc_tests(ctx):
    global _test_doc, _test_ctx
    if _test_doc:
        _test_doc.close(True)
    _test_doc = None
    _test_ctx = None

def _execute_calc_tool(name, args, doc=None):
    from plugin.main import get_tools, get_services
    from plugin.framework.tool import ToolContext
    # Pass suite bootstrap ctx (same as setup_calc_tests); None makes
    # get_desktop() use uno.getComponentContext() and can segfault under
    # python -m plugin.testing_runner.
    tctx = ToolContext(doc if doc is not None else _test_doc, _test_ctx, "calc", get_services(), "test")
    try:
        res = get_tools().execute(name, tctx, **args)
    except (KeyError, ValueError) as e:
        res = {"status": "error", "error": str(e)}
    return res


@native_test
def test_set_cell_style_and_details():
    active_sheet = _test_doc.getCurrentController().getActiveSheet()
    _execute_calc_tool("set_style", {"range_name": "A1", "bold": True, "bg_color": "yellow"})
    cell = active_sheet.getCellByPosition(0, 0)
    from com.sun.star.awt.FontWeight import BOLD
    assert cell.getPropertyValue("CharWeight") == BOLD, "Bold not set"
    assert cell.getPropertyValue("CellBackColor") == 0xFFFF00, "Background color not set"

    from plugin.calc.bridge import CalcBridge
    from plugin.calc.inspector import CellInspector
    b = CalcBridge(_test_doc)
    insp = CellInspector(b)
    details = insp.get_cell_details("A1")

    assert details.get("background_color") == 0xFFFF00, f"Details readback bg color failed: {details}"
    assert details.get("bold") == BOLD, f"Details readback bold failed: {details}"


@native_test
def test_merge_cells():
    active_sheet = _test_doc.getCurrentController().getActiveSheet()
    _execute_calc_tool("merge_cells", {"range_name": ["C1:D1", "E1:F1"]})
    rng1 = active_sheet.getCellRangeByPosition(2, 0, 3, 0)
    rng2 = active_sheet.getCellRangeByPosition(4, 0, 5, 0)
    assert rng1.getIsMerged(), "C1:D1 not merged"
    assert rng2.getIsMerged(), "E1:F1 not merged"


@native_test
def test_clear_range():
    active_sheet = _test_doc.getCurrentController().getActiveSheet()
    active_sheet.getCellByPosition(6, 0).setString("ClearMe")
    active_sheet.getCellByPosition(7, 0).setString("ClearMe")
    _execute_calc_tool("write_formula_range", {"range_name": ["G1", "H1"], "formula_or_values": ""})
    assert active_sheet.getCellByPosition(6, 0).getString() == "", "G1 not cleared"
    assert active_sheet.getCellByPosition(7, 0).getString() == "", "H1 not cleared"


@native_test
def test_read_cell_range():
    active_sheet = _test_doc.getCurrentController().getActiveSheet()

    # Populate a 3x3 grid (A1:C3)
    # Row 1: Strings
    active_sheet.getCellByPosition(0, 0).setString("Col1")
    active_sheet.getCellByPosition(1, 0).setString("Col2")
    active_sheet.getCellByPosition(2, 0).setString("Col3")

    # Row 2: Numbers
    active_sheet.getCellByPosition(0, 1).setValue(1.0)
    active_sheet.getCellByPosition(1, 1).setValue(2.5)
    active_sheet.getCellByPosition(2, 1).setValue(3.14)

    # Row 3: Mixed (String, Empty, Formula)
    active_sheet.getCellByPosition(0, 2).setString("End")
    # Leave B3 empty
    active_sheet.getCellByPosition(2, 2).setFormula("=A2+B2")

    res = _execute_calc_tool("read_cell_range", {"range_name": ["A1:C3"]})
    assert res.get("status") == "ok", f"read_cell_range failed: {res}"

    result_data = res.get("result", [])
    assert len(result_data) == 1, "Expected list of 1 result for 1 range"

    grid = result_data[0]
    assert len(grid) == 3, "Expected 3 rows"
    assert len(grid[0]) == 3, "Expected 3 columns per row"

    # Check Row 1
    assert grid[0][0]["value"] == "Col1"
    assert grid[0][1]["value"] == "Col2"
    assert grid[0][2]["value"] == "Col3"

    # Check Row 2
    assert grid[1][0]["value"] == 1.0
    assert grid[1][1]["value"] == 2.5
    assert grid[1][2]["value"] == 3.14

    # Check Row 3
    assert grid[2][0]["value"] == "End"
    assert grid[2][1]["value"] is None
    # Formula value depends on evaluation but formula property should be set
    assert grid[2][2]["formula"] == "=A2+B2"


def _set_number_format(cell, format_str: str) -> None:
    formats = _test_doc.getNumberFormats()
    locale = _test_doc.getPropertyValue("CharLocale")
    format_id = formats.queryKey(format_str, locale, False)
    if format_id == -1:
        format_id = formats.addNew(format_str, locale)
    cell.setPropertyValue("NumberFormat", format_id)


@native_test
def test_read_cell_range_date_time_enrichment():
    """Public read_cell_range puts ISO in value; internal raw path keeps serials."""
    active_sheet = _test_doc.getCurrentController().getActiveSheet()

    date_cell = active_sheet.getCellByPosition(0, 20)  # A21
    date_cell.setValue(46240.0)
    _set_number_format(date_cell, "YYYY-MM-DD")

    time_cell = active_sheet.getCellByPosition(1, 20)  # B21
    time_cell.setValue(0.5)
    _set_number_format(time_cell, "HH:MM:SS")

    datetime_cell = active_sheet.getCellByPosition(2, 20)  # C21
    datetime_cell.setValue(46240.5)
    _set_number_format(datetime_cell, "YYYY-MM-DD HH:MM:SS")

    formula_cell = active_sheet.getCellByPosition(3, 20)  # D21
    formula_cell.setFormula("=A21")
    _set_number_format(formula_cell, "YYYY-MM-DD")

    plain_cell = active_sheet.getCellByPosition(4, 20)  # E21
    plain_cell.setValue(42.0)

    res = _execute_calc_tool("read_cell_range", {"range_name": ["A21:E21"]})
    assert res.get("status") == "ok", f"read_cell_range failed: {res}"
    row = res["result"][0][0]

    assert row[0]["value"] == "2026-08-06"
    assert row[0]["type"] == "date"
    assert row[0]["format_category"] == "date"
    assert "iso8601" not in row[0]

    assert row[1]["value"] == "12:00:00"
    assert row[1]["type"] == "time"
    assert row[1]["format_category"] == "time"

    assert row[2]["value"] == "2026-08-06T12:00:00"
    assert row[2]["type"] == "datetime"
    assert row[2]["format_category"] == "datetime"

    assert row[3]["formula"] == "=A21"
    assert row[3]["value"] == "2026-08-06"
    assert row[3]["type"] == "date"
    assert row[3]["format_category"] == "date"

    assert row[4]["value"] == 42.0
    assert row[4]["type"] == "value"
    assert "format_category" not in row[4]

    # Internal default path must stay raw for =PY / analysis consumers.
    from plugin.calc.bridge import CalcBridge
    from plugin.calc.inspector import CellInspector

    raw = CellInspector(CalcBridge(_test_doc)).read_range("A21")
    assert raw[0][0]["value"] == 46240.0
    assert raw[0][0]["type"] == "value"
    assert "format_category" not in raw[0][0]


@native_test
def test_read_range_format_info_performance():
    """Opt-in enrichment must stay cheap for plain numbers and scale with format groups."""
    import time

    from plugin.calc.bridge import CalcBridge
    from plugin.calc.inspector import CellInspector

    active_sheet = _test_doc.getCurrentController().getActiveSheet()
    inspector = CellInspector(CalcBridge(_test_doc))

    def _avg_ms(addr: str, *, include_format_info: bool, rounds: int = 3) -> float:
        t0 = time.perf_counter()
        for _ in range(rounds):
            inspector.read_range(addr, include_format_info=include_format_info)
        return (time.perf_counter() - t0) * 1000.0 / rounds

    # 100x100 = 10_000 plain numbers starting at AA1 (col 26).
    plain = active_sheet.getCellRangeByPosition(26, 0, 125, 99)
    plain.setDataArray(tuple(tuple(float(r * 100 + c) for c in range(100)) for r in range(100)))

    # Warm the UNO path (first reads after sheet fill can be anomalously slow).
    for _ in range(3):
        inspector.read_range("AA1:DV100")
        inspector.read_range("AA1:DV100", include_format_info=True)
    raw_ms = _avg_ms("AA1:DV100", include_format_info=False)
    plain_enriched_ms = _avg_ms("AA1:DV100", include_format_info=True)

    # One shared date format on a same-sized block (worst case: every cell is a date).
    date_rng = active_sheet.getCellRangeByPosition(26, 110, 125, 209)
    date_rng.setDataArray(tuple(tuple(46200.0 + r for _c in range(100)) for r in range(100)))
    _set_number_format(date_rng, "YYYY-MM-DD")
    sample = inspector.read_range("AA111:DV210", include_format_info=True)
    assert sample[0][0].get("format_category") == "date", sample[0][0]
    date_enriched_ms = _avg_ms("AA111:DV210", include_format_info=True)

    # Realistic mix: 10 of 100 columns (~10% of cells) are dates; the rest stay plain numbers.
    # Layout at FA1:IV100 (col 156..255). First 10 columns (FA:FJ) get date serials + date format.
    mixed = active_sheet.getCellRangeByPosition(156, 0, 255, 99)
    mixed_rows = []
    for r in range(100):
        row = []
        for c in range(100):
            row.append(46200.0 + r if c < 10 else float(r * 100 + c))
        mixed_rows.append(tuple(row))
    mixed.setDataArray(tuple(mixed_rows))
    date_cols = active_sheet.getCellRangeByPosition(156, 0, 165, 99)
    _set_number_format(date_cols, "YYYY-MM-DD")

    mixed_sample = inspector.read_range("FA1:IV100", include_format_info=True)
    date_hits = sum(1 for row in mixed_sample for cell in row if cell.get("format_category") == "date")
    plain_hits = sum(1 for row in mixed_sample for cell in row if "format_category" not in cell)
    assert date_hits == 1000, f"expected 10% date cells (1000), got {date_hits}"
    assert plain_hits == 9000, f"expected 90% plain cells (9000), got {plain_hits}"
    assert isinstance(mixed_sample[0][0].get("value"), str) and mixed_sample[0][0].get("type") == "date"
    assert isinstance(mixed_sample[0][10].get("value"), float)
    assert "format_category" not in mixed_sample[0][10]

    mixed_enriched_ms = _avg_ms("FA1:IV100", include_format_info=True)

    print(
        f"[read_range perf] raw={raw_ms:.1f}ms plain_enriched={plain_enriched_ms:.1f}ms "
        f"mixed_10pct_dates={mixed_enriched_ms:.1f}ms all_dates={date_enriched_ms:.1f}ms "
        f"(avg of 3 over 10k cells)"
    )

    # Plain-number enrichment should stay near the raw path (preflight finds no date cells
    # and no formulas). Allow generous headroom for CI variance without accepting a collapse.
    assert plain_enriched_ms < max(raw_ms * 3.0, raw_ms + 50.0), (
        f"plain enriched too slow: raw={raw_ms:.1f}ms enriched={plain_enriched_ms:.1f}ms"
    )
    # Mixed 10% dates should land between plain and all-dates, not near the all-dates worst case.
    assert mixed_enriched_ms < max(raw_ms * 6.0, 400.0), (
        f"mixed 10% dates too slow: raw={raw_ms:.1f}ms mixed={mixed_enriched_ms:.1f}ms"
    )
    assert date_enriched_ms < max(raw_ms * 8.0, 500.0), (
        f"all-dates too slow: raw={raw_ms:.1f}ms all_dates={date_enriched_ms:.1f}ms"
    )


@native_test
def test_read_after_write_stability():
    # 1. Write data
    res_write = _execute_calc_tool("write_formula_range", {"range_name": "Z1:Z2", "formula_or_values": [["Apple"], ["Banana"]]})
    assert res_write.get("status") == "ok", f"write_formula_range failed: {res_write}"

    # 2. Read back
    res_read = _execute_calc_tool("read_cell_range", {"range_name": "Z1:Z2"})
    assert res_read.get("status") == "ok", f"read_cell_range failed: {res_read}"
    grid = res_read.get("result", [])[0]
    assert grid[0][0]["value"] == "Apple", f"Expected Apple, got {grid[0][0]['value']}"
    assert grid[1][0]["value"] == "Banana", f"Expected Banana, got {grid[1][0]['value']}"

    # 3. Merge and read back
    res_merge = _execute_calc_tool("merge_cells", {"range_name": "Z1:Z2"})
    assert res_merge.get("status") == "ok", f"merge_cells failed: {res_merge}"
    res_read_merged = _execute_calc_tool("read_cell_range", {"range_name": "Z1:Z2"})
    assert res_read_merged.get("status") == "ok", f"read_cell_range after merge failed: {res_read_merged}"
    grid_merged = res_read_merged.get("result", [])[0]
    # In LibreOffice, the top-left cell of a merged range keeps the value
    assert grid_merged[0][0]["value"] == "Apple", f"Expected Apple in merged range, got {grid_merged[0][0]['value']}"

    # 4. Clear range and search
    res_clear = _execute_calc_tool("write_formula_range", {"range_name": "Z1:Z2", "formula_or_values": ""})
    assert res_clear.get("status") == "ok", f"write_formula_range clear failed: {res_clear}"
    res_search = _execute_calc_tool("search_in_spreadsheet", {"pattern": "Apple"})
    assert res_search.get("status") == "ok", f"search_in_spreadsheet failed: {res_search}"
    # Filter matches to only check Z column to avoid false positives from other tests
    z_matches = [m for m in res_search.get("matches", []) if m.get("cell", "").startswith("Z")]
    assert len(z_matches) == 0, f"Expected 0 matches for Apple in Z column, found {len(z_matches)}"


@native_test
def test_elapsed_time_over_24h_reads_as_duration():
    """§3.2: 1.25 under [HH]:MM:SS becomes PT30H, not 06:00:00."""
    active_sheet = _test_doc.getCurrentController().getActiveSheet()
    cell = active_sheet.getCellByPosition(0, 30)  # A31
    cell.setValue(1.25)
    _set_number_format(cell, "[HH]:MM:SS")

    res = _execute_calc_tool("read_cell_range", {"range_name": ["A31"]})
    assert res.get("status") == "ok", res
    info = res["result"][0][0][0]
    assert info["value"] == "PT30H", f"expected duration wire, got {info}"
    assert info["type"] == "duration"
    assert info["format_category"] == "duration"


@native_test
def test_write_and_read_date_time_cells():
    res = _execute_calc_tool(
        "write_formula_range",
        {"range_name": ["A26:B26"], "formula_or_values": '["2026-08-08", "08:00"]'},
    )
    assert res.get("status") == "ok", res
    msg = res.get("message", "")
    assert "1 date" in msg and "1 time" in msg, msg

    read_res = _execute_calc_tool("read_cell_range", {"range_name": ["A26:B26"]})
    assert read_res.get("status") == "ok", read_res
    row = read_res["result"][0][0]

    assert row[0]["value"] == "2026-08-08"
    assert row[0]["type"] == "date"
    assert row[0]["format_category"] == "date"

    assert row[1]["value"] == "08:00:00"
    assert row[1]["type"] == "time"
    assert row[1]["format_category"] == "time"


@native_test
def test_write_iso_mixed_with_formula_same_as_constants():
    """Phase 2: constants use setDataArray even when a formula is in the range."""
    res = _execute_calc_tool(
        "write_formula_range",
        {"range_name": ["A32:C32"], "formula_or_values": '["2026-08-08", "08:00", "=A32+1"]'},
    )
    assert res.get("status") == "ok", res
    msg = res.get("message", "")
    assert "1 date" in msg and "1 time" in msg and "1 formula" in msg, msg

    read_res = _execute_calc_tool("read_cell_range", {"range_name": ["A32:C32"]})
    row = read_res["result"][0][0]
    assert row[0]["value"] == "2026-08-08" and row[0]["type"] == "date"
    assert row[1]["value"] == "08:00:00" and row[1]["type"] == "time"
    assert row[2]["formula"] == "=A32+1"
    # S24: no format apply on formula cells — value may stay a raw serial under General.
    assert row[2]["value"] in ("2026-08-09", 46243.0) or (
        isinstance(row[2]["value"], float) and abs(row[2]["value"] - 46243.0) < 1e-6
    )


@native_test
def test_write_preserves_compatible_date_format():
    active_sheet = _test_doc.getCurrentController().getActiveSheet()
    cell = active_sheet.getCellByPosition(0, 33)  # A34
    cell.setValue(0)
    _set_number_format(cell, "MM/DD/YYYY")
    prior_key = int(cell.getPropertyValue("NumberFormat"))

    res = _execute_calc_tool(
        "write_formula_range",
        {"range_name": ["A34"], "formula_or_values": "2026-08-08"},
    )
    assert res.get("status") == "ok", res
    assert int(cell.getPropertyValue("NumberFormat")) == prior_key

    read_res = _execute_calc_tool("read_cell_range", {"range_name": ["A34"]})
    info = read_res["result"][0][0][0]
    assert info["type"] == "date"
    assert info["value"] == "2026-08-08"


@native_test
def test_write_time_preserves_elapsed_format():
    active_sheet = _test_doc.getCurrentController().getActiveSheet()
    cell = active_sheet.getCellByPosition(1, 33)  # B34
    _set_number_format(cell, "[HH]:MM:SS")
    prior_key = int(cell.getPropertyValue("NumberFormat"))

    res = _execute_calc_tool(
        "write_formula_range",
        {"range_name": ["B34"], "formula_or_values": "08:00"},
    )
    assert res.get("status") == "ok", res
    assert int(cell.getPropertyValue("NumberFormat")) == prior_key


@native_test
def test_write_iso_into_text_format_applies_temporal():
    active_sheet = _test_doc.getCurrentController().getActiveSheet()
    cell = active_sheet.getCellByPosition(2, 33)  # C34
    _set_number_format(cell, "@")

    res = _execute_calc_tool(
        "write_formula_range",
        {"range_name": ["C34"], "formula_or_values": "2026-08-08"},
    )
    assert res.get("status") == "ok", res
    read_res = _execute_calc_tool("read_cell_range", {"range_name": ["C34"]})
    info = read_res["result"][0][0][0]
    assert info["value"] == "2026-08-08"
    assert info["type"] == "date"


@native_test
def test_write_iso_date_time_with_guarded_doc():
    """Chat passes guard_uno(doc); NumberFormatter attach must unwrap or ISO writes fail."""
    from plugin.framework.thread_guard import guard_uno

    active_sheet = _test_doc.getCurrentController().getActiveSheet()
    for col in (0, 1):
        active_sheet.getCellByPosition(col, 35).setPropertyValue("NumberFormat", 0)  # A36:B36

    guarded = guard_uno(_test_doc)
    res = _execute_calc_tool(
        "write_formula_range",
        {"range_name": ["A36:B36"], "formula_or_values": '["2026-08-08", "08:00"]'},
        doc=guarded,
    )
    assert res.get("status") == "ok", res

    read_res = _execute_calc_tool("read_cell_range", {"range_name": ["A36:B36"]}, doc=guarded)
    row = read_res["result"][0][0]
    assert row[0]["value"] == "2026-08-08" and row[0]["type"] == "date", row[0]
    assert row[1]["value"] == "08:00:00" and row[1]["type"] == "time", row[1]


@native_test
def test_write_apostrophe_forces_text_keeps_at():
    active_sheet = _test_doc.getCurrentController().getActiveSheet()
    cell = active_sheet.getCellByPosition(3, 33)  # D34
    _set_number_format(cell, "YYYY-MM-DD")

    res = _execute_calc_tool(
        "write_formula_range",
        {"range_name": ["D34"], "formula_or_values": "'2026-08-08"},
    )
    assert res.get("status") == "ok", res
    assert cell.getString() == "2026-08-08"
    # Text format @
    formats = _test_doc.getNumberFormats()
    props = formats.getByKey(int(cell.getPropertyValue("NumberFormat")))
    assert props.getPropertyValue("FormatString") == "@"


@native_test
def test_write_ordinary_text_restores_prior_format():
    active_sheet = _test_doc.getCurrentController().getActiveSheet()
    cell = active_sheet.getCellByPosition(4, 33)  # E34
    _set_number_format(cell, "YYYY-MM-DD")
    prior_key = int(cell.getPropertyValue("NumberFormat"))

    res = _execute_calc_tool(
        "write_formula_range",
        {"range_name": ["E34"], "formula_or_values": "08/05/2026"},
    )
    assert res.get("status") == "ok", res
    assert cell.getString() == "08/05/2026"
    assert int(cell.getPropertyValue("NumberFormat")) == prior_key


@native_test
def test_write_idempotent_second_iso_keeps_format():
    active_sheet = _test_doc.getCurrentController().getActiveSheet()
    cell = active_sheet.getCellByPosition(5, 33)  # F34
    cell.setPropertyValue("NumberFormat", 0)

    _execute_calc_tool("write_formula_range", {"range_name": ["F34"], "formula_or_values": "2026-08-08"})
    key_after_first = int(cell.getPropertyValue("NumberFormat"))
    assert key_after_first != 0

    _execute_calc_tool("write_formula_range", {"range_name": ["F34"], "formula_or_values": "2026-08-08"})
    assert int(cell.getPropertyValue("NumberFormat")) == key_after_first


@native_test
def test_write_midnight_datetime_preserves_date_format():
    """S15: midnight datetime into a date cell keeps the destination format."""
    active_sheet = _test_doc.getCurrentController().getActiveSheet()
    cell = active_sheet.getCellByPosition(0, 34)  # A35
    cell.setValue(0)
    _set_number_format(cell, "MM/DD/YYYY")
    prior_key = int(cell.getPropertyValue("NumberFormat"))

    res = _execute_calc_tool(
        "write_formula_range",
        {"range_name": ["A35"], "formula_or_values": "2026-08-08T00:00:00"},
    )
    assert res.get("status") == "ok", res
    assert "1 datetime" in res.get("message", ""), res.get("message")
    assert int(cell.getPropertyValue("NumberFormat")) == prior_key

    read_res = _execute_calc_tool("read_cell_range", {"range_name": ["A35"]})
    info = read_res["result"][0][0][0]
    assert info["type"] == "date"
    assert info["value"] == "2026-08-08"


@native_test
def test_write_non_midnight_datetime_applies_into_date_format():
    """S15: non-midnight datetime into a date cell applies a datetime format."""
    active_sheet = _test_doc.getCurrentController().getActiveSheet()
    cell = active_sheet.getCellByPosition(1, 34)  # B35
    cell.setValue(0)
    _set_number_format(cell, "MM/DD/YYYY")
    prior_key = int(cell.getPropertyValue("NumberFormat"))

    res = _execute_calc_tool(
        "write_formula_range",
        {"range_name": ["B35"], "formula_or_values": "2026-08-08T08:00:00"},
    )
    assert res.get("status") == "ok", res
    assert "1 datetime" in res.get("message", ""), res.get("message")
    assert int(cell.getPropertyValue("NumberFormat")) != prior_key

    read_res = _execute_calc_tool("read_cell_range", {"range_name": ["B35"]})
    info = read_res["result"][0][0][0]
    assert info["type"] == "datetime"
    assert info["value"] == "2026-08-08T08:00:00"


@native_test
def test_write_empty_cell_does_not_bridge_disagreeing_format_runs():
    """S25: apply | empty | preserve must not bridge formats across the empty cell."""
    active_sheet = _test_doc.getCurrentController().getActiveSheet()
    left = active_sheet.getCellByPosition(0, 35)  # A36
    mid = active_sheet.getCellByPosition(1, 35)  # B36
    right = active_sheet.getCellByPosition(2, 35)  # C36
    left.setPropertyValue("NumberFormat", 0)
    mid.setPropertyValue("NumberFormat", 0)
    right.setValue(0)
    _set_number_format(right, "MM/DD/YYYY")
    right_prior = int(right.getPropertyValue("NumberFormat"))

    res = _execute_calc_tool(
        "write_formula_range",
        {"range_name": ["A36:C36"], "formula_or_values": '["2026-08-08", "", "2026-08-09"]'},
    )
    assert res.get("status") == "ok", res
    assert "2 dates" in res.get("message", ""), res.get("message")

    left_key = int(left.getPropertyValue("NumberFormat"))
    mid_key = int(mid.getPropertyValue("NumberFormat"))
    right_key = int(right.getPropertyValue("NumberFormat"))
    assert left_key != 0, "left General cell should receive an applied date format"
    assert right_key == right_prior, "right compatible date format must be preserved"
    # Empty stays General: disagreeing neighbors (apply vs preserve) do not bridge.
    assert mid_key == 0, f"empty cell format bridged unexpectedly: {mid_key}"


@native_test
def test_write_invalid_calendar_day_falls_back_to_text_with_s29_restore():
    """Gate accepts 2026-02-30 shape; Calc NotNumericException → text + S29 restore."""
    active_sheet = _test_doc.getCurrentController().getActiveSheet()
    cell = active_sheet.getCellByPosition(0, 36)  # A37
    _set_number_format(cell, "YYYY-MM-DD")
    prior_key = int(cell.getPropertyValue("NumberFormat"))

    res = _execute_calc_tool(
        "write_formula_range",
        {"range_name": ["A37"], "formula_or_values": "2026-02-30"},
    )
    assert res.get("status") == "ok", res
    msg = res.get("message", "")
    assert "1 text" in msg, msg
    assert "1 date" not in msg and "dates" not in msg, msg
    assert cell.getString() == "2026-02-30"
    assert int(cell.getPropertyValue("NumberFormat")) == prior_key


@native_test
def test_write_read_iso_round_trip_with_non_default_null_date():
    """Wire ISO is stable when document NullDate is not the Calc default."""
    import uno

    settings = _test_doc.getNumberFormatSettings()
    old_null = settings.getPropertyValue("NullDate")
    try:
        nd = uno.createUnoStruct("com.sun.star.util.Date")
        nd.Year, nd.Month, nd.Day = 1904, 1, 1
        settings.setPropertyValue("NullDate", nd)

        res = _execute_calc_tool(
            "write_formula_range",
            {"range_name": ["A38"], "formula_or_values": "2026-08-08"},
        )
        assert res.get("status") == "ok", res
        assert "1 date" in res.get("message", ""), res.get("message")

        active_sheet = _test_doc.getCurrentController().getActiveSheet()
        serial = active_sheet.getCellByPosition(0, 37).getValue()  # A38
        # Under NullDate 1904-01-01, 2026-08-08 is 44780 (46242 − 1462).
        assert abs(serial - 44780.0) < 1e-6, f"expected 1904-epoch serial, got {serial}"

        read_res = _execute_calc_tool("read_cell_range", {"range_name": ["A38"]})
        info = read_res["result"][0][0][0]
        assert info["value"] == "2026-08-08"
        assert info["type"] == "date"
        assert info["format_category"] == "date"
    finally:
        settings.setPropertyValue("NullDate", old_null)


@native_test
def test_write_iso_date_column_formats_all_cells():
    """Vertical merge correctness: homogeneous ISO column still enriches every cell."""
    res = _execute_calc_tool(
        "write_formula_range",
        {"range_name": ["A40:A42"], "formula_or_values": '["2026-08-08", "2026-08-09", "2026-08-10"]'},
    )
    assert res.get("status") == "ok", res
    assert "3 dates" in res.get("message", ""), res.get("message")

    read_res = _execute_calc_tool("read_cell_range", {"range_name": ["A40:A42"]})
    assert read_res.get("status") == "ok", read_res
    col = [row[0] for row in read_res["result"][0]]
    assert [c["value"] for c in col] == ["2026-08-08", "2026-08-09", "2026-08-10"]
    assert all(c["type"] == "date" and c["format_category"] == "date" for c in col)


@native_test
def test_write_and_read_duration_pt30h():
    """PT30H into General → duration serial + elapsed format; read back PT30H."""
    from plugin.calc.datetime_wire import is_elapsed_format_string

    res = _execute_calc_tool(
        "write_formula_range",
        {"range_name": ["A43"], "formula_or_values": "PT30H"},
    )
    assert res.get("status") == "ok", res
    assert "1 duration" in res.get("message", ""), res.get("message")

    active_sheet = _test_doc.getCurrentController().getActiveSheet()
    cell = active_sheet.getCellByPosition(0, 42)  # A43
    assert abs(cell.getValue() - 1.25) < 1e-9
    formats = _test_doc.getNumberFormats()
    props = formats.getByKey(int(cell.getPropertyValue("NumberFormat")))
    assert is_elapsed_format_string(props.getPropertyValue("FormatString"))

    read_res = _execute_calc_tool("read_cell_range", {"range_name": ["A43"]})
    info = read_res["result"][0][0][0]
    assert info["value"] == "PT30H"
    assert info["type"] == "duration"
    assert info["format_category"] == "duration"


@native_test
def test_write_duration_preserves_elapsed_format():
    """Duration into an elapsed column keeps the destination format (S16)."""
    active_sheet = _test_doc.getCurrentController().getActiveSheet()
    cell = active_sheet.getCellByPosition(1, 42)  # B43
    _set_number_format(cell, "[HH]:MM:SS")
    prior_key = int(cell.getPropertyValue("NumberFormat"))

    res = _execute_calc_tool(
        "write_formula_range",
        {"range_name": ["B43"], "formula_or_values": "PT8H"},
    )
    assert res.get("status") == "ok", res
    assert "1 duration" in res.get("message", ""), res.get("message")
    assert int(cell.getPropertyValue("NumberFormat")) == prior_key
    assert abs(cell.getValue() - (8.0 / 24.0)) < 1e-9


@native_test
def test_write_and_read_duration_pt1h30m():
    """Multi-component PT1H30M round-trips as duration wire, not clock time."""
    res = _execute_calc_tool(
        "write_formula_range",
        {"range_name": ["C43"], "formula_or_values": "PT1H30M"},
    )
    assert res.get("status") == "ok", res
    assert "1 duration" in res.get("message", ""), res.get("message")

    active_sheet = _test_doc.getCurrentController().getActiveSheet()
    cell = active_sheet.getCellByPosition(2, 42)  # C43
    assert abs(cell.getValue() - (1.5 / 24.0)) < 1e-9

    read_res = _execute_calc_tool("read_cell_range", {"range_name": ["C43"]})
    info = read_res["result"][0][0][0]
    assert info["value"] == "PT1H30M"
    assert info["type"] == "duration"
    assert info["format_category"] == "duration"


@native_test
def test_write_inherits_column_date_format():
    """P1: Write ISO date into empty row below formatted column inherits format."""
    active_sheet = _test_doc.getCurrentController().getActiveSheet()
    cell_a50 = active_sheet.getCellByPosition(0, 49)  # A50
    _set_number_format(cell_a50, "MM/DD/YYYY")
    cell_a50.setValue(46242.0)
    expected_key = int(cell_a50.getPropertyValue("NumberFormat"))

    res = _execute_calc_tool(
        "write_formula_range",
        {"range_name": ["A51"], "formula_or_values": "2026-08-08"},
    )
    assert res.get("status") == "ok", res
    cell_a51 = active_sheet.getCellByPosition(0, 50)  # A51
    assert int(cell_a51.getPropertyValue("NumberFormat")) == expected_key


@native_test
def test_write_inherits_column_format_with_empty_gap():
    """P1: Upward template scan skips empty rows to find nearest compatible format."""
    active_sheet = _test_doc.getCurrentController().getActiveSheet()
    cell_b50 = active_sheet.getCellByPosition(1, 49)  # B50
    _set_number_format(cell_b50, "MM/DD/YYYY")
    cell_b50.setValue(46242.0)
    expected_key = int(cell_b50.getPropertyValue("NumberFormat"))

    res = _execute_calc_tool(
        "write_formula_range",
        {"range_name": ["B52"], "formula_or_values": "2026-08-08"},
    )
    assert res.get("status") == "ok", res
    cell_b52 = active_sheet.getCellByPosition(1, 51)  # B52
    assert int(cell_b52.getPropertyValue("NumberFormat")) == expected_key


@native_test
def test_write_time_does_not_inherit_incompatible_date_format():
    """P1: Incompatible template (date column for time write) is not inherited."""
    active_sheet = _test_doc.getCurrentController().getActiveSheet()
    cell_c50 = active_sheet.getCellByPosition(2, 49)  # C50
    _set_number_format(cell_c50, "MM/DD/YYYY")
    cell_c50.setValue(46242.0)
    date_key = int(cell_c50.getPropertyValue("NumberFormat"))

    res = _execute_calc_tool(
        "write_formula_range",
        {"range_name": ["C51"], "formula_or_values": "08:00"},
    )
    assert res.get("status") == "ok", res
    cell_c51 = active_sheet.getCellByPosition(2, 50)  # C51
    assert int(cell_c51.getPropertyValue("NumberFormat")) != date_key

