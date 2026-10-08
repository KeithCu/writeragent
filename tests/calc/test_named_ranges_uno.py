# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import execute_calc_tool as _execute_calc_tool, with_native_doc


@native_test
@with_native_doc("calc")
def test_add_and_list_named_ranges(ctx, doc):
    named_ranges = doc.NamedRanges
    if named_ranges.hasByName("MyTestRange"):
        named_ranges.removeByName("MyTestRange")
    if named_ranges.hasByName("OtherRange"):
        named_ranges.removeByName("OtherRange")

    # 1. Add Named Range with flag
    res = _execute_calc_tool(
        doc, ctx, "named_range_add",
        {"name": "MyTestRange", "content": "$Sheet1.$A$1:$B$2", "flags": ["print_area"]}
    )
    assert res.get("status") == "ok", f"named_range_add failed: {res}"
    assert named_ranges.hasByName("MyTestRange"), "Named range was not created"

    # Add another
    res2 = _execute_calc_tool(
        doc, ctx, "named_range_add",
        {"name": "OtherRange", "content": "$Sheet1.$C$1"}
    )
    assert res2.get("status") == "ok", f"named_range_add failed: {res2}"

    # 2. List Named Ranges
    res_list = _execute_calc_tool(doc, ctx, "named_range_list", {"scope": "global"})
    assert res_list.get("status") == "ok"
    ranges = res_list.get("result", [])
    names = [r["name"] for r in ranges]
    assert "MyTestRange" in names
    assert "OtherRange" in names

    my_range = next(r for r in ranges if r["name"] == "MyTestRange")
    assert "$Sheet1.$A$1:$B$2" in my_range["content"]
    assert "print_area" in my_range["flags"]

    # Clean up
    if named_ranges.hasByName("MyTestRange"):
        named_ranges.removeByName("MyTestRange")
    if named_ranges.hasByName("OtherRange"):
        named_ranges.removeByName("OtherRange")


@native_test
@with_native_doc("calc")
def test_get_info_and_edit_named_range(ctx, doc):
    named_ranges = doc.NamedRanges
    if named_ranges.hasByName("EditTestRange"):
        named_ranges.removeByName("EditTestRange")
    if named_ranges.hasByName("RenamedTestRange"):
        named_ranges.removeByName("RenamedTestRange")

    # Add range
    res_add = _execute_calc_tool(
        doc, ctx, "named_range_add",
        {"name": "EditTestRange", "content": "$Sheet1.$A$1:$C$5"}
    )
    assert res_add.get("status") == "ok"

    # Get info
    res_info = _execute_calc_tool(doc, ctx, "named_range_get_info", {"name": "EditTestRange"})
    assert res_info.get("status") == "ok"
    info = res_info.get("result", {})
    assert info.get("name") == "EditTestRange"
    assert info.get("scope") == "global"
    assert "referred_range" in info
    assert info["referred_range"]["rows"] == 5
    assert info["referred_range"]["columns"] == 3

    # Edit range: update content and rename
    res_edit = _execute_calc_tool(
        doc, ctx, "named_range_edit",
        {
            "name": "EditTestRange",
            "new_name": "RenamedTestRange",
            "content": "$Sheet1.$A$1:$B$2",
            "flags": ["filter_criteria"],
        }
    )
    assert res_edit.get("status") == "ok"
    assert not named_ranges.hasByName("EditTestRange")
    assert named_ranges.hasByName("RenamedTestRange")

    # Verify updated info
    res_info2 = _execute_calc_tool(doc, ctx, "named_range_get_info", {"name": "RenamedTestRange"})
    assert res_info2.get("status") == "ok"
    info2 = res_info2.get("result", {})
    assert info2.get("name") == "RenamedTestRange"
    assert info2["referred_range"]["rows"] == 2
    assert "filter_criteria" in info2["flags"]

    # Clean up
    if named_ranges.hasByName("RenamedTestRange"):
        named_ranges.removeByName("RenamedTestRange")


