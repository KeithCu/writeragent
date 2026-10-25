
from plugin.chatbot.send_state import SendButtonState, SendEvent, SendEventKind, next_state as send_next_state
from plugin.chatbot.sidebar_state import SidebarCompositeState
from plugin.chatbot.tool_loop_state import ToolLoopState


def test_send_transition_replaces_send_and_leaves_tool_loop_same():
    send_initial = SendButtonState(False, False, False, False, True)
    tool_loop_initial = ToolLoopState(
        round_num=1,
        pending_tools=[],
        max_rounds=5,
        status="Thinking...",
    )
    composite = SidebarCompositeState(send=send_initial, tool_loop=tool_loop_initial)

    tr = send_next_state(composite.send, SendEvent(SendEventKind.TEXT_UPDATED, has_text=True))
    new_composite = SidebarCompositeState(send=tr.state, tool_loop=composite.tool_loop)

    assert new_composite.send.has_text is True
    assert new_composite.tool_loop is tool_loop_initial
