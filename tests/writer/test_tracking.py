from unittest.mock import MagicMock, patch, call


from plugin.calc.base import ToolCalcSpecialBase
from plugin.writer.tracking import (
    TrackChangesStart,
    TrackChangesStop,
    TrackChangesList,
    TrackChangesShow,
    ManageTrackedChanges,
)
import pytest

def _create_mock_ctx():
    ctx = MagicMock()
    
    doc = MagicMock()
    
    # Mock property value getter/setter
    props = {"RecordChanges": False}
    def _set_prop(name, val):
        props[name] = val
    def _get_prop(name):
        return props[name]
    doc.setPropertyValue.side_effect = _set_prop
    doc.getPropertyValue.side_effect = _get_prop

    # Default: empty redline table. The agent-self-resolution guard reads getCount() first; 0 means
    # "no changes pending" so bulk accept/reject is allowed (the pre-guard behavior for empty docs).
    doc.getRedlines.return_value.getCount.return_value = 0

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
    assert isinstance(ManageTrackedChanges(), ToolCalcSpecialBase)
    expected = (
        "com.sun.star.text.TextDocument",
        "com.sun.star.sheet.SpreadsheetDocument",
    )
    assert TrackChangesStart.uno_services == list(expected)
    assert TrackChangesList.uno_services == list(expected)


_REMOVED_TRACK_CHANGES_COMMENT_TOOLS = (
    "track_changes_comment_insert",
    "track_changes_comment_list",
    "track_changes_comment_delete",
)


def test_track_changes_comment_tools_removed():
    """Leftover annotation trio lived in tracking after comments domain landed; delete, do not alias."""
    import plugin.writer.tracking as tracking

    for cls_name in (
        "TrackChangesCommentInsert",
        "TrackChangesCommentList",
        "TrackChangesCommentDelete",
    ):
        assert not hasattr(tracking, cls_name)

    from plugin.scripting import writeragent_api

    assert writeragent_api.DOMAIN_TOOLS["tracking"] == [
        "manage_tracked_changes",
        "track_changes_list",
        "track_changes_show",
        "track_changes_start",
        "track_changes_stop",
    ]
    for name in _REMOVED_TRACK_CHANGES_COMMENT_TOOLS:
        assert not hasattr(writeragent_api.tracking, name)


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
    
    start = MagicMock()
    span = MagicMock()
    span.getString.return_value = "inserted clause"
    start.getText.return_value.createTextCursorByRange.return_value = span
    end = MagicMock()

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
        if prop == "RedlineStart":
            return start
        if prop == "RedlineEnd":
            return end
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
    ctx.doc.getRedlines.return_value.getCount.return_value = 1

    with patch("plugin.writer.tracking.describe_match_location", return_value="body"):
        res = tool.execute(ctx)
    assert res["status"] == "ok"
    assert res["count"] == 1
    assert len(res["changes"]) == 1
    
    change = res["changes"][0]
    assert change["index"] == 0
    assert change["text"] == "inserted clause"
    assert change["location"] == "body"
    assert change["RedlineType"] == "Insert"
    assert change["RedlineAuthor"] == "Test Author"
    assert change["date"] == "2024-02-15 10:30"

def test_track_changes_show():
    ctx, _, _, view_settings = _create_mock_ctx()
    tool = TrackChangesShow()

    props = {"RedlineDisplayType": 1}
    def _set_prop(name, val):
        props[name] = val
    def _get_prop(name):
        if name == "ShowChanges":
            return props.get("RedlineDisplayType") == 2
        return props.get(name)
    ctx.doc.setPropertyValue.side_effect = _set_prop
    ctx.doc.getPropertyValue.side_effect = _get_prop

    # show=True sets RedlineDisplayType to 2
    res_true = tool.execute(ctx, show=True)
    assert res_true["status"] == "ok"
    assert res_true["message"] == "Showing tracked changes markup."
    assert props["RedlineDisplayType"] == 2
    view_settings.setPropertyValue.assert_not_called()

    # show=False sets RedlineDisplayType to 1
    res_false = tool.execute(ctx, show=False)
    assert res_false["status"] == "ok"
    assert res_false["message"] == "Hiding tracked changes markup."
    assert props["RedlineDisplayType"] == 1
    view_settings.setPropertyValue.assert_not_called()


