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
    res = _exec_tool(doc, ctx, "form_create_control", {"control": "checkbox", "name": "MyCheck", "label": "Agree"})
    assert res["status"] == "ok", f"create_form_control failed: {res}"
    
    # 2. List controls
    res = _exec_tool(doc, ctx, "form_list_controls", {})
    assert res["status"] == "ok", f"list_form_controls failed: {res}"
    assert res["count"] == 1
    assert res["controls"][0]["name"] == "MyCheck"
    
    shape_index = res["controls"][0]["index"]
    
    # 3. Edit control
    res = _exec_tool(doc, ctx, "form_edit_control", {"index": shape_index, "name": "UpdatedCheck", "label": "Confirmed"})
    assert res["status"] == "ok", f"edit_form_control failed: {res}"
    
    res = _exec_tool(doc, ctx, "form_list_controls", {})
    assert res["controls"][0]["name"] == "UpdatedCheck"
    
    # 4. Delete control
    res = _exec_tool(doc, ctx, "form_delete_control", {"index": shape_index})
    assert res["status"] == "ok", f"delete_form_control failed: {res}"
    
    res = _exec_tool(doc, ctx, "form_list_controls", {})
    assert res["count"] == 0


@native_test
@with_native_doc("draw")
def test_form_edit_by_name_and_checkbox_state(ctx, doc):
    # Non-control between widgets would break index-only addressing.
    from plugin.draw.bridge import DrawBridge

    bridge = DrawBridge(doc)
    from com.sun.star.awt import Point, Size
    from plugin.draw.shapes import DrawShapes

    page = bridge.get_active_page()
    deco, _, _ = DrawShapes().safe_create_shape(
        doc, page, "com.sun.star.drawing.RectangleShape", Point(200, 200), Size(800, 800)
    )
    deco.Name = "decoration"

    res = _exec_tool(doc, ctx, "form_create_control", {"control": "checkbox", "name": "MyCheck", "label": "Agree"})
    assert res["status"] == "ok", f"create failed: {res}"

    listed = _exec_tool(doc, ctx, "form_list_controls", {})
    assert listed["status"] == "ok"
    assert listed["count"] == 1
    assert listed["controls"][0]["name"] == "MyCheck"
    assert listed["controls"][0]["index"] != 0, "checkbox should not be the first shape on the page"
    assert listed["controls"][0].get("state") == 0

    edited = _exec_tool(doc, ctx, "form_edit_control", {"name": "MyCheck", "state": 1, "label": "Confirmed"})
    assert edited["status"] == "ok", f"edit by name failed: {edited}"
    assert edited.get("state") == 1

    listed2 = _exec_tool(doc, ctx, "form_list_controls", {})
    assert listed2["controls"][0]["name"] == "MyCheck"
    assert listed2["controls"][0]["label"] == "Confirmed"
    assert listed2["controls"][0]["state"] == 1

    deleted = _exec_tool(doc, ctx, "form_delete_control", {"name": "MyCheck"})
    assert deleted["status"] == "ok", f"delete by name failed: {deleted}"
    listed3 = _exec_tool(doc, ctx, "form_list_controls", {})
    assert listed3["count"] == 0


@native_test
@with_native_doc("draw")
def test_fill_draw_fields_happy_and_miss(ctx, doc):
    page_idx = 0
    try:
        ctrl = doc.getCurrentController()
        if ctrl is not None and hasattr(ctrl, "getCurrentPage"):
            cur = ctrl.getCurrentPage()
            pages = doc.getDrawPages()
            for i in range(pages.getCount()):
                if pages.getByIndex(i) == cur:
                    page_idx = i
                    break
    except Exception:
        page_idx = 0

    _exec_tool(doc, ctx, "shape_upsert", {
        "action": "create",
        "shape_type": "text",
        "x": 500, "y": 3000, "width": 3500, "height": 800,
        "text": "Lot:",
        "name": "lbl_lot",
        "page": page_idx,
    })
    _exec_tool(doc, ctx, "shape_upsert", {
        "action": "create",
        "shape_type": "text",
        "x": 4500, "y": 3000, "width": 5000, "height": 800,
        "text": "",
        "name": "fld_lot",
        "page": page_idx,
    })

    res = _exec_tool(doc, ctx, "fill_draw_fields", {
        "page": page_idx,
        "fields": [
            {"name": "fld_lot", "value": "LOT-42"},
            {"name": "no_such_field", "value": "x"},
        ],
    })
    assert res["status"] == "partial", f"expected partial: {res}"
    assert res["ok_count"] == 1
    assert res["fail_count"] == 1
    assert res["results"][0]["ok"] is True
    assert res["results"][1]["ok"] is False

    page = doc.getDrawPages().getByIndex(page_idx)
    found = None
    for i in range(page.getCount()):
        shape = page.getByIndex(i)
        if getattr(shape, "Name", "") == "fld_lot":
            found = shape
            break
    assert found is not None
    assert found.getString() == "LOT-42"


@native_test
@with_native_doc("draw")
def test_generate_form_draw(ctx, doc):
    # Test that generate_form is registered for Draw
    from plugin.main import get_tools
    tools = get_tools()
    gen_tool = tools.get("form_generate")
    assert gen_tool is not None
    assert "com.sun.star.drawing.DrawingDocument" in gen_tool.uno_services
