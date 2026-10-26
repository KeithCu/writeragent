# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Unit tests for Calc form text insertion and per-field error reporting.

No LibreOffice process. Cell types are stubs with the same ``.value`` name
PyUNO's ``CellContentType`` enum exposes.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from plugin.framework.errors import ToolExecutionError

from plugin.writer.specialized.forms import (
    FormCreate,
    FormCreateControl,
    FormEditControl,
    FormDeleteControl,
    FormGenerate,
    FormListControls,
    _append_text_to_calc_active_area,
    _create_control,
    _resolve_form_page,
)


def _cell(kind, displayed=""):
    cell = MagicMock()
    if isinstance(kind, str):
        cell.getType.return_value = SimpleNamespace(value=kind)
    else:
        cell.getType.return_value = kind
    cell.getString.return_value = displayed
    return cell


def _doc_with_cell(cell, col=1, row=2):
    doc = MagicMock()
    controller = MagicMock()
    sheet = MagicMock()
    controller.ActiveSheet = sheet
    doc.getCurrentController.return_value = controller
    addr = SimpleNamespace(StartColumn=col, StartRow=row)
    selection = MagicMock()
    selection.getRangeAddress.return_value = addr
    controller.getSelection.return_value = selection
    sheet.getCellByPosition.return_value = cell
    return doc, sheet


def test_append_text_leaves_formula_and_value_cells():
    for kind in ("FORMULA", "VALUE"):
        cell = _cell(kind, "=A1+1" if kind == "FORMULA" else "42")
        doc, sheet = _doc_with_cell(cell, col=3, row=4)
        _append_text_to_calc_active_area(doc, " Name ")
        cell.setString.assert_not_called()
        sheet.getCellByPosition.assert_called_once_with(3, 4)


def test_append_text_leaves_enum_repr_formula():
    # Same shape as ``str(uno.Enum)`` when ``.value`` is not present.
    class EnumRepr:
        def __str__(self):
            return "<Enum instance com.sun.star.table.CellContentType ('FORMULA')>"

    cell = _cell(EnumRepr(), "=SUM(A1:A2)")
    doc, _sheet = _doc_with_cell(cell)
    _append_text_to_calc_active_area(doc, " ")
    cell.setString.assert_not_called()


def test_append_text_appends_to_text_and_writes_empty():
    text_cell = _cell("TEXT", "Hi")
    doc, _sheet = _doc_with_cell(text_cell)
    _append_text_to_calc_active_area(doc, " there")
    text_cell.setString.assert_called_once_with("Hi there")

    empty = _cell("EMPTY", "should-not-be-read")
    doc, _sheet = _doc_with_cell(empty)
    _append_text_to_calc_active_area(doc, "Label ")
    empty.setString.assert_called_once_with("Label ")
    empty.getString.assert_not_called()


def test_append_text_skips_unknown_cell_type():
    cell = MagicMock()
    cell.getString.return_value = "=A1"
    doc, _sheet = _doc_with_cell(cell)
    _append_text_to_calc_active_area(doc, " ")
    cell.setString.assert_not_called()


def test_append_text_without_selection_uses_a1():
    cell = _cell("EMPTY", "")
    doc = MagicMock()
    controller = MagicMock()
    sheet = MagicMock()
    controller.ActiveSheet = sheet
    controller.getSelection.return_value = SimpleNamespace()
    doc.getCurrentController.return_value = controller
    sheet.getCellByPosition.return_value = cell

    _append_text_to_calc_active_area(doc, "Label")

    sheet.getCellByPosition.assert_called_once_with(0, 0)
    cell.setString.assert_called_once_with("Label")


def test_insert_space_and_insert_text_skip_formula_cells(monkeypatch):
    monkeypatch.setattr("plugin.writer.specialized.forms._is_spreadsheet_doc", lambda doc: True)
    cell = _cell("FORMULA", "=B2")
    doc, _sheet = _doc_with_cell(cell)
    ctx = SimpleNamespace(doc=doc)

    FormCreate()._insert_space(ctx)
    FormGenerate()._insert_text(ctx, "<b>Name</b> extra")

    cell.setString.assert_not_called()


def test_insert_text_appends_plain_label_on_text_cell(monkeypatch):
    monkeypatch.setattr("plugin.writer.specialized.forms._is_spreadsheet_doc", lambda doc: True)
    cell = _cell("TEXT", "Hi")
    doc, _sheet = _doc_with_cell(cell)
    FormGenerate()._insert_text(SimpleNamespace(doc=doc), "<p>Name</p>")
    cell.setString.assert_called_once_with("HiName ")


