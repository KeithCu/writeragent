import pytest
from plugin.modules.chatbot.state_machine import (
    SendHandlerState, next_state, StartEvent, StopRequestedEvent,
    StreamChunkEvent, StreamDoneEvent, ErrorEvent, UIEffect, CompleteJobEffect,
    SpawnDirectImageEffect, SpawnAgentWorkerEffect, SpawnWebWorkerEffect,
)

class TestSendHandlerStateMachine:
    def test_start_image(self):
        state = SendHandlerState(handler_type="image", status="ready")
        event = StartEvent(query_text="draw a cat", model=None, doc_type_str="image")

        step = next_state(state, event)
        new_state, effects = step.state, step.effects

        assert new_state.status == "starting"
        assert new_state.query_text == "draw a cat"
        assert len(effects) == 5
        assert isinstance(effects[0], UIEffect) # You
        assert isinstance(effects[1], UIEffect) # Using image
        assert isinstance(effects[2], UIEffect) # AI:
        assert isinstance(effects[3], UIEffect) # SetStatusEffect replacement
        assert effects[3].kind == "status"
        assert isinstance(effects[4], SpawnDirectImageEffect)

    def test_start_web(self):
        state = SendHandlerState(handler_type="web", status="ready")
        event = StartEvent(query_text="search python", model=None, doc_type_str="web")

        step = next_state(state, event)
        new_state, effects = step.state, step.effects

        assert new_state.status == "starting"
        assert new_state.query_text == "search python"
        assert len(effects) == 3
        assert isinstance(effects[0], UIEffect) # You
#        assert isinstance(effects[1], UIEffect) # Using research
        assert isinstance(effects[1], UIEffect) # Starting status
        #assert effects[2].kind == "status"
        assert isinstance(effects[2], SpawnWebWorkerEffect)

    def test_stop_event_agent_terminates(self):
        state = SendHandlerState(handler_type="agent", status="running", round_num=2, max_rounds=10)
        event = StopRequestedEvent()

        step = next_state(state, event)
        new_state, effects = step.state, step.effects

        # Verify termination state
        assert new_state.status == "stopped"

        # Verify proper effects
        assert len(effects) == 3
        assert isinstance(effects[0], UIEffect)
        assert effects[0].kind == "status"
        assert effects[0].text == "Stopped"
        assert isinstance(effects[1], UIEffect)
        assert effects[1].kind == "append"
        assert effects[1].text == "\n[Stopped by user]\n"
        assert isinstance(effects[2], CompleteJobEffect)
        assert effects[2].terminal_status == "Stopped"

    def test_stop_event_other_terminates(self):
        state = SendHandlerState(handler_type="web", status="running", round_num=2, max_rounds=10)
        event = StopRequestedEvent()

        step = next_state(state, event)
        new_state, effects = step.state, step.effects

        # Verify termination state
        assert new_state.status == "stopped"

        # Verify proper effects - should NOT have the UIEffect "append" artifact for web/image
        assert len(effects) == 2
        assert isinstance(effects[0], UIEffect)
        assert effects[0].kind == "status"
        assert effects[0].text == "Stopped"
        assert isinstance(effects[1], CompleteJobEffect)
        assert effects[1].terminal_status == "Stopped"

    def test_stream_chunk(self):
        state = SendHandlerState(handler_type="image", status="running", query_text="cat")
        event = StreamChunkEvent(chunk_text="test data")

        step = next_state(state, event)
        new_state, effects = step.state, step.effects

        assert new_state.status == "running" # Unchanged
        assert new_state.query_text == "cat"
        assert len(effects) == 1
        assert isinstance(effects[0], UIEffect)
        assert effects[0].kind == "append"
        assert effects[0].text == "test data"

    def test_error_event(self):
        state = SendHandlerState(handler_type="web", status="running")
        event = ErrorEvent(error=Exception("Network failure"), context="test", error_time=123.45)

        step = next_state(state, event)
        new_state, effects = step.state, step.effects

        assert new_state.status == "error"
        assert new_state.last_error == "Network failure"
        assert new_state.error_time == 123.45
        assert len(effects) == 3
        assert isinstance(effects[0], UIEffect)
        assert effects[0].kind == "status"
        assert effects[0].text == "Error"
        assert isinstance(effects[1], UIEffect)
        assert effects[1].kind == "append"
        # The exact format might vary depending on format_error_for_display output
        assert "Research Chat error: " in effects[1].text
        assert isinstance(effects[2], CompleteJobEffect)
        assert effects[2].terminal_status == "Error"

    def test_terminal_error_state(self):
        state = SendHandlerState(handler_type="web", status="error", last_error="Network failure")
        event = StreamChunkEvent(chunk_text="test data")

        step = next_state(state, event)
        new_state, effects = step.state, step.effects

        assert new_state.status == "error"
        assert new_state.last_error == "Network failure"
        assert len(effects) == 0

    def test_round_counter_invariant(self):
        # A mock test to verify that the next_state contract holds (e.g. no exceptions thrown)
        state = SendHandlerState(handler_type="agent", status="running", round_num=5, max_rounds=10)
        event = StreamDoneEvent(response={})

        step = next_state(state, event)
        new_state = step.state
        assert new_state.round_num <= 10 # Post condition passes
