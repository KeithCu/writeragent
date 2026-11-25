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

import uno  # noqa: F401

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import with_native_doc


def _clear_doc(doc):
    if not doc:
        return
    dp = doc.getDrawPage()
    while dp.getCount() > 0:
        dp.remove(dp.getByIndex(0))
    # Reset text too
    doc.getText().setString("")


class MockCtx:
    def __init__(self, doc, ctx=None):
        self.doc = doc
        self.ctx = ctx
        self.doc_type = "writer"
        self.selection_pin = None


@native_test
@with_native_doc("writer")
def test_create_form_control_checkbox(ctx, doc):
    from plugin.app import get_tools
    registry = get_tools()
    tool = registry.get("form_create_control")
    assert tool is not None, "create_form_control tool not found"
    
    mock_ctx = MockCtx(doc)
    
    # Test creating a checkbox
    res = tool.execute(mock_ctx, control="checkbox", name="test_check", label="Test Label")
    assert res["status"] == "ok", f"Tool execution failed: {res}"
    
    # Verify shape exists on draw page
    dp = doc.getDrawPage()
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
@with_native_doc("writer")
def test_create_form_fat_api(ctx, doc):
    from plugin.app import get_tools
    registry = get_tools()
    tool = registry.get("form_create")
    assert tool is not None, "create_form tool not found"
    
    mock_ctx = MockCtx(doc)
    
    fields = [
        {"control": "text", "name": "f1", "label": "Field 1", "placeholder": "Hint"},
        {"control": "combobox", "name": "f2", "items": ["Option A", "Option B"]}
    ]
    
    res = tool.execute(mock_ctx, fields=fields)
    assert res["status"] == "ok", f"Fat API failed: {res}"
    assert len(res["results"]) == 2
    
    # Verify both exist
    dp = doc.getDrawPage()
    names = []
    for i in range(dp.getCount()):
        shape = dp.getByIndex(i)
        if shape.getShapeType() == "com.sun.star.drawing.ControlShape":
            names.append(shape.Control.Name)
    
    assert "f1" in names
    assert "f2" in names


@native_test
@with_native_doc("writer")
def test_generate_form_processing_logic(ctx, doc):
    # We test the parser and processor directly to avoid flaky LLM calls in CI
    from plugin.writer.specialized.forms import FormGenerate
    tool = FormGenerate()
    
    mock_ctx = MockCtx(doc)
    
    # Test markdown snippet with multiple field types
    content = (
        "# Title\n"
        "Name: {FIELD:control='text',name='nm',placeholder='Name'}\n"
        "Agree: {FIELD:control='checkbox',name='cb',label='Yes'}"
    )
    
    res = tool._process_form_content(mock_ctx, content)
    assert res["status"] == "ok"
    
    # Verify form control names are on the draw page; bind every shape proxy once
    # before inspecting it so a late release of a dropped proxy cannot detach a refetched one (docs/framework/uno-test-lifecycle.md).
    dp = doc.getDrawPage()
    shapes = [dp.getByIndex(i) for i in range(dp.getCount())]
    names = [s.Control.Name for s in shapes if s.getShapeType() == "com.sun.star.drawing.ControlShape"]
    assert "nm" in names
    assert "cb" in names


@native_test
@with_native_doc("writer")
def test_insert_text_with_view_cursor(ctx, doc):
    """Verify that _insert_text correctly handles the ViewCursor by converting it."""
    from plugin.writer.specialized.forms import FormGenerate
    tool = FormGenerate()
    
    mock_ctx = MockCtx(doc)
    
    tool._insert_text(mock_ctx, "<b>Bold Text</b>")
    
    # Verify insertion
    text = doc.getText().getString()
    assert "Bold Text" in text


