# WriterAgent - AI Writing Assistant for LibreOffice
from plugin.testing_runner import native_test
from plugin.tests.testing_utils import with_native_doc


def _exec_tool(doc, ctx, name, args):
    from plugin.main import get_tools
    from plugin.framework.tool import ToolContext
    tctx = ToolContext(doc, ctx, "draw", {}, "test")
    res = get_tools().execute(name, tctx, **args)
    return res


@native_test
@with_native_doc("draw")
def test_draw_form_lifecycle(ctx, doc):
    # 1. Create a control
    res = _exec_tool(doc, ctx, "form_create_control", {"type": "checkbox", "name": "MyCheck", "label": "Agree"})
    assert res["status"] == "ok", f"create_form_control failed: {res}"
    
    # 2. List controls
    res = _exec_tool(doc, ctx, "form_list_controls", {})
    assert res["status"] == "ok", f"list_form_controls failed: {res}"
    assert res["count"] == 1
    assert res["controls"][0]["name"] == "MyCheck"
    
    shape_index = res["controls"][0]["index"]
    
    # 3. Edit control
    res = _exec_tool(doc, ctx, "form_edit_control", {"shape_index": shape_index, "name": "UpdatedCheck", "label": "Confirmed"})
    assert res["status"] == "ok", f"edit_form_control failed: {res}"
    
    res = _exec_tool(doc, ctx, "form_list_controls", {})
    assert res["controls"][0]["name"] == "UpdatedCheck"
    
    # 4. Delete control
    res = _exec_tool(doc, ctx, "form_delete_control", {"shape_index": shape_index})
    assert res["status"] == "ok", f"delete_form_control failed: {res}"
    
    res = _exec_tool(doc, ctx, "form_list_controls", {})
    assert res["count"] == 0


@native_test
@with_native_doc("draw")
def test_generate_form_draw(ctx, doc):
    # Test that generate_form is registered for Draw
    from plugin.main import get_tools
    tools = get_tools()
    gen_tool = tools.get("form_generate")
    assert gen_tool is not None
    assert "com.sun.star.drawing.DrawingDocument" in gen_tool.uno_services