def test_track_changes_show_type_assertion():
    ctx, _, _, _ = _create_mock_ctx()
    tool = TrackChangesShow()
    with pytest.raises(AssertionError):
        tool.execute(ctx, show="not-a-bool")  # type: ignore[arg-type]
    with pytest.raises(KeyError):
        tool.execute(ctx)


def test_track_changes_show_readback_mismatch():
    ctx, _, _, _ = _create_mock_ctx()
    tool = TrackChangesShow()

    # Getter always reports type 2 and ShowChanges True
    ctx.doc.getPropertyValue.side_effect = lambda name: 2 if name == "RedlineDisplayType" else True
    res = tool.execute(ctx, show=False)
    assert res["status"] == "error"
    assert res["code"] == "TOOL_EXECUTION_ERROR"
    assert res["message"].startswith("Failed to set tracked-changes markup visibility")
    assert "Hiding tracked changes markup." not in res["message"]


def test_track_changes_show_calc_like_controller_returns_stub():
    """Spreadsheet controllers have no getViewSettings; Calc path is a no-op stub for now."""
    ctx = MagicMock()
    doc = MagicMock(spec=["getCurrentController", "setPropertyValue"])

    class CalcLikeController:
        pass

    doc.getCurrentController.return_value = CalcLikeController()
    ctx.doc = doc

    res = TrackChangesShow().execute(ctx, show=True)
    assert res["status"] == "ok"
    assert res.get("calc_track_changes_show_unsupported") is True
    assert "not supported" in res["message"].lower()
    doc.setPropertyValue.assert_not_called()


def test_track_changes_show_ignores_view_settings_without_redlines():
    ctx = MagicMock()
    doc = MagicMock(spec=["getCurrentController", "setPropertyValue"])
    controller = MagicMock()
    controller.getViewSettings.return_value = MagicMock()
    doc.getCurrentController.return_value = controller
    ctx.doc = doc

    res = TrackChangesShow().execute(ctx, show=True)
    assert res["status"] == "ok"
    assert res.get("calc_track_changes_show_unsupported") is True
    doc.setPropertyValue.assert_not_called()

@pytest.mark.parametrize(
    "action, value",
    [
        pytest.param("accept_all", ".uno:AcceptAllTrackedChanges", id="test_manage_tracked_changes_accept_all"),
        pytest.param("reject_all", ".uno:RejectAllTrackedChanges", id="test_manage_tracked_changes_reject_all"),
    ],
)
def test_manage_tracked_changes_accept_all(action, value):
    ctx, dispatcher, frame, _ = _create_mock_ctx()
    tool = ManageTrackedChanges()
    
    res = tool.execute(ctx, action=action)
    assert res["status"] == "ok"
    dispatcher.executeDispatch.assert_called_with(frame, value, "", 0, ())


def _redline_property_set():
    """A redline like the real UNO one: a property set with RedlineStart/RedlineEnd and NO
    getAnchor (the old tests pinned select(getAnchor()) — a method MagicMock fabricated but the
    real object never had, which is exactly why accept/reject by index was broken)."""
    start = MagicMock()
    span_cursor = MagicMock()
    start.getText.return_value.createTextCursorByRange.return_value = span_cursor
    props = {"RedlineStart": start, "RedlineEnd": MagicMock(), "RedlineComment": "user edit"}
    redline_mock = MagicMock(spec=["getPropertyValue"])
    redline_mock.getPropertyValue.side_effect = lambda p: props.get(p, "")
    return redline_mock, span_cursor