@native_test
@with_native_doc("writer")
def test_list_form_controls(ctx, doc):
    _clear_doc(doc)
    from plugin.app import get_tools
    registry = get_tools()
    
    # 1. Create a few controls
    create_tool = registry.get("form_create_control")
    mock_ctx = MockCtx(doc)
    create_tool.execute(mock_ctx, control="text", name="txt1", label="Label 1")
    create_tool.execute(mock_ctx, control="checkbox", name="chk1", label="Label 2")
    
    # 2. List them
    list_tool = registry.get("form_list_controls")
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
@with_native_doc("writer")
def test_edit_form_control(ctx, doc):
    _clear_doc(doc)
    from plugin.app import get_tools
    registry = get_tools()
    mock_ctx = MockCtx(doc)
    
    # 1. Create a control (use checkbox which has Label)
    create_tool = registry.get("form_create_control")
    create_tool.execute(mock_ctx, control="checkbox", name="original_name", label="Original Label")
    
    # 2. Get its index
    list_tool = registry.get("form_list_controls")
    list_res = list_tool.execute(mock_ctx)
    idx = list_res["controls"][0]["index"]
    
    # 3. Edit it
    edit_tool = registry.get("form_edit_control")
    edit_res = edit_tool.execute(mock_ctx, index=idx, name="new_name", label="New Label")
    
    assert edit_res["status"] == "ok"
    
    # 4. Verify changes
    list_res2 = list_tool.execute(mock_ctx)
    ctrl = list_res2["controls"][0]
    assert ctrl["name"] == "new_name"
    assert ctrl["label"] == "New Label"
    assert "text" not in ctrl

    # 5. Test text field editing
    _clear_doc(doc)
    create_tool.execute(mock_ctx, control="text", name="txt_orig", default_value="Original Text")
    list_res3 = list_tool.execute(mock_ctx)
    idx2 = list_res3["controls"][0]["index"]
    
    edit_tool.execute(mock_ctx, index=idx2, text="New Text")
    list_res4 = list_tool.execute(mock_ctx)
    ctrl2 = list_res4["controls"][0]
    assert ctrl2["text"] == "New Text"
    assert "label" not in ctrl2


@native_test
@with_native_doc("writer")
def test_delete_form_control(ctx, doc):
    _clear_doc(doc)
    from plugin.app import get_tools
    registry = get_tools()
    mock_ctx = MockCtx(doc)
    
    # 1. Create a control
    create_tool = registry.get("form_create_control")
    create_tool.execute(mock_ctx, control="checkbox", name="to_delete")
    
    # 2. Get index
    list_tool = registry.get("form_list_controls")
    list_res = list_tool.execute(mock_ctx)
    idx = list_res["controls"][0]["index"]
    
    # 3. Delete it
    delete_tool = registry.get("form_delete_control")
    del_res = delete_tool.execute(mock_ctx, index=idx)
    
    assert del_res["status"] == "ok"
    
    # 4. Verify it is gone
    list_res2 = list_tool.execute(mock_ctx)
    assert list_res2["count"] == 0


@native_test
@with_native_doc("writer")
def test_forms_radios_addressing(ctx, doc):
    _clear_doc(doc)
    from plugin.app import get_tools
    registry = get_tools()
    mock_ctx = MockCtx(doc)

    create_tool = registry.get("form_create_control")
    list_tool = registry.get("form_list_controls")
    edit_tool = registry.get("form_edit_control")
    delete_tool = registry.get("form_delete_control")

    # 1. Two radios in one group
    r1 = create_tool.execute(mock_ctx, control="radio", name="rad_male", group_name="gender", label="Male")
    assert r1["status"] == "ok"
    assert r1["control_name"] == "rad_male"
    assert r1["group_name"] == "gender"

    r2 = create_tool.execute(mock_ctx, control="radio", name="rad_female", group_name="gender", label="Female")
    assert r2["status"] == "ok"
    assert r2["control_name"] == "rad_female"
    assert r2["group_name"] == "gender"

    listed = list_tool.execute(mock_ctx)
    assert listed["count"] == 2
    names = {c["name"] for c in listed["controls"]}
    assert names == {"rad_male", "rad_female"}
    # Both share model name "gender", reported as group_name
    for c in listed["controls"]:
        assert c["group_name"] == "gender"

    # 2. Edit by shape name (addresses unique shape even though model name is shared)
    edit_res = edit_tool.execute(mock_ctx, name="rad_male", label="Male Option")
    assert edit_res["status"] == "ok"

    # 3. Delete by shape name
    del_res = delete_tool.execute(mock_ctx, name="rad_female")
    assert del_res["status"] == "ok"
    after_del = list_tool.execute(mock_ctx)
    assert after_del["count"] == 1
    assert after_del["controls"][0]["name"] == "rad_male"
    assert after_del["controls"][0]["label"] == "Male Option"


