# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
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
"""Tests for Writer form management tools, running inside LibreOffice."""

import uno
from typing import Any
from plugin.framework.uno_context import get_desktop
from plugin.testing_runner import setup, teardown, native_test

_test_doc: Any = None

def _clear_doc():
    global _test_doc
    if not _test_doc:
        return
    dp = _test_doc.getDrawPage()
    while dp.getCount() > 0:
        dp.remove(dp.getByIndex(0))
    # Reset text too
    _test_doc.getText().setString("")

class MockCtx:
    def __init__(self, doc):
        self.doc = doc
        self.doc_type = "writer"

@setup
def setup_form_management_tests(ctx):
    global _test_doc
    desktop = get_desktop(ctx)
    from com.sun.star.beans import PropertyValue
    hidden_prop = PropertyValue()
    hidden_prop.Name = "Hidden"
    hidden_prop.Value = True
    _test_doc = desktop.loadComponentFromURL("private:factory/swriter", "_blank", 0, (hidden_prop,))
    assert _test_doc is not None, "Could not create test writer document"

@teardown
def teardown_form_management_tests(ctx):
    global _test_doc
    if _test_doc:
        _test_doc.close(True)
    _test_doc = None

@native_test
def test_list_form_controls():
    _clear_doc()
    from plugin.main import get_tools
    registry = get_tools()
    
    # 1. Create a few controls
    create_tool = registry.get("create_form_control")
    mock_ctx = MockCtx(_test_doc)
    create_tool.execute(mock_ctx, type="text", name="txt1", label="Label 1")
    create_tool.execute(mock_ctx, type="checkbox", name="chk1", label="Label 2")
    
    # 2. List them
    list_tool = registry.get("list_form_controls")
    res = list_tool.execute(mock_ctx)
    
    assert res["status"] == "ok"
    assert res["count"] == 2
    
    names = [c["name"] for c in res["controls"]]
    assert "txt1" in names
    assert "chk1" in names
    
    types = [c["type"] for c in res["controls"]]
    assert "text" in types
    assert "checkbox" in types

@native_test
def test_edit_form_control():
    _clear_doc()
    from plugin.main import get_tools
    registry = get_tools()
    mock_ctx = MockCtx(_test_doc)
    
    # 1. Create a control (use checkbox which has Label)
    create_tool = registry.get("create_form_control")
    create_tool.execute(mock_ctx, type="checkbox", name="original_name", label="Original Label")
    
    # 2. Get its index
    list_tool = registry.get("list_form_controls")
    list_res = list_tool.execute(mock_ctx)
    idx = list_res["controls"][0]["index"]
    
    # 3. Edit it
    edit_tool = registry.get("edit_form_control")
    edit_res = edit_tool.execute(mock_ctx, shape_index=idx, name="new_name", label="New Label")
    
    assert edit_res["status"] == "ok"
    
    # 4. Verify changes
    list_res2 = list_tool.execute(mock_ctx)
    ctrl = list_res2["controls"][0]
    assert ctrl["name"] == "new_name"
    assert ctrl["label"] == "New Label"
    assert "text" not in ctrl

    # 5. Test text field editing
    _clear_doc()
    create_tool.execute(mock_ctx, type="text", name="txt_orig", default_value="Original Text")
    list_res3 = list_tool.execute(mock_ctx)
    idx2 = list_res3["controls"][0]["index"]
    
    edit_tool.execute(mock_ctx, shape_index=idx2, text="New Text")
    list_res4 = list_tool.execute(mock_ctx)
    ctrl2 = list_res4["controls"][0]
    assert ctrl2["text"] == "New Text"
    assert "label" not in ctrl2

@native_test
def test_delete_form_control():
    _clear_doc()
    from plugin.main import get_tools
    registry = get_tools()
    mock_ctx = MockCtx(_test_doc)
    
    # 1. Create a control
    create_tool = registry.get("create_form_control")
    create_tool.execute(mock_ctx, type="checkbox", name="to_delete")
    
    # 2. Get index
    list_tool = registry.get("list_form_controls")
    list_res = list_tool.execute(mock_ctx)
    idx = list_res["controls"][0]["index"]
    
    # 3. Delete it
    delete_tool = registry.get("delete_form_control")
    del_res = delete_tool.execute(mock_ctx, shape_index=idx)
    
    assert del_res["status"] == "ok"
    
    # 4. Verify it is gone
    list_res2 = list_tool.execute(mock_ctx)
    assert list_res2["count"] == 0