def test_manage_tracked_changes_bypasses_self_resolve_invariant():
    ctx, dispatcher, frame, _ = _create_mock_ctx()
    tool = ManageTrackedChanges()

    user_redline = _fake_redline("user edit")
    agent_redline = _fake_redline(_AGENT_COMMENT)

    # We mock it so that the dispatcher mistakenly resolves the agent edit!
    def _create_enum():
        enum = MagicMock()
        # The first time it is called (materialize), both are present.
        # The second time (after dispatch), only user_redline is present.
        call_count = getattr(_create_enum, "call_count", 0)
        if call_count >= 1:
            enum.hasMoreElements.side_effect = [True, False]
            enum.nextElement.return_value = user_redline
        else:
            enum.hasMoreElements.side_effect = [True, True, False]
            enum.nextElement.side_effect = [user_redline, agent_redline]
        _create_enum.call_count = call_count + 1
        return enum
    _create_enum.call_count = 0

    ctx.doc.getRedlines.return_value.createEnumeration.side_effect = _create_enum
    ctx.doc.getRedlines.return_value.getCount.side_effect = [2, 1]

    # To pass overlap check, make sure they don't overlap.
    # When text.compareRegionStarts is called, it returns 1.
    text_mock = MagicMock()
    text_mock.compareRegionStarts.return_value = 1

    start_mock1 = MagicMock()
    start_mock1.getText.return_value = text_mock

    start_mock2 = MagicMock()
    start_mock2.getText.return_value = text_mock

    # Provide mocked properties based on what's requested
    def _get_user_prop(name):
        if name in ("RedlineStart", "RedlineEnd"):
            return start_mock1
        return "user edit"
    user_redline.getPropertyValue.side_effect = _get_user_prop

    def _get_agent_prop(name):
        if name in ("RedlineStart", "RedlineEnd"):
            return start_mock2
        return _AGENT_COMMENT
    agent_redline.getPropertyValue.side_effect = _get_agent_prop

    # Try to accept index 0 (the user edit)
    # But because of our mock, the dispatcher removes the agent edit instead
    res = tool.execute(ctx, action="accept", index=0)

    assert res["status"] == "error"
    assert "Agent edit was resolved, which is forbidden." in res["message"]
    assert "Undo failed" not in res["message"]
    ctx.doc.getUndoManager().undo.assert_called_once()


def test_manage_tracked_changes_self_resolve_undo_fails():
    ctx, dispatcher, frame, _ = _create_mock_ctx()
    tool = ManageTrackedChanges()

    user_redline = _fake_redline("user edit")
    agent_redline = _fake_redline(_AGENT_COMMENT)

    def _create_enum():
        enum = MagicMock()
        call_count = getattr(_create_enum, "call_count", 0)
        if call_count >= 1:
            enum.hasMoreElements.side_effect = [True, False]
            enum.nextElement.return_value = user_redline
        else:
            enum.hasMoreElements.side_effect = [True, True, False]
            enum.nextElement.side_effect = [user_redline, agent_redline]
        _create_enum.call_count = call_count + 1
        return enum
    _create_enum.call_count = 0

    ctx.doc.getRedlines.return_value.createEnumeration.side_effect = _create_enum
    ctx.doc.getRedlines.return_value.getCount.side_effect = [2, 1]

    text_mock = MagicMock()
    text_mock.compareRegionStarts.return_value = 1

    start_mock1 = MagicMock()
    start_mock1.getText.return_value = text_mock

    start_mock2 = MagicMock()
    start_mock2.getText.return_value = text_mock

    user_redline.getPropertyValue.side_effect = lambda name: start_mock1 if name in ("RedlineStart", "RedlineEnd") else "user edit"
    agent_redline.getPropertyValue.side_effect = lambda name: start_mock2 if name in ("RedlineStart", "RedlineEnd") else _AGENT_COMMENT

    ctx.doc.getUndoManager.return_value.undo.side_effect = RuntimeError("undo crashed")

    with patch("plugin.writer.tracking.log.exception") as mock_log_exc:
        res = tool.execute(ctx, action="accept", index=0)

    assert res["status"] == "error"
    assert "Agent edit was resolved, which is forbidden." in res["message"]
    assert "Undo failed; the change is still resolved." in res["message"]
    mock_log_exc.assert_called_once_with("manage_tracked_changes: undo of a forbidden agent resolution failed")