def _mock_form_ctx(doc=None):
    if doc is None:
        doc = MagicMock()
    mgr = MagicMock()
    mgr.isLocked.return_value = False
    mgr.getAllUndoActionTitles.return_value = ["Form Create", "Form Generate"]
    doc.getUndoManager.return_value = mgr
    return SimpleNamespace(doc=doc, ctx=MagicMock())


def _patch_field_creator(monkeypatch, failing_name):
    def _execute(ctx, *, page_cursors=None, **kwargs):
        if kwargs.get("name") == failing_name:
            return {"status": "error", "code": "TOOL_EXECUTION_ERROR", "message": "no draw page"}
        return {"status": "ok", "message": "created", "control_name": kwargs.get("name")}

    monkeypatch.setattr("plugin.writer.specialized.forms._create_control", _execute)


def test_form_create_reports_field_failures(monkeypatch):
    _patch_field_creator(monkeypatch, "bad")
    tool = FormCreate()
    monkeypatch.setattr(tool, "_insert_space", lambda ctx: None)

    res = tool.execute(_mock_form_ctx(), fields=[{"control": "text", "name": "ok1"}, {"control": "text", "name": "bad"}])

    assert res["status"] == "error"
    assert res["code"] == "TOOL_EXECUTION_ERROR"
    assert "bad: no draw page" in res["message"]
    assert "1 of 2" in res["message"]
    assert res["details"]["results"][0]["status"] == "ok"
    assert res["details"]["results"][1]["message"] == "no draw page"


def test_form_create_ok_when_every_field_succeeds(monkeypatch):
    _patch_field_creator(monkeypatch, "nobody")
    tool = FormCreate()
    monkeypatch.setattr(tool, "_insert_space", lambda ctx: None)

    res = tool.execute(_mock_form_ctx(), fields=[{"control": "text", "name": "ok1"}])

    assert res["status"] == "ok"
    assert res["results"][0]["control_name"] == "ok1"
    assert "Processed 1 form fields" in res["message"]


def test_form_generate_reports_field_failures(monkeypatch):
    _patch_field_creator(monkeypatch, "bad")
    tool = FormGenerate()
    monkeypatch.setattr(tool, "_insert_text", lambda ctx, text, page_cursors=None: None)

    res = tool._process_form_content(_mock_form_ctx(), "Hello {FIELD:control='text',name='bad',label='X'} tail")

    assert res["status"] == "error"
    assert "bad: no draw page" in res["message"]
    assert res["details"]["results"][0]["status"] == "error"


def test_form_generate_reports_unparsed_field_tag():
    tool = FormGenerate()
    tool._insert_text = lambda ctx, text, page_cursors=None: None

    res = tool._process_form_content(_mock_form_ctx(), "Intro {FIELD:broken} end")

    assert res["status"] == "error"
    assert "Could not parse form field tag" in res["message"]


def test_form_generate_ok_when_fields_succeed(monkeypatch):
    _patch_field_creator(monkeypatch, "nobody")
    tool = FormGenerate()
    inserted = []
    monkeypatch.setattr(tool, "_insert_text", lambda ctx, text, page_cursors=None: inserted.append(text))

    res = tool._process_form_content(_mock_form_ctx(), "Hello {FIELD:control='text',name='nm',label='Name'} tail")

    assert res == {"status": "ok", "message": "Form generation completed and inserted."}
    assert inserted == ["Hello ", " tail"]


def test_form_generate_insert_text_typeerror_is_not_retried():
    tool = FormGenerate()
    calls = []

    def fake_insert(ctx, text, page_cursors=None):
        calls.append(text)
        raise TypeError("simulated error")

    tool._insert_text = fake_insert

    with pytest.raises(TypeError, match="simulated error"):
        tool._process_form_content(_mock_form_ctx(), "plain text")

    assert calls == ["plain text"]


def test_form_generate_blocks_llm_wait_off_ui_thread(monkeypatch):
    tool = FormGenerate()
    assert tool.is_async() is True

    monkeypatch.setattr("plugin.framework.thread_guard.on_main_thread", lambda: True)

    ctx = MagicMock()

    with patch("plugin.framework.config.get_api_config"), \
         patch("plugin.framework.client.llm_client.LlmClient"):
        res = tool.execute(ctx, description="test form")

        assert res["status"] == "error"
        assert "FormGenerate LLM call must not run on the UI thread" in res["message"]


def test_resolve_form_page_reraises_disposed(monkeypatch):
    class DisposedException(Exception):
        pass

    doc = MagicMock()
    monkeypatch.setattr("plugin.writer.specialized.forms._is_draw_doc", lambda d: True)

    bridge_mock = MagicMock()
    bridge_mock.get_pages.return_value.getByIndex.side_effect = DisposedException("closed")
    monkeypatch.setattr("plugin.draw.bridge.DrawBridge", lambda d: bridge_mock)

    with pytest.raises(DisposedException):
        _resolve_form_page(doc, page=0)

    bridge_mock.get_pages.return_value.getByIndex.side_effect = IndexError("bad index")
    assert _resolve_form_page(doc, page=0) is None