@native_test
@with_native_doc("writer")
def test_forms_button(ctx, doc):
    _clear_doc(doc)
    from plugin.app import get_tools
    registry = get_tools()
    mock_ctx = MockCtx(doc)
    create_tool = registry.get("form_create_control")

    btn_res = create_tool.execute(mock_ctx, control="button", name="btn_submit", label="Submit")
    assert btn_res["status"] == "ok"
    assert btn_res["control_name"] == "btn_submit"


@native_test
@with_native_doc("writer")
def test_forms_date(ctx, doc):
    _clear_doc(doc)
    from plugin.app import get_tools
    registry = get_tools()
    mock_ctx = MockCtx(doc)
    create_tool = registry.get("form_create_control")

    dt_res = create_tool.execute(mock_ctx, control="date", name="dt_test", default_value="15.08.2026")
    assert dt_res["status"] == "ok"

    dp = doc.getDrawPage()
    date_model = None
    for i in range(dp.getCount()):
        s = dp.getByIndex(i)
        if getattr(s, "Name", "") == "dt_test":
            date_model = s.Control
            break
    assert date_model is not None, "Date control not found"
    if hasattr(date_model.Date, "Year"):
        assert date_model.Date.Year == 2026 and date_model.Date.Month == 8 and date_model.Date.Day == 15
        assert date_model.DefaultDate.Year == 2026 and date_model.DefaultDate.Month == 8 and date_model.DefaultDate.Day == 15
    else:
        assert date_model.Date == 20260815
        assert date_model.DefaultDate == 20260815


@native_test
@with_native_doc("writer")
def test_forms_radio_group_rename(ctx, doc):
    _clear_doc(doc)
    from plugin.app import get_tools

    registry = get_tools()
    mock_ctx = MockCtx(doc)
    create_tool = registry.get("form_create_control")
    edit_tool = registry.get("form_edit_control")

    r1 = create_tool.execute(mock_ctx, control="radio", name="radio1", group_name="groupA")
    assert r1["status"] == "ok"
    r2 = create_tool.execute(mock_ctx, control="radio", name="radio2", group_name="groupA")
    assert r2["status"] == "ok"

    # Rename shape only
    edit_res = edit_tool.execute(mock_ctx, name="radio1", new_name="radio1_renamed")
    assert edit_res["status"] == "ok"
    assert edit_res["control_name"] == "radio1_renamed"
    assert edit_res["group_name"] == "groupA"

    # Rename group only
    edit_res2 = edit_tool.execute(mock_ctx, name="radio2", group_name="groupB")
    assert edit_res2["status"] == "ok"
    assert edit_res2["control_name"] == "radio2"
    assert edit_res2["group_name"] == "groupB"


