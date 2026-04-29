# WriterAgent - AI Writing Assistant for LibreOffice
import json
from typing import Any
from plugin.framework.logging import log
from plugin.framework.uno_context import get_desktop
from plugin.testing_runner import setup, teardown, native_test

_test_doc: Any = None
_test_ctx: Any = None

@setup
def setup_draw_form_tests(ctx):
    global _test_doc, _test_ctx
    _test_ctx = ctx

    desktop = get_desktop(ctx)
    import uno

    hidden_prop = uno.createUnoStruct(
        "com.sun.star.beans.PropertyValue",
        Name="Hidden",
        Value=True,
    )

    _test_doc = desktop.loadComponentFromURL("private:factory/sdraw", "_blank", 0, (hidden_prop,))
    assert _test_doc is not None, "Could not create Draw document"
    log.info("[DrawFormTests] starting tests")

@teardown
def teardown_draw_form_tests(ctx):
    global _test_doc, _test_ctx
    if _test_doc:
        _test_doc.close(True)
    _test_doc = None
    _test_ctx = None

def _exec_tool(name, args):
    from plugin.main import get_tools
    from plugin.framework.tool_context import ToolContext
    tctx = ToolContext(_test_doc, _test_ctx, "draw", {}, "test")
    res = get_tools().execute(name, tctx, **args)
    return res

@native_test
def test_draw_form_lifecycle():
    try:
        import pytest
        if _test_doc is None:
            pytest.skip("Requires LibreOffice document from native runner")
    except ImportError:
        pass

    # 1. Create a control
    res = _exec_tool("create_form_control", {"type": "checkbox", "name": "MyCheck", "label": "Agree"})
    assert res["status"] == "ok", f"create_form_control failed: {res}"
    
    # 2. List controls
    res = _exec_tool("list_form_controls", {})
    assert res["status"] == "ok", f"list_form_controls failed: {res}"
    assert res["count"] == 1
    assert res["controls"][0]["name"] == "MyCheck"
    
    shape_index = res["controls"][0]["index"]
    
    # 3. Edit control
    res = _exec_tool("edit_form_control", {"shape_index": shape_index, "name": "UpdatedCheck", "label": "Confirmed"})
    assert res["status"] == "ok", f"edit_form_control failed: {res}"
    
    res = _exec_tool("list_form_controls", {})
    assert res["controls"][0]["name"] == "UpdatedCheck"
    
    # 4. Delete control
    res = _exec_tool("delete_form_control", {"shape_index": shape_index})
    assert res["status"] == "ok", f"delete_form_control failed: {res}"
    
    res = _exec_tool("list_form_controls", {})
    assert res["count"] == 0

@native_test
def test_generate_form_draw():
    try:
        import pytest
        if _test_doc is None:
            pytest.skip("Requires LibreOffice document from native runner")
    except ImportError:
        pass

    # Test that generate_form is registered for Draw
    from plugin.main import get_tools
    tools = get_tools()
    gen_tool = tools.get("generate_form")
    assert gen_tool is not None
    assert "com.sun.star.drawing.DrawingDocument" in gen_tool.uno_services