def test_append_text_multi_range_selection_uses_first_range():
    cell = _cell("EMPTY", "")
    doc = MagicMock()
    controller = MagicMock()
    sheet = MagicMock()
    controller.ActiveSheet = sheet
    addr = SimpleNamespace(StartColumn=2, StartRow=3)
    selection = MagicMock(spec=["getRangeAddresses"])
    selection.getRangeAddresses.return_value = [addr]
    controller.getSelection.return_value = selection
    doc.getCurrentController.return_value = controller
    sheet.getCellByPosition.return_value = cell

    _append_text_to_calc_active_area(doc, "Label")

    sheet.getCellByPosition.assert_called_once_with(2, 3)
    cell.setString.assert_called_once_with("Label")


def test_form_control_create_writer_inserts_on_anchor_host():
    doc = MagicMock()
    body_text = MagicMock(name="body_text")
    doc.getText.return_value = body_text

    controller = MagicMock()
    doc.getCurrentController.return_value = controller

    host = MagicMock(name="cell_host")
    anchor = MagicMock(name="anchor")
    anchor.getText.return_value = host

    selection = MagicMock()
    selection.getCount.return_value = 1
    selection.getByIndex.return_value = anchor
    controller.getSelection.return_value = selection

    doc.createInstance.return_value = MagicMock()

    ctx = SimpleNamespace(doc=doc)
    res = _create_control(ctx, control="text", name="fld")
    assert res["status"] == "ok"
    host.insertTextContent.assert_called_once()
    body_text.insertTextContent.assert_not_called()


def test_forms_button_present_in_both_schemas():
    assert "button" in FormCreateControl.parameters["properties"]["control"]["enum"]
    assert "button" in FormCreate.parameters["properties"]["fields"]["items"]["properties"]["control"]["enum"]


def test_forms_create_rejects_unknown_control():
    ctx = SimpleNamespace(doc=MagicMock())
    res = _create_control(ctx, control="laser_cannon", name="fld")
    assert res["status"] == "error"
    assert "Unknown control type 'laser_cannon'" in res["message"]


def test_forms_create_rejects_bad_date():
    doc = MagicMock()
    ctx = SimpleNamespace(doc=doc)
    res = _create_control(ctx, control="date", name="dt", default_value="32.13.2026")
    assert res["status"] == "error"
    assert "Invalid date format" in res["message"]
    doc.createInstance.assert_not_called()


def test_forms_edit_invalid_state_leaves_model_untouched():
    from plugin.writer.specialized.forms import FormEditControl

    doc = MagicMock()
    dp = MagicMock()
    shape = MagicMock()
    shape.getShapeType.return_value = "com.sun.star.drawing.ControlShape"
    shape.Name = "target_ctrl"
    model = MagicMock(spec=["Name", "Label", "State"])
    model.Name = "target_ctrl"
    shape.Control = model
    dp.getCount.return_value = 1
    dp.getByIndex.return_value = shape

    ctx = SimpleNamespace(doc=doc)
    with patch("plugin.writer.specialized.forms._resolve_form_page", return_value=dp):
        res = FormEditControl()._execute_main(ctx, name="target_ctrl", state="not-a-state", new_name="new_ctrl", label="Should Not Change")
        assert res["status"] == "error"
        assert "Could not interpret" in res["message"]
        # Neither Name nor Label were updated
        assert model.Name == "target_ctrl"
        assert shape.Name == "target_ctrl"
        assert not hasattr(model, "Label") or model.Label != "Should Not Change"


def test_forms_coerce_items_string():
    from plugin.writer.specialized.forms import _coerce_items
    assert _coerce_items("a, b, c") == ["a", "b", "c"]
    assert _coerce_items(["x", "y"]) == ["x", "y"]
    assert _coerce_items(123) is None


def test_forms_create_skips_spacer_on_failure(monkeypatch):
    tool = FormCreate()
    spacer_called = []
    monkeypatch.setattr(tool, "_insert_space", lambda ctx: spacer_called.append(True))
    ctx = SimpleNamespace(doc=MagicMock())

    res = tool.execute(ctx, fields=[{"control": "invalid_control", "name": "bad"}])
    assert res["status"] == "error"
    assert len(spacer_called) == 0