@native_test
@with_native_doc("calc")
def test_sheet_scoped_named_range(ctx, doc):
    sheets = doc.getSheets()
    sheet0 = sheets.getByIndex(0)
    sheet_name = sheet0.getName()

    # Ensure clean state
    if sheet0.NamedRanges.hasByName("LocalRange"):
        sheet0.NamedRanges.removeByName("LocalRange")

    # Add sheet-local named range
    res_add = _execute_calc_tool(
        doc, ctx, "named_range_add",
        {"name": "LocalRange", "content": "$A$1:$A$10", "scope": sheet_name}
    )
    assert res_add.get("status") == "ok"
    assert sheet0.NamedRanges.hasByName("LocalRange")

    # List sheet-scoped ranges
    res_list = _execute_calc_tool(doc, ctx, "named_range_list", {"scope": sheet_name})
    assert res_list.get("status") == "ok"
    names = [r["name"] for r in res_list.get("result", [])]
    assert "LocalRange" in names

    # Delete sheet-scoped range
    res_del = _execute_calc_tool(
        doc, ctx, "named_range_delete",
        {"name": "LocalRange", "scope": sheet_name}
    )
    assert res_del.get("status") == "ok"
    assert not sheet0.NamedRanges.hasByName("LocalRange")


@native_test
@with_native_doc("calc")
def test_create_from_titles(ctx, doc):
    sheet = doc.getSheets().getByIndex(0)
    # Set headers in A1:B1 and data in A2:B5
    sheet.getCellByPosition(0, 0).setString("ColAlpha")
    sheet.getCellByPosition(1, 0).setString("ColBeta")
    for row in range(1, 5):
        sheet.getCellByPosition(0, row).setValue(row * 10)
        sheet.getCellByPosition(1, row).setValue(row * 20)

    named_ranges = doc.NamedRanges
    if named_ranges.hasByName("ColAlpha"):
        named_ranges.removeByName("ColAlpha")
    if named_ranges.hasByName("ColBeta"):
        named_ranges.removeByName("ColBeta")

    res = _execute_calc_tool(
        doc, ctx, "named_range_create_from_titles",
        {"range": ["A1:B5"], "border": "top", "scope": "global"}
    )
    assert res.get("status") == "ok", f"named_range_create_from_titles failed: {res}"
    assert named_ranges.hasByName("ColAlpha"), "ColAlpha named range was not created"
    assert named_ranges.hasByName("ColBeta"), "ColBeta named range was not created"

    # Clean up
    if named_ranges.hasByName("ColAlpha"):
        named_ranges.removeByName("ColAlpha")
    if named_ranges.hasByName("ColBeta"):
        named_ranges.removeByName("ColBeta")


@native_test
@with_native_doc("calc")
def test_delete_named_range(ctx, doc):
    named_ranges = doc.NamedRanges
    if not named_ranges.hasByName("DeleteTestRange"):
        from com.sun.star.table import CellAddress
        addr = CellAddress(Sheet=0, Column=0, Row=0)
        named_ranges.addNewByName("DeleteTestRange", "$Sheet1.$A$1", addr, 0)

    res = _execute_calc_tool(doc, ctx, "named_range_delete", {"name": "DeleteTestRange"})
    assert res.get("status") == "ok", f"named_range_delete failed: {res}"
    assert not named_ranges.hasByName("DeleteTestRange"), "Named range was not deleted"


@native_test
@with_native_doc("calc")
def test_transparent_named_range_read_write(ctx, doc):
    named_ranges = doc.NamedRanges
    if not named_ranges.hasByName("TransparentRange"):
        from com.sun.star.table import CellAddress
        addr = CellAddress(Sheet=0, Column=0, Row=9)
        named_ranges.addNewByName("TransparentRange", "$Sheet1.$A$10:$B$10", addr, 0)

    sheet = doc.getSheets().getByIndex(0)
    sheet.getCellByPosition(0, 9).setString("Apple")
    sheet.getCellByPosition(1, 9).setString("Banana")

    # 1. Read using the named range
    res_read = _execute_calc_tool(doc, ctx, "read_cell_range", {"range": ["TransparentRange"]})
    assert res_read.get("status") == "ok", f"read_cell_range failed: {res_read}"

    result_data = res_read["result"][0]
    assert result_data[0][0]["value"] == "Apple"
    assert result_data[0][1]["value"] == "Banana"

    # 2. Write using the named range
    res_write = _execute_calc_tool(doc, ctx, "write_formula_range", {
        "range": ["TransparentRange"],
        "values": '["Cherry", "Date"]'
    })
    assert res_write.get("status") == "ok", f"write_formula_range failed: {res_write}"
    assert sheet.getCellByPosition(0, 9).getString() == "Cherry"
    assert sheet.getCellByPosition(1, 9).getString() == "Date"

    # Clean up
    if named_ranges.hasByName("TransparentRange"):
        named_ranges.removeByName("TransparentRange")
