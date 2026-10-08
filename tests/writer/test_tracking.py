from unittest.mock import MagicMock, patch


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
    
    with patch("plugin.writer.search._describe_match_location", return_value="body"):
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
        # The first time it is called (to get redlines list), both are present.
        # The second time (before dispatch), both are present.
        # The third time (after dispatch), only user_redline is present.
        call_count = getattr(_create_enum, "call_count", 0)
        if call_count >= 2:
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
    assert "Agent edit was resolved" in res["message"]
    ctx.doc.getUndoManager().undo.assert_called_once()

def test_manage_tracked_changes_accept():
    ctx, dispatcher, frame, _ = _create_mock_ctx()
    tool = ManageTrackedChanges()

    redline_mock, span_cursor = _redline_property_set()

    # We need to mock createEnumeration to return a new enum each time.
    def _create_enum():
        enum = MagicMock()
        enum.hasMoreElements.side_effect = [True, False]
        enum.nextElement.return_value = redline_mock
        return enum

    ctx.doc.getRedlines.return_value.createEnumeration.side_effect = _create_enum

    # Mock redlines.getCount() before and after
    ctx.doc.getRedlines.return_value.getCount.side_effect = [1, 0]

    res = tool.execute(ctx, action="accept", index=0)
    assert res["status"] == "ok"
    ctx.doc.getCurrentController().select.assert_called_with(span_cursor)
    dispatcher.executeDispatch.assert_called_with(frame, ".uno:AcceptTrackedChange", "", 0, ())

def test_manage_tracked_changes_reject():
    ctx, dispatcher, frame, _ = _create_mock_ctx()
    tool = ManageTrackedChanges()

    redline_mock, span_cursor = _redline_property_set()

    def _create_enum():
        enum = MagicMock()
        enum.hasMoreElements.side_effect = [True, False]
        enum.nextElement.return_value = redline_mock
        return enum

    ctx.doc.getRedlines.return_value.createEnumeration.side_effect = _create_enum

    # Mock redlines.getCount() before and after
    ctx.doc.getRedlines.return_value.getCount.side_effect = [1, 0]

    res = tool.execute(ctx, action="reject", index=0)
    assert res["status"] == "ok"
    ctx.doc.getCurrentController().select.assert_called_with(span_cursor)
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

# --- Agent-self-resolution guard (B3): the agent must never accept/reject its OWN edits --------

from plugin.writer.review_scan import TOKEN_PREFIX

_AGENT_COMMENT = TOKEN_PREFIX + "sess123:0"


def _fake_redline(comment, raise_comment=False):
    rl = MagicMock()
    props = {"RedlineComment": comment, "RedlineIdentifier": f"id:{comment}",
             # Real redlines expose these (and no getAnchor); the accept/reject selection uses them.
             "RedlineStart": MagicMock(), "RedlineEnd": MagicMock()}

    def _get(name):
        if raise_comment and name == "RedlineComment":
            raise RuntimeError("comment read boom")
        return props[name]

    rl.getPropertyValue.side_effect = _get
    return rl


def _install_redlines(ctx, items, count=None):
    """Wire ctx.doc.getRedlines() to enumerate *items* (count override simulates a truncated scan)."""
    rls = ctx.doc.getRedlines.return_value
    rls.getCount.return_value = len(items) if count is None else count

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
    _install_redlines(ctx, [_fake_redline("")])
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
    redline = _fake_redline("")
    # Provide dummy values to pass the overlap check logic
    text_mock = MagicMock()
    text_mock.compareRegionStarts.return_value = 1
    redline.getPropertyValue.return_value.getText.return_value = text_mock
    _install_redlines(ctx, [redline])
    ctx.doc.getRedlines.return_value.getCount.side_effect = [1, 0]
    res = ManageTrackedChanges().execute(ctx, action="accept", index=0)
    assert res["status"] == "ok"
    dispatcher.executeDispatch.assert_called_with(frame, ".uno:AcceptTrackedChange", "", 0, ())


def test_single_accept_allowed_on_touching_ranges():
    ctx, dispatcher, frame, _ = _create_mock_ctx()
    r1 = _fake_redline("")
    r2 = _fake_redline("")

    text_mock = MagicMock()
    # 0 means they are strictly adjacent (touching)
    text_mock.compareRegionStarts.return_value = 0
    text_mock.compareRegionEnds.return_value = 0

    start_mock1 = MagicMock()
    start_mock1.getText.return_value = text_mock
    r1.getPropertyValue.side_effect = lambda prop: start_mock1 if prop == "RedlineStart" else start_mock1

    start_mock2 = MagicMock()
    start_mock2.getText.return_value = text_mock
    r2.getPropertyValue.side_effect = lambda prop: start_mock2 if prop == "RedlineStart" else start_mock2

    _install_redlines(ctx, [r1, r2])
    ctx.doc.getRedlines.return_value.getCount.side_effect = [2, 1]

    res = ManageTrackedChanges().execute(ctx, action="accept", index=0)
    assert res["status"] == "ok"
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
