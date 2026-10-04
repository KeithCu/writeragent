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
from plugin.tests.testing_utils import TestingFactory, with_native_doc


def _execute_calc_tool(doc, ctx, name, args):
    return TestingFactory.execute_tool(doc, ctx, name, args, doc_type="calc")


@native_test
@with_native_doc("calc")
def test_calc_search_and_replace(ctx, doc):
    # Write some data
    _execute_calc_tool(doc, ctx, "write_formula_range", {"range": "A20:B21", "values": [
        ["AppleUnique", "BananaUnique"],
        ["CherryUnique", "DateUnique"]
    ]})

    # 1. Search for "BananaUnique"
    res_search = _execute_calc_tool(doc, ctx, "search_in_spreadsheet", {"pattern": "BananaUnique"})
    assert res_search.get("status") == "ok", f"search_in_spreadsheet failed: {res_search}"
    matches = res_search.get("matches", [])
    assert len(matches) == 1, f"Expected 1 match, found {len(matches)}"
    assert matches[0].get("cell") == "B20", f"Expected B20, got {matches[0].get('cell')}"

    # 2. Replace "BananaUnique" with "BlueberryUnique"
    res_replace = _execute_calc_tool(doc, ctx, "replace_in_spreadsheet", {"search": "BananaUnique", "replace": "BlueberryUnique"})
    assert res_replace.get("status") == "ok", f"replace_in_spreadsheet failed: {res_replace}"
    assert res_replace.get("replacements") == 1, f"Expected 1 replacement, got {res_replace.get('replacements')}"

    # 3. Verify replacement
    res_search_after = _execute_calc_tool(doc, ctx, "search_in_spreadsheet", {"pattern": "BlueberryUnique"})

    matches_after = res_search_after.get("matches", [])
    assert len(matches_after) == 1, f"Expected 1 match for BlueberryUnique, found {len(matches_after)}"
    assert matches_after[0].get("cell") == "B20", f"Expected BlueberryUnique at B20, got {matches_after[0].get('cell')}"

@native_test
@with_native_doc("calc")
def test_calc_search_replace_hidden_sheets(ctx, doc):
    doc.getSheets().insertNewByName("_HiddenDB", 1)

    _execute_calc_tool(doc, ctx, "write_formula_range", {"range": "_HiddenDB.A1:A1", "values": [["HiddenData"]]})

    res_search = _execute_calc_tool(doc, ctx, "search_in_spreadsheet", {"pattern": "HiddenData", "all_sheets": True})
    assert res_search.get("status") == "ok"
    assert len(res_search.get("matches", [])) == 0, f"Expected 0 matches for HiddenData on hidden sheet, found {len(res_search.get('matches', []))}"

    res_replace = _execute_calc_tool(doc, ctx, "replace_in_spreadsheet", {"search": "HiddenData", "replace": "ExposedData", "all_sheets": True})
    assert res_replace.get("status") == "ok"
    assert res_replace.get("replacements") == 0, f"Expected 0 replacements on hidden sheet, got {res_replace.get('replacements')}"

@native_test
@with_native_doc("calc")
def test_calc_search_replace_invalid_regex(ctx, doc):
    res_search = _execute_calc_tool(doc, ctx, "search_in_spreadsheet", {"pattern": "[invalid", "regex": True})
    assert res_search.get("status") == "error"
    assert res_search.get("code") == "INVALID_REGEX"

    res_replace = _execute_calc_tool(doc, ctx, "replace_in_spreadsheet", {"search": "[invalid", "replace": "valid", "regex": True})
    assert res_replace.get("status") == "error"
    assert res_replace.get("code") == "INVALID_REGEX"