def test_manage_tracked_changes_accept():
    ctx, dispatcher, frame, _ = _create_mock_ctx()
    tool = ManageTrackedChanges()

    r = _fake_redline("user edit")
    start = r.getPropertyValue("RedlineStart")
    span_cursor = MagicMock()
    start.getText.return_value.createTextCursorByRange.return_value = span_cursor
    items = [r]
    _install_redlines(ctx, items)

    def do_dispatch(*args):
        items.clear()
        return MagicMock()
    dispatcher.executeDispatch.side_effect = do_dispatch

    res = tool.execute(ctx, action="accept", index=0)
    assert res["status"] == "ok"
    assert "track_changes_list" in res["message"]
    assert "0" in res["message"]
    assert ctx.doc.getCurrentController().select.call_args_list == [
        call(span_cursor),
        call(ctx.doc.getCurrentController().getSelection.return_value),
    ]
    dispatcher.executeDispatch.assert_called_with(frame, ".uno:AcceptTrackedChange", "", 0, ())


def test_manage_tracked_changes_reject():
    ctx, dispatcher, frame, _ = _create_mock_ctx()
    tool = ManageTrackedChanges()

    r = _fake_redline("user edit")
    start = r.getPropertyValue("RedlineStart")
    span_cursor = MagicMock()
    start.getText.return_value.createTextCursorByRange.return_value = span_cursor
    items = [r]
    _install_redlines(ctx, items)

    def do_dispatch(*args):
        items.clear()
        return MagicMock()
    dispatcher.executeDispatch.side_effect = do_dispatch

    res = tool.execute(ctx, action="reject", index=0)
    assert res["status"] == "ok"
    assert "track_changes_list" in res["message"]
    assert "0" in res["message"]
    assert ctx.doc.getCurrentController().select.call_args_list == [
        call(span_cursor),
        call(ctx.doc.getCurrentController().getSelection.return_value),
    ]
    dispatcher.executeDispatch.assert_called_with(frame, ".uno:RejectTrackedChange", "", 0, ())


def test_manage_tracked_changes_accept_requires_index():
    ctx, dispatcher, _frame, _ = _create_mock_ctx()
    res = ManageTrackedChanges().execute(ctx, action="accept")
    assert res["status"] == "error"
    assert "index" in res["message"].lower()
    dispatcher.executeDispatch.assert_not_called()


def test_manage_tracked_changes_invalid_action():
    ctx, dispatcher, _frame, _ = _create_mock_ctx()
    res = ManageTrackedChanges().execute(ctx, action="squash")
    assert res["status"] == "error"
    dispatcher.executeDispatch.assert_not_called()


def test_manage_tracked_changes_index_type_assertion():
    ctx, dispatcher, _frame, _ = _create_mock_ctx()
    tool = ManageTrackedChanges()
    with pytest.raises(AssertionError):
        tool.execute(ctx, action="accept", index="two")  # type: ignore[arg-type]
    with pytest.raises(AssertionError):
        tool.execute(ctx, action="accept", index=True)  # type: ignore[arg-type]
    dispatcher.executeDispatch.assert_not_called()

# --- Agent-self-resolution guard (B3): the agent must never accept/reject its OWN edits --------

from plugin.writer.review_scan import TOKEN_PREFIX

_AGENT_COMMENT = TOKEN_PREFIX + "sess123:0"


def _fake_redline(comment, raise_comment=False, text_mock=None):
    rl = MagicMock()
    start = MagicMock()
    if text_mock is not None:
        start.getText.return_value = text_mock
    end = MagicMock()
    if text_mock is not None:
        end.getText.return_value = text_mock
    props = {
        "RedlineComment": comment,
        "RedlineIdentifier": f"id:{comment}",
        "RedlineType": "Insert",
        "RedlineAuthor": "Author",
        "RedlineDateTime": None,
        "RedlineStart": start,
        "RedlineEnd": end,
    }

    def _get(name):
        if raise_comment and name == "RedlineComment":
            raise RuntimeError("comment read boom")
        return props.get(name, "")

    rl.getPropertyValue.side_effect = _get
    return rl


def _install_redlines(ctx, items, count=None):
    """Wire ctx.doc.getRedlines() to enumerate *items* (count override simulates a truncated scan)."""
    rls = ctx.doc.getRedlines.return_value
    if count is not None:
        rls.getCount.side_effect = None
        rls.getCount.return_value = count
    else:
        rls.getCount.side_effect = lambda: len(items)

    def _mk_enum(*_a, **_k):
        seq = list(items)
        enum = MagicMock()
        enum.hasMoreElements.side_effect = lambda: len(seq) > 0
        enum.nextElement.side_effect = lambda: seq.pop(0)
        return enum

    rls.createEnumeration.side_effect = _mk_enum
    return rls


