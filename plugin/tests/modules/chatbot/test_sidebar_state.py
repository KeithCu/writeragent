import dataclasses

from plugin.modules.chatbot.audio_recorder_state import AudioRecorderState
from plugin.modules.chatbot.send_state import SendButtonState, SendEvent, SendEventKind
from plugin.modules.chatbot.sidebar_state import (
    LogSidebarEffect,
    SidebarCompositeState,
    SidebarEvent,
    SidebarEventKind,
    sidebar_next_state,
)
from plugin.modules.chatbot.tool_loop_state import (
    EventKind,
    ToolLoopEvent,
    ToolLoopState,
    next_state as tool_loop_next_state,
)


def _composite(send=None, tool_loop=None, audio=None):
    return SidebarCompositeState(
        send=send
        or SendButtonState(False, False, False, False, True),
        tool_loop=tool_loop,
        audio=audio or AudioRecorderState(status="idle"),
    )


def test_router_send_updates_send_slice_only():
    c = _composite()
    tr = sidebar_next_state(
        c,
        SidebarEvent(
            kind=SidebarEventKind.SEND,
            payload=SendEvent(SendEventKind.TEXT_UPDATED, {"has_text": True}),
        ),
    )
    assert tr.state.send.has_text is True
    assert tr.state.tool_loop is None
    assert tr.state.audio == c.audio
    assert len(tr.effects) == 1


def test_router_tool_loop_without_session_logs():
    c = _composite()
    ev = ToolLoopEvent(kind=EventKind.STOP_REQUESTED)
    tr = sidebar_next_state(
        c, SidebarEvent(kind=SidebarEventKind.TOOL_LOOP, payload=ev)
    )
    assert tr.state == c
    assert len(tr.effects) == 1
    assert isinstance(tr.effects[0], LogSidebarEffect)
    assert "no active session" in tr.effects[0].message


def test_router_tool_loop_with_session_forwards_effects():
    tl = ToolLoopState(
        round_num=0,
        pending_tools=[],
        max_rounds=5,
        status="Thinking...",
    )
    c = _composite(tool_loop=tl)
    ev = ToolLoopEvent(kind=EventKind.STOP_REQUESTED)
    tr = sidebar_next_state(
        c, SidebarEvent(kind=SidebarEventKind.TOOL_LOOP, payload=ev)
    )
    direct = tool_loop_next_state(tl, ev)
    assert tr.state.tool_loop == direct.state
    assert tr.state.send == c.send
    assert tr.effects == direct.effects


def test_router_audio_kind_noop():
    c = _composite()
    tr = sidebar_next_state(c, SidebarEvent(kind=SidebarEventKind.AUDIO, payload=None))
    assert tr.state == c
    assert tr.effects == []


def test_integration_send_record_tool_loop_lifecycle_slices():
    """Send → record → tool session → stop tool event → clear session (mirrors wiring)."""
    comp = _composite()
    tr1 = sidebar_next_state(
        comp,
        SidebarEvent(
            kind=SidebarEventKind.SEND,
            payload=SendEvent(SendEventKind.RECORD_CLICKED),
        ),
    )
    assert tr1.state.send.is_recording is True
    assert tr1.state.tool_loop is None

    tr2 = sidebar_next_state(
        tr1.state,
        SidebarEvent(
            kind=SidebarEventKind.SEND,
            payload=SendEvent(SendEventKind.STOP_REC_CLICKED),
        ),
    )
    assert tr2.state.send.is_recording is False
    assert tr2.state.send.has_audio is True
    assert tr2.state.send.is_busy is True

    tl = ToolLoopState(
        round_num=0,
        pending_tools=[],
        max_rounds=5,
        status="Thinking...",
    )
    comp3 = dataclasses.replace(tr2.state, tool_loop=tl)
    assert comp3.tool_loop is not None
    assert comp3.tool_loop.round_num == 0

    tr4 = sidebar_next_state(
        comp3,
        SidebarEvent(
            kind=SidebarEventKind.TOOL_LOOP,
            payload=ToolLoopEvent(kind=EventKind.STOP_REQUESTED),
        ),
    )
    assert tr4.state.tool_loop is not None
    assert tr4.state.tool_loop.is_stopped is True

    cleared = dataclasses.replace(tr4.state, tool_loop=None)
    assert cleared.tool_loop is None
    assert cleared.send.is_busy is True
