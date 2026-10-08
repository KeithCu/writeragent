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
from plugin.tests.testing_utils import execute_calc_tool as _execute_calc_tool, with_native_doc


@native_test
@with_native_doc("calc")
def test_calc_comments(ctx, doc):
    # 1. Add a comment
    res_add = _execute_calc_tool(doc, ctx, "add_cell_comment", {"cell": "A10", "text": "This is a test comment"})
    assert res_add.get("status") == "ok", f"add_cell_comment failed: {res_add}"

    # 2. List comments to verify
    res_list = _execute_calc_tool(doc, ctx, "list_cell_comments", {})
    assert res_list.get("status") == "ok", f"list_cell_comments failed: {res_list}"
    comments = res_list.get("comments", [])

    found = False
    for c in comments:
        if c.get("cell") == "A10" and c.get("text") == "This is a test comment":
            found = True
            break
    assert found, f"Comment not found in list: {comments}"

    # 3. Delete the comment
    res_delete = _execute_calc_tool(doc, ctx, "delete_cell_comment", {"cell": "A10"})
    assert res_delete.get("status") == "ok", f"delete_cell_comment failed: {res_delete}"

    # 4. Verify deletion
    res_list_after = _execute_calc_tool(doc, ctx, "list_cell_comments", {})
    comments_after = res_list_after.get("comments", [])
    found_after = any(c.get("cell") == "A10" for c in comments_after)
    assert not found_after, "Comment was not deleted"