@native_test
@with_native_doc("writer")
def test_form_create_recorded_accept_reject_and_record_mode_uno(ctx, doc):
    from plugin.app import get_tools
    from plugin.framework.config import get_config, save
    from plugin.framework.uno_context import execute_dispatch
    from plugin.writer.inline_review import agent_tokens, scan_redline_view

    tools = get_tools()
    form_create = tools.get("form_create")
    tctx = MockCtx(doc, ctx)
    fields = [
        {"control": "text", "name": "f_txt", "default_value": "hi"},
        {"control": "checkbox", "name": "f_chk", "label": "Ok"},
    ]

    _clear_doc(doc)
    doc.getText().setString("base")
    doc.setPropertyValue("RecordChanges", True)
    try:
        res_rej = form_create.execute(tctx, fields=fields)
        assert res_rej["status"] == "ok" and res_rej["tracked"] is True
        execute_dispatch(ctx, doc.getCurrentController(), ".uno:RejectAllTrackedChanges")
        assert doc.getText().getString() == "base"
        assert doc.getDrawPage().getCount() == 0

        res_acc = form_create.execute(tctx, fields=fields)
        assert res_acc["status"] == "ok"
        execute_dispatch(ctx, doc.getCurrentController(), ".uno:AcceptAllTrackedChanges")
        assert doc.getDrawPage().getCount() == 2
    finally:
        doc.setPropertyValue("RecordChanges", False)

    _clear_doc(doc)
    doc.getText().setString("before")
    prev = get_config("doc.agent_edit_review_mode")
    save({"doc.agent_edit_review_mode": "record"})
    try:
        res_rec = form_create.execute(tctx, fields=fields)
        assert res_rec["status"] == "ok"
        assert res_rec["tracked"] is True and res_rec["pending_review"] is True
        assert len(agent_tokens(scan_redline_view(doc))) == 1
        assert not doc.getPropertyValue("RecordChanges")

        before_text = doc.getText().getString()
        before_count = doc.getDrawPage().getCount()
        before_tokens = agent_tokens(scan_redline_view(doc))

        bad = form_create.execute(
            tctx,
            fields=[
                {"control": "text", "name": "ok_field"},
                {"control": "invalid_kind", "name": "bad_field"},
            ],
        )
        assert bad["status"] == "error"
        assert doc.getText().getString() == before_text
        assert doc.getDrawPage().getCount() == before_count
        assert agent_tokens(scan_redline_view(doc)) == before_tokens
        assert not doc.getPropertyValue("RecordChanges")
    finally:
        save({"doc.agent_edit_review_mode": prev})


@native_test
@with_native_doc("writer")
def test_form_create_uses_pinned_caret_uno(ctx, doc):
    from plugin.doc.text_helpers import selection_cursor_snapshot
    from plugin.framework.tool import SelectionPin
    from plugin.tests.testing_utils import native_writer_tool_context
    from plugin.writer.specialized.forms import FormCreate

    _clear_doc(doc)
    text = doc.getText()
    cur = text.createTextCursor()
    text.insertString(cur, "Alpha beta gamma", False)
    text.insertControlCharacter(cur, 0, False)
    text.insertString(cur, "Second para", False)
    cur.gotoStart(False)
    cur.goRight(6, False)
    controller = doc.getCurrentController()
    controller.select(cur)

    tctx = native_writer_tool_context(doc, ctx)
    tctx.selection_pin = SelectionPin(selection_cursor_snapshot(doc), doc)
    controller.getViewCursor().gotoEnd(False)

    res = FormCreate().execute(
        tctx,
        fields=[{"control": "text", "name": "f1"}, {"control": "checkbox", "name": "f2"}],
    )
    assert res["status"] == "ok", res

    dp = doc.getDrawPage()
    shapes = {dp.getByIndex(i).Name: dp.getByIndex(i) for i in range(dp.getCount())}
    assert set(shapes) >= {"f1", "f2"}
    a1 = shapes["f1"].getAnchor()
    a2 = shapes["f2"].getAnchor()
    before_f1 = text.createTextCursorByRange(a1.getStart())
    before_f1.gotoStart(True)
    assert before_f1.getString() == "Alpha "
    assert text.compareRegionStarts(a1.getStart(), a2.getStart()) > 0
    between = text.createTextCursorByRange(a1.getEnd())
    between.gotoRange(a2.getStart(), True)
    assert between.getString() == " "
    after_f2 = text.createTextCursorByRange(a2.getEnd())
    after_f2.gotoEnd(True)
    after_f2_text = after_f2.getString().replace("\r\n", "\n").replace("\r", "\n")
    assert after_f2_text == " beta gamma\nSecond para", repr(after_f2_text)


