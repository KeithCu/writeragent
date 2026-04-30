from unittest.mock import MagicMock

from plugin.tests.testing_utils import setup_uno_mocks
setup_uno_mocks()

from plugin.modules.calc.base import ToolCalcSpecialBase
from plugin.modules.writer.tracking import (
    TrackChangesStart,
    TrackChangesStop,
    TrackChangesList,
    TrackChangesShow,
    TrackChangesAcceptAll,
    TrackChangesRejectAll,
    TrackChangesAccept,
    TrackChangesReject,
    TrackChangesCommentInsert,
    TrackChangesCommentList,
    TrackChangesCommentDelete,
)

def _create_mock_ctx():
    ctx = MagicMock()
    
    doc = MagicMock()
    # Mocking hasattr for getRedlines
    doc.hasattr.side_effect = lambda name: name == "getRedlines"
    
    # Mock property value getter/setter
    props = {"RecordChanges": False}
    def _set_prop(name, val):
        props[name] = val
    def _get_prop(name):
        return props[name]
    doc.setPropertyValue.side_effect = _set_prop
    doc.getPropertyValue.side_effect = _get_prop
    
    ctx.doc = doc
    
    # Mock dispatcher and frame for accept/reject tests
    dispatcher = MagicMock()
    smgr = MagicMock()
    smgr.createInstanceWithContext.return_value = dispatcher
    ctx.ctx.ServiceManager = smgr
    
    frame = MagicMock()
    controller = MagicMock()
    controller.getFrame.return_value = frame
    
    view_settings = MagicMock()
    controller.getViewSettings.return_value = view_settings
    
    doc.getCurrentController.return_value = controller
    
    return ctx, dispatcher, frame, view_settings

def test_track_changes_tools_support_calc_document_type():
    assert isinstance(TrackChangesStart(), ToolCalcSpecialBase)
    assert isinstance(TrackChangesAccept(), ToolCalcSpecialBase)
    expected = (
        "com.sun.star.text.TextDocument",
        "com.sun.star.sheet.SpreadsheetDocument",
    )
    assert TrackChangesStart.uno_services == list(expected)
    assert TrackChangesList.uno_services == list(expected)


def test_track_changes_comment_tools_writer_only():
    assert not isinstance(TrackChangesCommentInsert(), ToolCalcSpecialBase)
    assert TrackChangesCommentInsert.uno_services == ["com.sun.star.text.TextDocument"]


def test_track_changes_start():
    ctx, _, _, _ = _create_mock_ctx()
    tool = TrackChangesStart()
    
    res = tool.execute(ctx)
    assert res["status"] == "ok"
    assert "Started" in res["message"]
    assert ctx.doc.getPropertyValue("RecordChanges") is True

def test_track_changes_stop():
    ctx, _, _, _ = _create_mock_ctx()
    tool = TrackChangesStop()
    
    res = tool.execute(ctx)
    assert res["status"] == "ok"
    assert "Stopped" in res["message"]
    assert ctx.doc.getPropertyValue("RecordChanges") is False

def test_track_changes_list():
    ctx, _, _, _ = _create_mock_ctx()
    tool = TrackChangesList()
    
    # Mock redlines
    redline_mock = MagicMock()
    def _get_redline_prop(prop):
        if prop == "RedlineDateTime":
            dt = MagicMock()
            dt.Year = 2024
            dt.Month = 2
            dt.Day = 15
            dt.Hours = 10
            dt.Minutes = 30
            return dt
        return {
            "RedlineType": "Insert",
            "RedlineAuthor": "Test Author",
            "RedlineComment": "Test Comment",
            "RedlineIdentifier": "id_1"
        }.get(prop)
    redline_mock.getPropertyValue.side_effect = _get_redline_prop
    
    enum_mock = MagicMock()
    enum_mock.hasMoreElements.side_effect = [True, False]
    enum_mock.nextElement.return_value = redline_mock
    
    ctx.doc.getRedlines.return_value.createEnumeration.return_value = enum_mock
    
    res = tool.execute(ctx)
    assert res["status"] == "ok"
    assert res["count"] == 1
    assert len(res["changes"]) == 1
    
    change = res["changes"][0]
    assert change["index"] == 0
    assert change["RedlineType"] == "Insert"
    assert change["RedlineAuthor"] == "Test Author"
    assert change["date"] == "2024-02-15 10:30"

def test_track_changes_show():
    ctx, _, _, view_settings = _create_mock_ctx()
    tool = TrackChangesShow()
    
    # Missing arg
    res_err = tool.execute(ctx)
    assert res_err["status"] == "error"
    assert "Missing required parameter" in res_err["message"]
    
    # valid
    res = tool.execute(ctx, show=True)
    assert res["status"] == "ok"
    view_settings.setPropertyValue.assert_called_with("ShowChangesInMargin", True)


def test_track_changes_show_calc_like_controller_returns_stub():
    """Spreadsheet controllers have no getViewSettings; Calc path is a no-op stub for now."""
    ctx = MagicMock()
    doc = MagicMock()

    class CalcLikeController:
        pass

    doc.getCurrentController.return_value = CalcLikeController()
    ctx.doc = doc

    res = TrackChangesShow().execute(ctx, show=True)
    assert res["status"] == "ok"
    assert res.get("calc_track_changes_show_unsupported") is True
    assert "not supported" in res["message"].lower()

