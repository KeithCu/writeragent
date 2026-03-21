import pytest
from plugin.modules.chatbot.tool_loop_state import (
    ToolLoopState,
    ToolLoopEvent,
    EventKind,
    SpawnLLMWorkerEffect,
    SpawnToolWorkerEffect,
    UIEffect,
    LogAgentEffect,
    AddMessageEffect,
    UpdateActivityStateEffect,
    next_state,
)

# --- Helpers ---
def create_base_state(round_num=0, pending_tools=None, max_rounds=5, is_stopped=False):
    return ToolLoopState(
        round_num=round_num,
        pending_tools=pending_tools or [],
        max_rounds=max_rounds,
        status="Ready",
        is_stopped=is_stopped,
        async_tools=frozenset(["web_research", "generate_image"])
    )

def create_event(kind: EventKind, **kwargs):
    return ToolLoopEvent(kind=kind, data=kwargs)

# --- Tests ---

def test_stop_requested():
    state = create_base_state()
    event = create_event(EventKind.STOP_REQUESTED)
    new_state, effects = next_state(state, event)

    assert new_state.is_stopped is True
    assert new_state.status == "Stopped"

    assert "exit_loop" in effects
    assert any(isinstance(e, AddMessageEffect) for e in effects)
    assert any(isinstance(e, UIEffect) and e.kind == "status" and e.text == "Stopped" for e in effects)

def test_final_done():
    state = create_base_state()
    event = create_event(EventKind.FINAL_DONE, content="Final words")
    new_state, effects = next_state(state, event)

    assert new_state.status == "Ready"
    assert "exit_loop" in effects
    
    msg_effect = next(e for e in effects if isinstance(e, AddMessageEffect))
    assert msg_effect.content == "Final words"
    assert msg_effect.role == "assistant"

def test_error_event():
    state = create_base_state()
    event = create_event(EventKind.ERROR, error=Exception("Something broke"))
    new_state, effects = next_state(state, event)

    assert new_state.status == "Error"
    assert "exit_loop" in effects

def test_stream_done_finish_reasons():
    state = create_base_state()
    
    # finish_reason="length"
    event_len = create_event(EventKind.STREAM_DONE, response={"finish_reason": "length", "content": None})
    new_state_len, effects_len = next_state(state, event_len)
    assert new_state_len.status == "Ready"
    assert any(isinstance(e, UIEffect) and "out of tokens" in e.text for e in effects_len)
    
    # finish_reason="content_filter"
    event_filt = create_event(EventKind.STREAM_DONE, response={"finish_reason": "content_filter", "content": None})
    new_state_filt, effects_filt = next_state(state, event_filt)
    assert any(isinstance(e, UIEffect) and "Content filter" in e.text for e in effects_filt)

def test_stream_done_empty_tool_calls():
    state = create_base_state()
    # explicitly testing empty list
    event = create_event(EventKind.STREAM_DONE, response={"tool_calls": [], "content": "I couldn't figure it out."})
    new_state, effects = next_state(state, event)

    assert new_state.status == "Ready"
    assert "exit_loop" in effects
    msg_eff = next((e for e in effects if isinstance(e, AddMessageEffect)), None)
    assert msg_eff is not None
    assert msg_eff.content == "I couldn't figure it out."
    assert msg_eff.tool_calls is None

def test_stream_done_with_tool_calls():
    state = create_base_state()
    tool_calls = [{"id": "1", "function": {"name": "test"}}]
    event = create_event(EventKind.STREAM_DONE, response={"tool_calls": tool_calls, "content": "Let me test."})
    new_state, effects = next_state(state, event)

    assert len(new_state.pending_tools) == 1
    assert "trigger_next_tool" in effects
    msg_eff = next((e for e in effects if isinstance(e, AddMessageEffect)), None)
    assert msg_eff.content == "Let me test."
    assert msg_eff.tool_calls == tool_calls