def test_accept_all_blocked_when_agent_change_pending():
    ctx, dispatcher, _frame, _ = _create_mock_ctx()
    _install_redlines(ctx, [_fake_redline(_AGENT_COMMENT)])
    res = ManageTrackedChanges().execute(ctx, action="accept_all")
    assert res["status"] == "error"
    assert "agent edit" in res["message"].lower()
    dispatcher.executeDispatch.assert_not_called()


def test_reject_all_blocked_when_agent_change_pending():
    ctx, dispatcher, _frame, _ = _create_mock_ctx()
    _install_redlines(ctx, [_fake_redline(_AGENT_COMMENT)])
    res = ManageTrackedChanges().execute(ctx, action="reject_all")
    assert res["status"] == "error"
    dispatcher.executeDispatch.assert_not_called()


def test_accept_all_allowed_with_only_user_redlines():
    # The user's OWN tracked changes (no wa-review token) may be bulk-resolved on request.
    ctx, dispatcher, frame, _ = _create_mock_ctx()
    items = [_fake_redline("user edit")]
    _install_redlines(ctx, items)
    dispatcher.executeDispatch.side_effect = lambda *_: items.clear()
    res = ManageTrackedChanges().execute(ctx, action="accept_all")
    assert res["status"] == "ok"
    dispatcher.executeDispatch.assert_called_with(frame, ".uno:AcceptAllTrackedChanges", "", 0, ())


def test_accept_all_blocked_when_scan_unreliable():
    # Redlines present (count=2) but only 1 enumerates -> can't prove no agent change -> fail closed.
    ctx, dispatcher, _frame, _ = _create_mock_ctx()
    _install_redlines(ctx, [_fake_redline("")], count=2)
    res = ManageTrackedChanges().execute(ctx, action="accept_all")
    assert res["status"] == "error"
    dispatcher.executeDispatch.assert_not_called()


def test_single_accept_blocked_on_agent_redline():
    ctx, dispatcher, _frame, _ = _create_mock_ctx()
    _install_redlines(ctx, [_fake_redline(_AGENT_COMMENT)])
    res = ManageTrackedChanges().execute(ctx, action="accept", index=0)
    assert res["status"] == "error"
    assert "agent edit" in res["message"].lower()
    dispatcher.executeDispatch.assert_not_called()
    ctx.doc.getCurrentController().select.assert_not_called()


def test_single_reject_blocked_on_agent_redline():
    ctx, dispatcher, _frame, _ = _create_mock_ctx()
    _install_redlines(ctx, [_fake_redline(_AGENT_COMMENT)])
    res = ManageTrackedChanges().execute(ctx, action="reject", index=0)
    assert res["status"] == "error"
    dispatcher.executeDispatch.assert_not_called()


def test_single_accept_allowed_on_user_redline():
    ctx, dispatcher, frame, _ = _create_mock_ctx()
    text_mock = MagicMock()
    text_mock.compareRegionStarts.return_value = 1
    redline = _fake_redline("user edit", text_mock=text_mock)
    items = [redline]
    _install_redlines(ctx, items)
    dispatcher.executeDispatch.side_effect = lambda *_: items.clear()
    res = ManageTrackedChanges().execute(ctx, action="accept", index=0)
    assert res["status"] == "ok"
    assert "track_changes_list" in res["message"]
    assert "0" in res["message"]
    dispatcher.executeDispatch.assert_called_with(frame, ".uno:AcceptTrackedChange", "", 0, ())


def test_single_accept_allowed_on_touching_ranges():
    ctx, dispatcher, frame, _ = _create_mock_ctx()
    text_mock = MagicMock()
    # 0 means they are strictly adjacent (touching)
    text_mock.compareRegionStarts.return_value = 0
    text_mock.compareRegionEnds.return_value = 0

    r1 = _fake_redline("user1", text_mock=text_mock)
    r2 = _fake_redline("user2", text_mock=text_mock)

    items = [r1, r2]
    _install_redlines(ctx, items)
    dispatcher.executeDispatch.side_effect = lambda *_: items.remove(r1)

    res = ManageTrackedChanges().execute(ctx, action="accept", index=0)
    assert res["status"] == "ok"
    assert "track_changes_list" in res["message"]
    assert "0" in res["message"]
    dispatcher.executeDispatch.assert_called_with(frame, ".uno:AcceptTrackedChange", "", 0, ())