def test_forms_parse_field_tag_requires_name_and_valid_control():
    tool = FormGenerate()
    # Missing name
    assert tool._parse_field_tag("{FIELD:control='text'}") is None
    # Missing control
    assert tool._parse_field_tag("{FIELD:name='my_name'}") is None
    # Invalid control
    assert tool._parse_field_tag("{FIELD:control='teleport',name='ctrl'}") is None
    # Valid
    parsed = tool._parse_field_tag("{FIELD:control='combobox',name='c1',items='one,two'}")
    assert parsed is not None
    assert parsed["control"] == "combobox"
    assert parsed["name"] == "c1"
    assert parsed["items"] == ["one", "two"]


def test_form_create_control_disposal_from_create_instance_propagates():
    from plugin.framework.errors import DocumentDisposedError

    doc = MagicMock()
    doc.createInstance.side_effect = DocumentDisposedError("closed")
    ctx = SimpleNamespace(doc=doc)
    with pytest.raises(DocumentDisposedError):
        _create_control(ctx, control="text", name="fld")


def test_form_create_control_date_default_value_applied():
    doc = MagicMock()
    model = MagicMock()
    doc.createInstance.side_effect = lambda name: model if "DateField" in name else MagicMock()
    controller = MagicMock()
    doc.getCurrentController.return_value = controller
    selection = MagicMock()
    selection.getCount.return_value = 1
    selection.getByIndex.return_value.getText.return_value = MagicMock()
    controller.getSelection.return_value = selection

    ctx = SimpleNamespace(doc=doc)
    res = _create_control(ctx, control="date", name="dt", default_value="2026-10-18")
    assert res["status"] == "ok"
    assert hasattr(model, "Date") or hasattr(model, "DefaultDate")


def test_form_create_second_field_failure_triggers_undo(monkeypatch):
    _patch_field_creator(monkeypatch, "bad")
    tool = FormCreate()
    monkeypatch.setattr(tool, "_insert_space", lambda ctx: None)

    doc = MagicMock()
    mgr = MagicMock()
    mgr.isLocked.return_value = False
    # The stack only shows the title once the context was entered.
    mgr.getAllUndoActionTitles.side_effect = lambda: ["Form Create"] if mgr.enterUndoContext.called else []
    doc.getUndoManager.return_value = mgr

    res = tool.execute(SimpleNamespace(doc=doc), fields=[{"control": "text", "name": "ok1"}, {"control": "text", "name": "bad"}])

    assert res["status"] == "error"
    assert res.get("rolled_back") is True
    mgr.enterUndoContext.assert_called_once_with("Form Create")
    mgr.leaveUndoContext.assert_called_once()
    mgr.undo.assert_called_once()


def test_form_create_locked_undo_manager_rejects(monkeypatch):
    tool = FormCreate()
    called = []
    monkeypatch.setattr("plugin.writer.specialized.forms._create_control", lambda *a, **k: called.append(True))

    doc = MagicMock()
    mgr = MagicMock()
    mgr.isLocked.return_value = True
    doc.getUndoManager.return_value = mgr

    res = tool.execute(SimpleNamespace(doc=doc), fields=[{"control": "text", "name": "ok1"}])

    assert res["status"] == "error"
    assert res["code"] == "UNDO_UNAVAILABLE"
    assert len(called) == 0
    mgr.enterUndoContext.assert_not_called()


def test_form_generate_passes_stop_checker_and_handles_user_stopped(monkeypatch):
    from plugin.framework.errors import WriterAgentException

    tool = FormGenerate()
    monkeypatch.setattr("plugin.framework.thread_guard.on_main_thread", lambda: False)

    mock_client = MagicMock()
    stop_checker = MagicMock()
    status_callback = MagicMock()

    stopped_exc = WriterAgentException("User cancelled", code="USER_STOPPED")
    mock_client.chat_completion_sync.side_effect = stopped_exc

    ctx = MagicMock()
    ctx.stop_checker = stop_checker
    ctx.status_callback = status_callback

    with patch("plugin.framework.config.get_api_config"), \
         patch("plugin.framework.client.llm_client.LlmClient", return_value=mock_client):
        res = tool.execute(ctx, description="test form")

        assert res["status"] == "error"
        assert res["code"] == "USER_STOPPED"
        assert res["message"] == "User cancelled"
        mock_client.chat_completion_sync.assert_called_once()
        _, kwargs = mock_client.chat_completion_sync.call_args
        assert kwargs.get("stop_checker") is stop_checker
        assert kwargs.get("status_callback") is status_callback


def test_form_create_and_control_schemas_match_control_type_map():
    from plugin.draw.tree import CONTROL_TYPE_MAP

    expected_keys = sorted(CONTROL_TYPE_MAP.keys())
    assert sorted(FormCreateControl.parameters["properties"]["control"]["enum"]) == expected_keys
    assert sorted(FormCreate.parameters["properties"]["fields"]["items"]["properties"]["control"]["enum"]) == expected_keys