def test_track_changes_accept_all():
    ctx, dispatcher, frame, _ = _create_mock_ctx()
    tool = TrackChangesAcceptAll()
    
    res = tool.execute(ctx)
    assert res["status"] == "ok"
    dispatcher.executeDispatch.assert_called_with(frame, ".uno:AcceptAllTrackedChanges", "", 0, ())

def test_track_changes_reject_all():
    ctx, dispatcher, frame, _ = _create_mock_ctx()
    tool = TrackChangesRejectAll()
    
    res = tool.execute(ctx)
    assert res["status"] == "ok"
    dispatcher.executeDispatch.assert_called_with(frame, ".uno:RejectAllTrackedChanges", "", 0, ())

def test_track_changes_accept():
    ctx, dispatcher, frame, _ = _create_mock_ctx()
    tool = TrackChangesAccept()
    
    # Mock redlines
    redline_mock = MagicMock()
    anchor_mock = MagicMock()
    redline_mock.getAnchor.return_value = anchor_mock
    
    enum_mock = MagicMock()
    enum_mock.hasMoreElements.side_effect = [True, False]
    enum_mock.nextElement.return_value = redline_mock
    
    ctx.doc.getRedlines.return_value.createEnumeration.return_value = enum_mock
    
    res = tool.execute(ctx, index=0)
    assert res["status"] == "ok"
    ctx.doc.getCurrentController().select.assert_called_with(anchor_mock)
    dispatcher.executeDispatch.assert_called_with(frame, ".uno:AcceptTrackedChange", "", 0, ())

def test_track_changes_reject():
    ctx, dispatcher, frame, _ = _create_mock_ctx()
    tool = TrackChangesReject()
    
    # Mock redlines
    redline_mock = MagicMock()
    anchor_mock = MagicMock()
    redline_mock.getAnchor.return_value = anchor_mock
    
    enum_mock = MagicMock()
    enum_mock.hasMoreElements.side_effect = [True, False]
    enum_mock.nextElement.return_value = redline_mock
    
    ctx.doc.getRedlines.return_value.createEnumeration.return_value = enum_mock
    
    res = tool.execute(ctx, index=0)
    assert res["status"] == "ok"
    ctx.doc.getCurrentController().select.assert_called_with(anchor_mock)
    dispatcher.executeDispatch.assert_called_with(frame, ".uno:RejectTrackedChange", "", 0, ())

# --- Comment Tests ---

def test_comment_insert():
    ctx, _, _, _ = _create_mock_ctx()
    tool = TrackChangesCommentInsert()
    
    res = tool.execute(ctx, content="test comment", author="Jules")
    assert res["status"] == "ok"
    
    # verify annotation creation and properties
    ctx.doc.createInstance.assert_called_with("com.sun.star.text.textfield.Annotation")
    anno_mock = ctx.doc.createInstance.return_value
    
    # Check that properties were set
    calls = anno_mock.setPropertyValue.call_args_list
    assert any(c[0][0] == "Content" and c[0][1] == "test comment" for c in calls)
    assert any(c[0][0] == "Author" and c[0][1] == "Jules" for c in calls)
    
    # verify insertTextContent called
    view_cursor_mock = ctx.doc.getCurrentController().getViewCursor.return_value
    text_mock = view_cursor_mock.getText.return_value
    text_mock.insertTextContent.assert_called_with(view_cursor_mock, anno_mock, True)

def test_comment_list():
    ctx, _, _, _ = _create_mock_ctx()
    tool = TrackChangesCommentList()
    
    # Mock comments
    comment_mock = MagicMock()
    comment_mock.supportsService.return_value = True
    
    def _get_comment_prop(prop):
        if prop == "Date":
            dt = MagicMock()
            dt.Year = 2024
            dt.Month = 2
            dt.Day = 15
            return dt
        return {
            "Author": "Test Author",
            "Content": "Test Content",
        }.get(prop)
    comment_mock.getPropertyValue.side_effect = _get_comment_prop
    
    enum_mock = MagicMock()
    enum_mock.hasMoreElements.side_effect = [True, False]
    enum_mock.nextElement.return_value = comment_mock
    
    ctx.doc.getTextFields.return_value.createEnumeration.return_value = enum_mock
    
    res = tool.execute(ctx)
    assert res["status"] == "ok"
    assert res["count"] == 1
    assert len(res["comments"]) == 1
    
    c = res["comments"][0]
    assert c["index"] == 0
    assert c["author"] == "Test Author"
    assert c["content"] == "Test Content"
    assert c["date"] == "2024-02-15"

def test_comment_delete():
    ctx, _, _, _ = _create_mock_ctx()
    tool = TrackChangesCommentDelete()
    
    comment_mock = MagicMock()
    comment_mock.supportsService.return_value = True
    
    enum_mock = MagicMock()
    enum_mock.hasMoreElements.side_effect = [True, False]
    enum_mock.nextElement.return_value = comment_mock
    
    ctx.doc.getTextFields.return_value.createEnumeration.return_value = enum_mock
    
    res = tool.execute(ctx, index=0)
    assert res["status"] == "ok"
    comment_mock.dispose.assert_called_once()