def test_single_accept_blocked_on_overlap():
    ctx, dispatcher, frame, _ = _create_mock_ctx()
    r1 = _fake_redline("")
    r2 = _fake_redline("")

    text_mock = MagicMock()
    # -1 means they genuinely overlap (one starts before the other ends but isn't adjacent)
    text_mock.compareRegionStarts.return_value = -1
    text_mock.compareRegionEnds.return_value = -1

    start_mock1 = MagicMock()
    start_mock1.getText.return_value = text_mock
    r1.getPropertyValue.side_effect = lambda prop: start_mock1 if prop == "RedlineStart" else start_mock1

    start_mock2 = MagicMock()
    start_mock2.getText.return_value = text_mock
    r2.getPropertyValue.side_effect = lambda prop: start_mock2 if prop == "RedlineStart" else start_mock2

    _install_redlines(ctx, [r1, r2])
    ctx.doc.getRedlines.return_value.getCount.side_effect = [2, 1]

    res = ManageTrackedChanges().execute(ctx, action="accept", index=0)
    assert res["status"] == "error"
    assert "overlaps with another change" in res["message"]
    dispatcher.executeDispatch.assert_not_called()


def test_single_accept_blocked_when_comment_unreadable():
    # Fail closed: if we can't read the change's metadata we can't prove it isn't an agent change.
    ctx, dispatcher, _frame, _ = _create_mock_ctx()
    _install_redlines(ctx, [_fake_redline("", raise_comment=True)])
    res = ManageTrackedChanges().execute(ctx, action="accept", index=0)
    assert res["status"] == "error"
    dispatcher.executeDispatch.assert_not_called()

def test_single_accept_blocked_when_bounds_unreadable():
    # Fail closed: if we cannot read the bounds of another redline, we cannot prove
    # it doesn't overlap.
    ctx, dispatcher, _frame, _ = _create_mock_ctx()
    r1 = _fake_redline("")
    r2 = _fake_redline("")

    start_mock1 = MagicMock()
    # Mock getText to just return a dummy
    start_mock1.getText.return_value = MagicMock()
    r1.getPropertyValue.side_effect = lambda prop: start_mock1 if prop == "RedlineStart" else start_mock1

    # For r2, throw when trying to get RedlineStart
    def r2_prop(prop):
        if prop == "RedlineStart":
            raise RuntimeError("bounds read boom")
        return MagicMock()
    r2.getPropertyValue.side_effect = r2_prop

    _install_redlines(ctx, [r1, r2])
    ctx.doc.getRedlines.return_value.getCount.side_effect = [2, 1]

    res = ManageTrackedChanges().execute(ctx, action="accept", index=0)
    assert res["status"] == "error"
    assert "Failed to read bounds" in res["message"]
    dispatcher.executeDispatch.assert_not_called()


def test_track_changes_list_no_get_redlines():
    class NoRedlinesDoc:
        def getPropertyValue(self, name):
            if name == "RecordChanges":
                return True
            raise KeyError(name)

    ctx = MagicMock()
    ctx.doc = NoRedlinesDoc()
    res = TrackChangesList().execute(ctx)
    assert res["status"] == "ok"
    assert res["recording"] is True
    assert res["track_changes_list_unsupported"] is True
    assert "count" not in res
    assert "changes" not in res
    assert res["message"] == "This document does not expose a redlines API, so this is not an empty change list. track_changes_start and track_changes_stop still control recording."


def test_track_changes_list_record_changes_read_fails():
    ctx, _, _, _ = _create_mock_ctx()
    ctx.doc.getPropertyValue.side_effect = RuntimeError("cannot read property")
    enum_mock = MagicMock()
    enum_mock.hasMoreElements.return_value = False
    ctx.doc.getRedlines.return_value.createEnumeration.return_value = enum_mock

    with patch("plugin.writer.tracking.log.exception") as mock_log_exc:
        res = TrackChangesList().execute(ctx)
    assert res["status"] == "ok"
    assert res["recording"] is None
    assert res["count"] == 0
    assert res["changes"] == []
    assert "track_changes_list_unsupported" not in res
    mock_log_exc.assert_called_once_with("track_changes_list: could not read RecordChanges")