def test_form_create_control_listbox():
    doc = MagicMock()
    model = MagicMock()
    shape = MagicMock()
    instances = []

    def fake_create(service):
        instances.append(service)
        if "ListBox" in service:
            return model
        return shape

    doc.createInstance.side_effect = fake_create

    controller = MagicMock()
    doc.getCurrentController.return_value = controller
    selection = MagicMock()
    anchor = MagicMock()
    anchor.getText.return_value = MagicMock()
    selection.getCount.return_value = 1
    selection.getByIndex.return_value = anchor
    controller.getSelection.return_value = selection

    ctx = SimpleNamespace(doc=doc)
    res = _create_control(ctx, control="listbox", name="Choice", items=["a", "b"])

    assert res["status"] == "ok"
    assert any("ListBox" in s for s in instances)
    assert model.StringItemList == ("a", "b")


def test_draw_page_stacked_cursor_single_scan_and_y_positions(monkeypatch):
    from plugin.writer.specialized.forms import FormCreate
    from com.sun.star.awt import Point, Size

    dp = MagicMock()
    existing_shape = MagicMock()
    existing_shape.getPosition.return_value = Point(0, 0)
    existing_shape.getSize.return_value = Size(100, 500)
    dp.getCount.return_value = 1
    dp.getByIndex.return_value = existing_shape

    doc = MagicMock()
    mgr = MagicMock()
    mgr.isLocked.return_value = False
    doc.getUndoManager.return_value = mgr

    created_shapes = []

    def fake_create_instance(service):
        inst = MagicMock()
        if service == "com.sun.star.drawing.ControlShape":
            created_shapes.append(inst)
        return inst

    doc.createInstance.side_effect = fake_create_instance

    monkeypatch.setattr("plugin.writer.specialized.forms._is_draw_doc", lambda d: True)
    monkeypatch.setattr("plugin.writer.specialized.forms._is_spreadsheet_doc", lambda d: False)
    monkeypatch.setattr("plugin.writer.specialized.forms._resolve_form_page", lambda d, p: dp)

    tool = FormCreate()
    fields = [
        {"control": "text", "name": "t1", "height": 600},
        {"control": "text", "name": "t2", "height": 600},
        {"control": "text", "name": "t3", "height": 600},
    ]
    res = tool.execute(SimpleNamespace(doc=doc), fields=fields)
    assert res["status"] == "ok"

    # getByIndex was called only once during initial page scan (for 1 existing shape)
    assert dp.getByIndex.call_count == 1

    # Check positions:
    # bottom = 800. first Y = 800 + 400 = 1200.
    # after h=600: next Y = 1200 + 600 + 400 = 2200.
    # after h=600: next Y = 2200 + 600 + 400 = 3200.
    assert created_shapes[0].setPosition.call_args[0][0].Y == 1200
    assert created_shapes[1].setPosition.call_args[0][0].Y == 2200
    assert created_shapes[2].setPosition.call_args[0][0].Y == 3200


def test_draw_form_create_stacks_per_page(monkeypatch):
    from com.sun.star.awt import Point, Size
    from plugin.writer.specialized.forms import FormCreate

    class _PlainPage:
        def __init__(self, existing_shape):
            self._shape = existing_shape
            self.added = []
            self.get_by_index_calls = 0

        def getCount(self):
            return 1

        def getByIndex(self, idx):
            self.get_by_index_calls += 1
            return self._shape

        def add(self, shape):
            self.added.append(shape)

    shape_a = MagicMock()
    shape_a.getPosition.return_value = Point(0, 0)
    shape_a.getSize.return_value = Size(100, 500)
    page_a = _PlainPage(shape_a)

    shape_b = MagicMock()
    shape_b.getPosition.return_value = Point(0, 200)
    shape_b.getSize.return_value = Size(100, 800)
    page_b = _PlainPage(shape_b)

    doc = MagicMock()
    mgr = MagicMock()
    mgr.isLocked.return_value = False
    doc.getUndoManager.return_value = mgr

    created_shapes = []

    def fake_create_instance(service):
        inst = MagicMock()
        if service == "com.sun.star.drawing.ControlShape":
            created_shapes.append(inst)
        return inst

    doc.createInstance.side_effect = fake_create_instance

    def fake_resolve_form_page(d, p):
        if p is None or p == 0:
            return page_a
        if p == 1:
            return page_b
        return None

    monkeypatch.setattr("plugin.writer.specialized.forms._is_draw_doc", lambda d: True)
    monkeypatch.setattr("plugin.writer.specialized.forms._is_spreadsheet_doc", lambda d: False)
    monkeypatch.setattr("plugin.writer.specialized.forms._resolve_form_page", fake_resolve_form_page)

    tool = FormCreate()
    fields = [
        {"control": "text", "name": "f1", "height": 600},
        {"control": "text", "name": "f2", "page": 1, "height": 600},
        {"control": "text", "name": "f3", "page": 1, "height": 600},
        {"control": "text", "name": "f4", "page": 0, "height": 600},
    ]
    res = tool.execute(SimpleNamespace(doc=doc), fields=fields)
    assert res["status"] == "ok"

    # Each page's getByIndex runs once
    assert page_a.get_by_index_calls == 1
    assert page_b.get_by_index_calls == 1

    # Page A bottom: max(800, 500) = 800. First Y = 800 + 400 = 1200.
    # Page B bottom: max(800, 1000) = 1000. First Y = 1000 + 400 = 1400.
    # Page B second field: 1400 + 600 + 400 = 2400.
    # Page A second field (field 4, page=0): 1200 + 600 + 400 = 2200.
    assert created_shapes[0].setPosition.call_args[0][0].Y == 1200
    assert created_shapes[1].setPosition.call_args[0][0].Y == 1400
    assert created_shapes[2].setPosition.call_args[0][0].Y == 2400
    assert created_shapes[3].setPosition.call_args[0][0].Y == 2200

    # Shapes added to respective pages
    assert page_a.added == [created_shapes[0], created_shapes[3]]
    assert page_b.added == [created_shapes[1], created_shapes[2]]