def test_next_tool_advances_round_and_handles_max_rounds():
    # Regular advance
    state = create_base_state(round_num=3, max_rounds=5)
    event = create_event(EventKind.NEXT_TOOL)
    new_state, effects = next_state(state, event)
    
    assert new_state.round_num == 4
    spawn_eff = next((e for e in effects if isinstance(e, SpawnLLMWorkerEffect)), None)
    assert spawn_eff is not None
    assert spawn_eff.round_num == 4
    assert "spawn_final_stream" not in effects

    # Exhausted advance
    state_exhausted = create_base_state(round_num=4, max_rounds=5)
    new_state_ex, effects_ex = next_state(state_exhausted, event)
    assert new_state_ex.round_num == 5
    assert not any(isinstance(e, SpawnLLMWorkerEffect) for e in effects_ex)
    assert "spawn_final_stream" in effects_ex

def test_next_tool_invalid_max_rounds():
    # If round_num somehow exceeds max_rounds, it caps to current round_num to prevent going back
    state = create_base_state(round_num=5, max_rounds=2)
    event = create_event(EventKind.NEXT_TOOL)
    new_state, effects = next_state(state, event)
    assert new_state.round_num == 5
    assert "spawn_final_stream" in effects

def test_next_tool_with_pending_tools_and_action_state():
    tool_calls = [{"id": "call_1", "function": {"name": "test_tool", "arguments": "{}"}}]
    state = create_base_state(pending_tools=tool_calls)
    event = create_event(EventKind.NEXT_TOOL)
    new_state, effects = next_state(state, event)
    
    # Consumed 1 tool
    assert len(new_state.pending_tools) == 0
    
    spawn_eff = next(e for e in effects if isinstance(e, SpawnToolWorkerEffect))
    assert spawn_eff.func_name == "test_tool"
    
    # Check activity state effect
    activity_eff = next(e for e in effects if isinstance(e, UpdateActivityStateEffect))
    assert activity_eff.action == "tool_execute"
    assert activity_eff.tool_name == "test_tool"

def test_next_tool_malformed_arguments_and_missing_func():
    # If we have a pending tool with malformed arguments, it should parse as empty dict
    tool_calls = [{"id": "call_1", "type": "function", "function": {"arguments": "invalid-json"}}]
    state = create_base_state(pending_tools=tool_calls)
    event = create_event(EventKind.NEXT_TOOL)
    new_state, effects = next_state(state, event)
    
    assert len(new_state.pending_tools) == 0
    
    spawn_eff = next(e for e in effects if isinstance(e, SpawnToolWorkerEffect))
    assert spawn_eff.func_name == "unknown" # Missing name defaults to unknown
    assert spawn_eff.func_args == {}  # Handled parsing failure
    assert spawn_eff.func_args_str == "invalid-json"

def test_next_tool_when_stopped():
    # If is_stopped=True but empty pending_tools, it shouldn't update status
    state = create_base_state(is_stopped=True)
    event = create_event(EventKind.NEXT_TOOL)
    new_state, effects = next_state(state, event)
    
    assert not any(isinstance(e, UIEffect) and e.kind == "status" for e in effects)
    assert any(isinstance(e, SpawnLLMWorkerEffect) for e in effects)

def test_tool_result_parsing():
    state = create_base_state()
    
    # Valid JSON tool result
    event_valid = create_event(
        EventKind.TOOL_RESULT,
        call_id="call_x",
        func_name="test_tool",
        func_args_str="{}",
        result='{"success": true, "message": "done"}',
        mutates_document=True
    )
    new_state, effects = next_state(state, event_valid)
    assert "trigger_next_tool" in effects
    assert "update_document_context" in effects  # Because is_success=True and mutates=True
    
    msg_eff = next(e for e in effects if isinstance(e, AddMessageEffect))
    assert msg_eff.role == "tool"
    assert msg_eff.call_id == "call_x"
    assert msg_eff.content == '{"success": true, "message": "done"}'

    # apply_document_content edge case output
    event_adc = create_event(
        EventKind.TOOL_RESULT,
        call_id="call_y",
        func_name="apply_document_content",
        func_args_str='{"content": "' + ("A" * 1000) + '"}',
        result='{"message": "Replaced 0 occurrences"}',
        mutates_document=False
    )
    new_state_adc, effects_adc = next_state(state, event_adc)
    
    ui_effs = [e for e in effects_adc if isinstance(e, UIEffect)]
    assert any("[Debug: params" in e.text for e in ui_effs)
    # the 1000 'A's should be truncated to 800 + "..."
    assert any("..." in e.text for e in ui_effs)
    assert "update_document_context" not in effects_adc # Because mutates_document=False