@pytest.mark.parametrize("action", ["accept_all", "reject_all"])
def test_manage_tracked_changes_bulk_unsupported_when_dispatch_returns_none(action):
    ctx = MagicMock()
    doc = MagicMock(spec=["getCurrentController"])
    dispatcher = MagicMock()
    dispatcher.executeDispatch.return_value = None
    smgr = MagicMock()
    smgr.createInstanceWithContext.return_value = dispatcher
    ctx.ctx.ServiceManager = smgr
    controller = MagicMock()
    frame = MagicMock()
    controller.getFrame.return_value = frame
    doc.getCurrentController.return_value = controller
    ctx.doc = doc

    res = ManageTrackedChanges().execute(ctx, action=action)
    assert res["status"] == "error"
    assert res["code"] == "UNSUPPORTED"
    assert res["tracked_changes_bulk_unsupported"] is True
    assert res["message"] == "Accepting or rejecting all tracked changes is not supported on this document."


def test_single_accept_wrong_redline_is_undone():
    ctx, dispatcher, frame, _ = _create_mock_ctx()
    text_mock = MagicMock()
    text_mock.compareRegionStarts.return_value = 1
    r0 = _fake_redline("user 0", text_mock=text_mock)
    r1 = _fake_redline("user 1", text_mock=text_mock)

    items = [r0, r1]
    _install_redlines(ctx, items)
    dispatcher.executeDispatch.side_effect = lambda *_: items.remove(r1)

    res = ManageTrackedChanges().execute(ctx, action="accept", index=0)
    assert res["status"] == "error"
    assert "different tracked change" in res["message"]
    dispatcher.executeDispatch.assert_called_once()
    ctx.doc.getUndoManager().undo.assert_called_once()


def test_single_accept_silent_when_nothing_changes():
    ctx, dispatcher, frame, _ = _create_mock_ctx()
    text_mock = MagicMock()
    text_mock.compareRegionStarts.return_value = 1
    r0 = _fake_redline("user 0", text_mock=text_mock)

    items = [r0]
    _install_redlines(ctx, items)

    res = ManageTrackedChanges().execute(ctx, action="accept", index=0)
    assert res["status"] == "error"
    assert res["message"] == "Failed to resolve tracked change: operation silently failed."
    ctx.doc.getUndoManager().undo.assert_not_called()


def test_single_accept_skips_redline_in_another_text():
    ctx, dispatcher, frame, _ = _create_mock_ctx()

    class PlainTextTarget:
        def createTextCursorByRange(self, rng):
            return MagicMock()

        def compareRegionStarts(self, *args):
            return 1

    class PlainTextOther:
        def compareRegionStarts(self, *args):
            raise AssertionError("other_text.compareRegionStarts should not be called")

    target_text = PlainTextTarget()
    other_text = PlainTextOther()

    target_start = MagicMock()
    target_start.getText.return_value = target_text
    r0 = _fake_redline("user 0")
    r0.getPropertyValue.side_effect = lambda prop: target_start if prop in ("RedlineStart", "RedlineEnd") else {
        "RedlineComment": "user 0",
        "RedlineIdentifier": "id:user 0",
        "RedlineType": "Insert",
        "RedlineAuthor": "Author",
        "RedlineDateTime": None,
    }.get(prop, "")

    other_start = MagicMock()
    other_start.getText.return_value = other_text
    r1 = _fake_redline("user 1")
    r1.getPropertyValue.side_effect = lambda prop: other_start if prop in ("RedlineStart", "RedlineEnd") else {
        "RedlineComment": "user 1",
        "RedlineIdentifier": "id:user 1",
        "RedlineType": "Insert",
        "RedlineAuthor": "Author",
        "RedlineDateTime": None,
    }.get(prop, "")

    items = [r0, r1]
    _install_redlines(ctx, items)
    dispatcher.executeDispatch.side_effect = lambda *_: items.remove(r0)

    res = ManageTrackedChanges().execute(ctx, action="accept", index=0)
    assert res["status"] == "ok"
    assert "track_changes_list" in res["message"]
    assert "0" in res["message"]
    dispatcher.executeDispatch.assert_called_with(frame, ".uno:AcceptTrackedChange", "", 0, ())
    assert items == [r1]


