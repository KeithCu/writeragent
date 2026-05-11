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

def _execute_calc_tool(name, args):
    from plugin.main import get_tools, get_services
    from plugin.framework.tool import ToolContext
    # Pass suite bootstrap ctx (same as setup_calc_tests); None makes
    # get_desktop() use uno.getComponentContext() and can segfault under
    # python -m plugin.testing_runner.
    tctx = ToolContext(_test_doc, _test_ctx, "calc", get_services(), "test")
    try:
        res = get_tools().execute(name, tctx, **args)
    except (KeyError, ValueError) as e:
        res = {"status": "error", "error": str(e)}
    return res


@native_test
def test_calc_comments():
    # 1. Add a comment
    res_add = _execute_calc_tool("add_cell_comment", {"cell": "A10", "text": "This is a test comment"})
    assert res_add.get("status") == "ok", f"add_cell_comment failed: {res_add}"

    # 2. List comments to verify
    res_list = _execute_calc_tool("list_cell_comments", {})
    assert res_list.get("status") == "ok", f"list_cell_comments failed: {res_list}"
    comments = res_list.get("comments", [])

    found = False
    for c in comments:
        if c.get("cell") == "A10" and c.get("text") == "This is a test comment":
            found = True
            break
    assert found, f"Comment not found in list: {comments}"

    # 3. Delete the comment
    res_delete = _execute_calc_tool("delete_cell_comment", {"cell": "A10"})
    assert res_delete.get("status") == "ok", f"delete_cell_comment failed: {res_delete}"

    # 4. Verify deletion
    res_list_after = _execute_calc_tool("list_cell_comments", {})
    comments_after = res_list_after.get("comments", [])
    found_after = any(c.get("cell") == "A10" for c in comments_after)
    assert not found_after, "Comment was not deleted"