@native_test
@with_native_doc("writer")
def test_form_generate_uses_pinned_caret_uno(ctx, doc):
    from plugin.doc.text_helpers import selection_cursor_snapshot
    from plugin.framework.tool import SelectionPin
    from plugin.tests.testing_utils import native_writer_tool_context
    from plugin.writer.specialized.forms import FormGenerate

    _clear_doc(doc)
    text = doc.getText()
    cur = text.createTextCursor()
    text.insertString(cur, "Alpha beta gamma", False)
    text.insertControlCharacter(cur, 0, False)
    text.insertString(cur, "Second para", False)
    cur.gotoStart(False)
    cur.goRight(6, False)
    controller = doc.getCurrentController()
    controller.select(cur)

    tctx = native_writer_tool_context(doc, ctx)
    tctx.selection_pin = SelectionPin(selection_cursor_snapshot(doc), doc)
    controller.getViewCursor().gotoEnd(False)

    content = "<p>Title</p>Name: {FIELD:control='text',name='g1'} Agree {FIELD:control='checkbox',name='g2'}<p>tail</p>"
    res = FormGenerate()._process_form_content(tctx, content)
    assert res["status"] == "ok", res

    dp = doc.getDrawPage()
    shapes = {dp.getByIndex(i).Name: dp.getByIndex(i) for i in range(dp.getCount())}
    assert {"g1", "g2"} <= set(shapes)
    g1_anchor = shapes["g1"].getAnchor()
    g2_anchor = shapes["g2"].getAnchor()
    before_g1 = text.createTextCursorByRange(g1_anchor.getStart())
    before_g1.gotoStart(True)
    assert before_g1.getString().replace("\r\n", "\n").replace("\r", "\n") == "Alpha Title\nName: "
    between_g = text.createTextCursorByRange(g1_anchor.getEnd())
    between_g.gotoRange(g2_anchor.getStart(), True)
    assert between_g.getString() == "Agree "
    after_g2 = text.createTextCursorByRange(g2_anchor.getEnd())
    after_g2.gotoEnd(True)
    assert after_g2.getString().replace("\r\n", "\n").replace("\r", "\n") == "tail\nbeta gamma\nSecond para"


@native_test
@with_native_doc("writer")
def test_form_create_in_table_cell_uno(ctx, doc):
    from plugin.framework.uno_context import uno_same
    from plugin.tests.testing_utils import native_writer_tool_context
    from plugin.writer.specialized.forms import FormCreate

    _clear_doc(doc)
    table = doc.createInstance("com.sun.star.text.TextTable")
    table.initialize(1, 1)
    doc.getText().insertTextContent(doc.getText().getEnd(), table, False)
    cell = table.getCellByName("A1")
    doc.getCurrentController().select(cell.getStart())

    tctx = native_writer_tool_context(doc, ctx)
    res = FormCreate().execute(
        tctx,
        fields=[{"control": "text", "name": "cell_f1"}, {"control": "text", "name": "cell_f2"}],
    )
    assert res["status"] == "ok", res

    dp = doc.getDrawPage()
    shapes = {dp.getByIndex(i).Name: dp.getByIndex(i) for i in range(dp.getCount())}
    assert uno_same(shapes["cell_f1"].getAnchor().getText(), cell)
    assert uno_same(shapes["cell_f2"].getAnchor().getText(), cell)


@native_test
@with_native_doc("writer")
def test_form_generate_recorded_over_selection_keeps_text_uno(ctx, doc):
    from plugin.tests.testing_utils import native_writer_tool_context
    from plugin.writer.specialized.forms import FormGenerate

    _clear_doc(doc)
    text = doc.getText()
    cur = text.createTextCursor()
    text.insertString(cur, "Alpha beta gamma", False)
    text.insertControlCharacter(cur, 0, False)
    text.insertString(cur, "Second para", False)
    sd = doc.createSearchDescriptor()
    sd.SearchString = "beta"
    found = doc.findFirst(sd)
    assert found is not None
    doc.getCurrentController().select(found)

    tctx = native_writer_tool_context(doc, ctx)
    doc.setPropertyValue("RecordChanges", True)
    try:
        res = FormGenerate()._process_form_content(
            tctx,
            "<p>Name: </p>{FIELD:control='text',name='g1'}",
        )
        assert res["status"] == "ok", res
        assert "Name:" in text.getString()

        from plugin.framework.uno_context import execute_dispatch

        execute_dispatch(ctx, doc.getCurrentController(), ".uno:AcceptAllTrackedChanges")
        accepted = text.getString()
        assert "beta" not in accepted
        assert "Name:" in accepted
        dp = doc.getDrawPage()
        assert any(dp.getByIndex(i).Name == "g1" for i in range(dp.getCount()))
    finally:
        doc.setPropertyValue("RecordChanges", False)