def test_single_accept_same_text_compare_error_still_refuses():
    ctx, dispatcher, frame, _ = _create_mock_ctx()

    class PlainText:
        def createTextCursorByRange(self, rng):
            return MagicMock()

        def compareRegionStarts(self, *args):
            raise RuntimeError("cross")

    same_text = PlainText()
    start_mock = MagicMock()
    start_mock.getText.return_value = same_text

    r0 = _fake_redline("user 0")
    r0.getPropertyValue.side_effect = lambda prop: start_mock if prop in ("RedlineStart", "RedlineEnd") else {
        "RedlineComment": "user 0",
        "RedlineIdentifier": "id:user 0",
        "RedlineType": "Insert",
        "RedlineAuthor": "Author",
        "RedlineDateTime": None,
    }.get(prop, "")

    r1 = _fake_redline("user 1")
    r1.getPropertyValue.side_effect = lambda prop: start_mock if prop in ("RedlineStart", "RedlineEnd") else {
        "RedlineComment": "user 1",
        "RedlineIdentifier": "id:user 1",
        "RedlineType": "Insert",
        "RedlineAuthor": "Author",
        "RedlineDateTime": None,
    }.get(prop, "")

    items = [r0, r1]
    _install_redlines(ctx, items)

    res = ManageTrackedChanges().execute(ctx, action="accept", index=0)
    assert res["status"] == "error"
    assert "Failed to read bounds" in res["message"]
    dispatcher.executeDispatch.assert_not_called()


def test_track_changes_list_truncated_enumeration_is_an_error():
    ctx, _, _, _ = _create_mock_ctx()
    tool = TrackChangesList()

    r0 = _fake_redline("user 0")
    _install_redlines(ctx, [r0], count=2)

    res = tool.execute(ctx)
    assert res["status"] == "error"
    assert "saw 1 of 2" in res["message"]
    assert "changes" not in res
    assert "count" not in res


def test_single_accept_truncated_enumeration_does_not_dispatch():
    ctx, dispatcher, frame, _ = _create_mock_ctx()
    r0 = _fake_redline("user 0")
    _install_redlines(ctx, [r0], count=2)

    res = ManageTrackedChanges().execute(ctx, action="accept", index=0)
    assert res["status"] == "error"
    assert "saw 1 of 2" in res["message"]
    dispatcher.executeDispatch.assert_not_called()


def test_accept_all_writer_ignores_none_dispatch_when_count_clears():
    ctx, dispatcher, frame, _ = _create_mock_ctx()
    redline = _fake_redline("user edit")
    items = [redline]
    _install_redlines(ctx, items)

    def do_dispatch(*args):
        items.clear()
        return None

    dispatcher.executeDispatch.side_effect = do_dispatch

    res = ManageTrackedChanges().execute(ctx, action="accept_all")
    assert res["status"] == "ok"
    assert res["message"] == "All tracked changes accepted."


def test_accept_all_writer_errors_when_count_unchanged():
    ctx, dispatcher, frame, _ = _create_mock_ctx()
    redline = _fake_redline("user edit")
    items = [redline]
    _install_redlines(ctx, items)
    dispatcher.executeDispatch.return_value = MagicMock()

    res = ManageTrackedChanges().execute(ctx, action="accept_all")
    assert res["status"] == "error"
    assert res["message"] == "Failed to resolve all tracked changes."
    ctx.doc.getUndoManager().undo.assert_not_called()


def test_tracking_disposal_is_document_disposed():
    from plugin.framework.errors import DocumentDisposedError

    tool = TrackChangesList()
    ctx, _, _, _ = _create_mock_ctx()
    ctx.doc.getRedlines.side_effect = DocumentDisposedError("gone")
    res = tool.execute_safe(ctx)
    assert res["status"] == "error"
    assert res["code"] == "DOCUMENT_DISPOSED"