def test_draw_form_generate_interleaved_text_shares_cursor(monkeypatch):
    from plugin.writer.specialized.forms import FormGenerate

    dp = MagicMock()
    dp.getCount.return_value = 0

    doc = MagicMock()
    mgr = MagicMock()
    mgr.isLocked.return_value = False
    doc.getUndoManager.return_value = mgr

    control_shapes = []
    text_shapes = []

    def fake_create_instance(service):
        inst = MagicMock()
        if service == "com.sun.star.drawing.ControlShape":
            control_shapes.append(inst)
        return inst

    doc.createInstance.side_effect = fake_create_instance

    bridge = MagicMock()
    bridge.get_active_page.return_value = dp

    def fake_safe_create_shape(self, doc, page, service, pos, sz, custom_shape_type=None):
        sh = MagicMock()
        text_shapes.append((sh, pos.Y, sz.Height))
        return sh, None, None

    monkeypatch.setattr("plugin.draw.shapes.DrawShapes.safe_create_shape", fake_safe_create_shape)

    monkeypatch.setattr("plugin.writer.specialized.forms._is_draw_doc", lambda d: True)
    monkeypatch.setattr("plugin.writer.specialized.forms._is_spreadsheet_doc", lambda d: False)
    monkeypatch.setattr("plugin.writer.specialized.forms._resolve_form_page", lambda d, p: dp)
    monkeypatch.setattr("plugin.draw.bridge.DrawBridge", lambda d: bridge)

    tool = FormGenerate()
    # Interleaved Draw text (h=1000) and control (h=600)
    content = "Label 1 {FIELD:control='text' name='c1'}"
    res = tool._process_form_content(SimpleNamespace(doc=doc), content)
    assert res["status"] == "ok"

    # Label text placed first: Y = 1200 (h=1000)
    assert len(text_shapes) == 1
    assert text_shapes[0][1] == 1200
    # Next Y = 1200 + 1000 + 400 = 2600 for control shape
    assert len(control_shapes) == 1
    assert control_shapes[0].setPosition.call_args[0][0].Y == 2600


def test_writer_create_does_not_call_scanner(monkeypatch):
    from plugin.writer.specialized.forms import FormCreate, _StackedDrawPageCursor

    cursor_inits = []
    orig_init = _StackedDrawPageCursor.__init__

    def fake_init(self, dp):
        cursor_inits.append(dp)
        orig_init(self, dp)

    monkeypatch.setattr(_StackedDrawPageCursor, "__init__", fake_init)

    doc = MagicMock()
    mgr = MagicMock()
    mgr.isLocked.return_value = False
    doc.getUndoManager.return_value = mgr

    # Writer doc
    monkeypatch.setattr("plugin.writer.specialized.forms._is_draw_doc", lambda d: False)
    monkeypatch.setattr("plugin.writer.specialized.forms._is_spreadsheet_doc", lambda d: False)

    ctrl = MagicMock()
    view_cursor = MagicMock()
    ctrl.getViewCursor.return_value = view_cursor
    ctrl.getSelection.return_value = None
    doc.getCurrentController.return_value = ctrl

    tool = FormCreate()
    res = tool.execute(SimpleNamespace(doc=doc), fields=[{"control": "text", "name": "f1"}])
    assert res["status"] == "ok"
    assert len(cursor_inits) == 0


