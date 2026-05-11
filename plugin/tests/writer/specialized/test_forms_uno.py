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
"""Tests for Writer form tools, running inside LibreOffice."""

import uno
from types import SimpleNamespace
from typing import Any
from plugin.framework.uno_context import get_desktop
from plugin.testing_runner import setup, teardown, native_test

_test_doc: Any = None
_test_ctx: Any = None

@setup
def setup_form_tests(ctx):
    global _test_doc, _test_ctx
    _test_ctx = ctx
    desktop = get_desktop(ctx)

    hidden_prop = uno.createUnoStruct(
        "com.sun.star.beans.PropertyValue",
        Name="Hidden",
        Value=True,
    )
    _test_doc = desktop.loadComponentFromURL("private:factory/swriter", "_blank", 0, (hidden_prop,))
    assert _test_doc is not None, "Could not create test writer document"

@teardown
def teardown_form_tests(ctx):
    global _test_doc
    if _test_doc:
        _test_doc.close(True)
    _test_doc = None

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

@native_test
def test_create_form_control_checkbox():
    from plugin.main import get_tools
    registry = get_tools()
    tool = registry.get("create_form_control")
    assert tool is not None, "create_form_control tool not found"
    
    mock_ctx = MockCtx(_test_doc)
    
    # Test creating a checkbox
    res = tool.execute(mock_ctx, type="checkbox", name="test_check", label="Test Label")
    assert res["status"] == "ok", f"Tool execution failed: {res}"
    
    # Verify shape exists on draw page
    dp = _test_doc.getDrawPage()
    found = False
    for i in range(dp.getCount()):
        shape = dp.getByIndex(i)
        if shape.getShapeType() == "com.sun.star.drawing.ControlShape":
            if hasattr(shape, "Control") and shape.Control.Name == "test_check":
                assert shape.Control.Label == "Test Label"
                found = True
                break
    assert found, "Checkbox shape not found in draw page"

@native_test
def test_create_form_fat_api():
    from plugin.main import get_tools
    registry = get_tools()
    tool = registry.get("create_form")
    assert tool is not None, "create_form tool not found"
    
    mock_ctx = MockCtx(_test_doc)
    
    fields = [
        {"type": "text", "name": "f1", "label": "Field 1", "placeholder": "Hint"},
        {"type": "combobox", "name": "f2", "items": ["Option A", "Option B"]}
    ]
    
    res = tool.execute(mock_ctx, fields=fields)
    assert res["status"] == "ok", f"Fat API failed: {res}"
    assert len(res["results"]) == 2
    
    # Verify both exist
    dp = _test_doc.getDrawPage()
    names = []
    for i in range(dp.getCount()):
        shape = dp.getByIndex(i)
        if shape.getShapeType() == "com.sun.star.drawing.ControlShape":
            names.append(shape.Control.Name)
    
    assert "f1" in names
    assert "f2" in names

@native_test
def test_generate_form_processing_logic():
    # We test the parser and processor directly to avoid flaky LLM calls in CI
    from plugin.writer.specialized.forms import GenerateForm
    tool = GenerateForm()
    
    mock_ctx = MockCtx(_test_doc)
    
    # Test markdown snippet with multiple field types
    content = (
        "# Title\n"
        "Name: {FIELD:type='text',name='nm',placeholder='Name'}\n"
        "Agree: {FIELD:type='checkbox',name='cb',label='Yes'}"
    )
    
    res = tool._process_form_content(mock_ctx, content)
    assert res["status"] == "ok"
    
    # Verify draw page count increased
    dp = _test_doc.getDrawPage()
    names = [dp.getByIndex(i).Control.Name for i in range(dp.getCount()) if dp.getByIndex(i).getShapeType() == "com.sun.star.drawing.ControlShape"]
    assert "nm" in names
    assert "cb" in names

@native_test
def test_parse_field_tag():
    from plugin.writer.specialized.forms import GenerateForm
    tool = GenerateForm()
    
    tag = "{FIELD:type='combobox',name='my_list',items='A,B,C',label='Pick one'}"
    params = tool._parse_field_tag(tag)
    
    assert params["type"] == "combobox"
    assert params["name"] == "my_list"
    assert params["items"] == ["A", "B", "C"]
    assert params["label"] == "Pick one"
    
    # Test single quotes vs double quotes
    tag2 = '{FIELD:type="text",name="foo"}'
    params2 = tool._parse_field_tag(tag2)
    assert params2["type"] == "text"
    assert params2["name"] == "foo"

    # Test button
    tag3 = "{FIELD:type='button',name='btn',label='Click'}"
    params3 = tool._parse_field_tag(tag3)
    assert params3["type"] == "button"
    assert params3["label"] == "Click"

@native_test
def test_insert_text_with_view_cursor():
    """Verify that _insert_text correctly handles the ViewCursor by converting it."""
    from plugin.writer.specialized.forms import GenerateForm
    tool = GenerateForm()
    
    mock_ctx = MockCtx(_test_doc)
    
    # This call previously crashed because it passed ViewCursor to insert_html_at_cursor
    try:
        tool._insert_text(mock_ctx, "<b>Bold Text</b>")
    except Exception as e:
        import pytest
        pytest.fail(f"_insert_text crashed with: {str(e)}")
    
    # Verify insertion
    text = _test_doc.getText().getString()
    assert "Bold Text" in text

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