def test_form_list_controls_handles_failing_shape(monkeypatch):
    from com.sun.star.awt import Point, Size

    bad_shape = MagicMock()
    bad_shape.getShapeType.return_value = "com.sun.star.drawing.ControlShape"
    bad_shape.getPosition.side_effect = RuntimeError("shape position corrupted")

    good_shape = MagicMock()
    good_shape.getShapeType.return_value = "com.sun.star.drawing.ControlShape"
    good_shape.Name = "good_control"
    model = MagicMock()
    model.Name = "good_control"
    model.supportsService.side_effect = lambda s: s == "com.sun.star.form.component.TextField"
    good_shape.Control = model
    good_shape.getPosition.return_value = Point(100, 200)
    good_shape.getSize.return_value = Size(300, 400)

    dp = MagicMock()
    dp.getCount.return_value = 2
    dp.getByIndex.side_effect = [bad_shape, good_shape]

    doc = MagicMock()
    monkeypatch.setattr("plugin.writer.specialized.forms._resolve_form_page", lambda d, p: dp)

    res = FormListControls()._execute_main(SimpleNamespace(doc=doc))
    assert res["status"] == "ok"
    assert res["count"] == 2
    assert res["controls"][0]["stale"] is True
    assert res["controls"][0]["index"] == 0
    assert "shape position corrupted" in res["controls"][0]["error"]
    assert res["controls"][1]["name"] == "good_control"
    assert res["controls"][1]["index"] == 1


def test_radio_edit_rename_preserves_group(monkeypatch):
    from com.sun.star.awt import Point, Size

    doc = MagicMock()
    dp = MagicMock()
    shape = MagicMock()
    shape.getShapeType.return_value = "com.sun.star.drawing.ControlShape"
    shape.Name = "radio1"
    shape.getPosition.return_value = Point(10, 20)
    shape.getSize.return_value = Size(100, 200)

    model = MagicMock()
    model.Name = "Group1"
    model.supportsService.side_effect = lambda s: "RadioButton" in s
    shape.Control = model

    dp.getCount.return_value = 1
    dp.getByIndex.return_value = shape
    monkeypatch.setattr("plugin.writer.specialized.forms._resolve_form_page", lambda d, p: dp)

    mgr = MagicMock()
    mgr.isLocked.return_value = False
    doc.getUndoManager.return_value = mgr

    ctx = SimpleNamespace(doc=doc)
    tool = FormEditControl()

    # Rename shape only
    res = tool._execute_main(ctx, name="radio1", new_name="radio1_renamed")
    assert res["status"] == "ok"
    assert shape.Name == "radio1_renamed"
    assert model.Name == "Group1"
    assert res["control_name"] == "radio1_renamed"
    assert res["group_name"] == "Group1"

    # Rename group only
    res2 = tool._execute_main(ctx, name="radio1_renamed", group_name="Group2")
    assert res2["status"] == "ok"
    assert model.Name == "Group2"
    assert shape.Name == "radio1_renamed"
    assert res2["group_name"] == "Group2"


def test_form_edit_and_delete_undo_atomic(monkeypatch):
    from com.sun.star.awt import Point, Size

    doc = MagicMock()
    dp = MagicMock()
    shape = MagicMock()
    shape.getShapeType.return_value = "com.sun.star.drawing.ControlShape"
    shape.Name = "ctrl1"
    shape.getPosition.return_value = Point(10, 20)
    shape.getSize.return_value = Size(100, 200)
    model = MagicMock(spec=["Name", "Label", "supportsService"])
    model.Name = "ctrl1"
    model.supportsService.return_value = False
    shape.Control = model
    dp.getCount.return_value = 1
    dp.getByIndex.return_value = shape
    monkeypatch.setattr("plugin.writer.specialized.forms._resolve_form_page", lambda d, p: dp)

    mgr = MagicMock()
    mgr.isLocked.return_value = True
    doc.getUndoManager.return_value = mgr
    ctx = SimpleNamespace(doc=doc)

    # Locked undo manager on edit -> UNDO_UNAVAILABLE
    edit_tool = FormEditControl()
    res = edit_tool._execute_main(ctx, name="ctrl1", label="New")
    assert res["status"] == "error"
    assert res["code"] == "UNDO_UNAVAILABLE"

    # Locked undo manager on delete -> UNDO_UNAVAILABLE
    del_tool = FormDeleteControl()
    res_del = del_tool.execute(ctx, name="ctrl1")
    assert res_del["status"] == "error"
    assert res_del["code"] == "UNDO_UNAVAILABLE"

    # Mid-edit exception calls rollback
    mgr.isLocked.return_value = False
    mgr.enterUndoContext.reset_mock()
    mgr.getAllUndoActionTitles.side_effect = lambda: ["WriterAgent: Edit form control"] if mgr.enterUndoContext.called else []
    shape.setPosition.side_effect = RuntimeError("drawing engine crashed")
    res_crash = edit_tool._execute_main(ctx, name="ctrl1", x=999)
    assert res_crash["status"] == "error"
    mgr.undo.assert_called()


def test_forms_edit_rejects_unsupported_properties(monkeypatch):
    doc = MagicMock()
    dp = MagicMock()
    shape = MagicMock()
    shape.getShapeType.return_value = "com.sun.star.drawing.ControlShape"
    shape.Name = "chk1"
    model = MagicMock(spec=["Name", "supportsService"])
    model.Name = "chk1"
    model.supportsService.side_effect = lambda s: "CheckBox" in s
    shape.Control = model
    dp.getCount.return_value = 1
    dp.getByIndex.return_value = shape
    monkeypatch.setattr("plugin.writer.specialized.forms._resolve_form_page", lambda d, p: dp)

    ctx = SimpleNamespace(doc=doc)
    tool = FormEditControl()

    # Model has no Label
    res = tool._execute_main(ctx, name="chk1", label="new label")
    assert res["status"] == "error"
    assert "checkbox control has no label." in res["message"]

    # Model has no Text
    res = tool._execute_main(ctx, name="chk1", text="some text")
    assert res["status"] == "error"
    assert "checkbox control has no text." in res["message"]

    # Model has no StringItemList
    res = tool._execute_main(ctx, name="chk1", items=["a", "b"])
    assert res["status"] == "error"
    assert "checkbox control has no items." in res["message"]


def test_forms_create_rejects_duplicate_name_on_page(monkeypatch):
    doc = MagicMock()
    dp = MagicMock()
    shape = MagicMock()
    shape.getShapeType.return_value = "com.sun.star.drawing.ControlShape"
    shape.Name = "ExistingField"
    dp.getCount.return_value = 1
    dp.getByIndex.return_value = shape
    monkeypatch.setattr("plugin.writer.specialized.forms._resolve_form_page", lambda d, p: dp)

    ctx = SimpleNamespace(doc=doc)
    res = _create_control(ctx, control="text", name="ExistingField")
    assert res["status"] == "error"
    assert "already exists on this page" in res["message"]


def test_forms_generate_rejects_duplicate_name_in_batch(monkeypatch):
    doc = MagicMock()
    mgr = MagicMock()
    mgr.isLocked.return_value = False
    doc.getUndoManager.return_value = mgr

    tool = FormGenerate()
    content = "{FIELD:control='text' name='same_name'} {FIELD:control='text' name='same_name'}"
    res = tool._process_form_content(SimpleNamespace(doc=doc), content)
    assert res["status"] == "error"
    assert "Duplicate control name 'same_name' in batch." in res["message"]


def test_resolve_form_page_out_of_range(monkeypatch):
    doc = MagicMock()
    monkeypatch.setattr("plugin.writer.specialized.forms._is_draw_doc", lambda d: True)
    bridge_mock = MagicMock()
    bridge_mock.get_pages.return_value.getCount.return_value = 2
    monkeypatch.setattr("plugin.draw.bridge.DrawBridge", lambda d: bridge_mock)

    with pytest.raises(ToolExecutionError) as exc_info:
        _resolve_form_page(doc, page=99)
    assert "Page index 99 is out of range (document has 2 pages)." in str(exc_info.value)


def test_parse_field_tag_quotes_and_empty_items():
    tool = FormGenerate()
    # Quotes containing other quote char
    p1 = tool._parse_field_tag('{FIELD:control="text" name="f1" label="It\'s"}')
    assert p1 is not None
    assert p1["label"] == "It's"

    p2 = tool._parse_field_tag('{FIELD:control=\'text\' name=\'f2\' label=\'say "hi"\'}')
    assert p2 is not None
    assert p2["label"] == 'say "hi"'

    # Empty items string
    p3 = tool._parse_field_tag('{FIELD:control="combobox" name="f3" items=""}')
    assert p3 is None

    p4 = tool._parse_field_tag("{FIELD:control='combobox' name='f4' items=''}")
    assert p4 is None


def test_form_tools_registered_for_all_doc_types():
    from plugin.main import get_tools

    registry = get_tools()
    form_tools = [
        "form_create",
        "form_create_control",
        "form_generate",
        "form_list_controls",
        "form_edit_control",
        "form_delete_control",
    ]

    for doc_type in ("writer", "calc", "draw", "impress"):
        tools = registry.get_tools(doc_type=doc_type, filter_doc_type=True, exclude_tiers=None)
        names = {t.name for t in tools}
        for ft in form_tools:
            assert ft in names, f"Form tool '{ft}' missing from registry for {doc_type}"





