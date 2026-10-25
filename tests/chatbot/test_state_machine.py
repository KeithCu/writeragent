from typing import Any, cast

from plugin.chatbot.state_machine import (
    EffectInterpreter,
    SendHandlerState,
    next_state,
    StartEvent,
    StopRequestedEvent,
    StreamChunkEvent,
    StreamDoneEvent,
    ErrorEvent,
    SendHandlerUIEffect,
    CompleteJobEffect,
    SpawnAgentWorkerEffect,
    SpawnDirectImageEffect,
    SpawnWebWorkerEffect,
    spawn_effects_for_start,
    ui_lines_for_handler_error,
    stop_effects_exclude_spawns,
)
import pytest

class TestSendHandlerStateMachine:
    def test_start_image(self):
        state = SendHandlerState(handler_type="image", status="ready")
        event = StartEvent(query_text="draw a cat", model=None, doc_type_str="image")

        step = next_state(state, event)
        new_state, effects = step.state, step.effects

        assert new_state.status == "starting"
        assert new_state.query_text == "draw a cat"
        assert any(isinstance(e, SpawnDirectImageEffect) for e in effects)
        assert any(isinstance(e, SendHandlerUIEffect) and e.kind == "status" for e in effects)
        assert any(isinstance(e, SendHandlerUIEffect) and e.kind == "append" for e in effects)

    def test_start_web(self):
        state = SendHandlerState(handler_type="web", status="ready")
        event = StartEvent(query_text="search python", model=None, doc_type_str="web")

        step = next_state(state, event)
        new_state, effects = step.state, step.effects

        assert new_state.status == "starting"
        assert new_state.query_text == "search python"
        assert any(isinstance(e, SpawnWebWorkerEffect) for e in effects)
        assert any(isinstance(e, SendHandlerUIEffect) and e.kind == "status" for e in effects)
        assert any(isinstance(e, SendHandlerUIEffect) and e.kind == "append" for e in effects)

    def test_start_unknown_handler_type_raises(self):
        state = SendHandlerState(handler_type="other", status="ready")
        with pytest.raises(AssertionError):
            next_state(state, StartEvent(query_text="foo", model=None, doc_type_str="other"))
        with pytest.raises(AssertionError):
            spawn_effects_for_start("other", "foo", None, "other")

    def test_stop_event_agent_terminates(self):
        state = SendHandlerState(handler_type="agent", status="running")
        event = StopRequestedEvent()

        step = next_state(state, event)
        new_state, effects = step.state, step.effects

        # Verify termination state
        assert new_state.status == "stopped"

        # The stop line is a session message, not a paint effect.
        assert len(effects) == 2
        assert isinstance(effects[0], SendHandlerUIEffect)
        assert effects[0].kind == "status"
        assert effects[0].text == "Stopped"
        assert isinstance(effects[1], CompleteJobEffect)
        assert effects[1].terminal_status == "Stopped"

    def test_stop_event_other_terminates(self):
        state = SendHandlerState(handler_type="web", status="running")
        event = StopRequestedEvent()

        step = next_state(state, event)
        new_state, effects = step.state, step.effects

        # Verify termination state
        assert new_state.status == "stopped"

        # Verify proper effects - should NOT have the SendHandlerUIEffect "append" artifact for web/image
        assert len(effects) == 2
        assert isinstance(effects[0], SendHandlerUIEffect)
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
        assert isinstance(effects[0], SendHandlerUIEffect)
        assert effects[0].kind == "append"
        assert effects[0].text == "test data"

    def test_error_event(self):
        state = SendHandlerState(handler_type="web", status="running")
        event = ErrorEvent(error=Exception("Network failure"), error_time=123.45)

        step = next_state(state, event)
        new_state, effects = step.state, step.effects

        assert new_state.status == "error"
        assert new_state.last_error == "Network failure"
        assert new_state.error_time == 123.45
        assert len(effects) == 3
        assert isinstance(effects[0], SendHandlerUIEffect)
        assert effects[0].kind == "status"
        assert effects[0].text == "Error"
        assert isinstance(effects[1], SendHandlerUIEffect)
        assert effects[1].kind == "append"
        # The exact format might vary depending on format_error_for_display output
        assert "Research Chat error: " in effects[1].text
        assert isinstance(effects[2], CompleteJobEffect)
        assert effects[2].terminal_status == "Error"

    def test_error_event_without_error_time_stays_none(self):
        state = SendHandlerState(handler_type="web", status="running")
        event = ErrorEvent(error=Exception("Network failure"))
        step = next_state(state, event)
        assert step.state.error_time is None

    def test_terminal_error_state(self):
        state = SendHandlerState(handler_type="web", status="error", last_error="Network failure")
        event = StreamChunkEvent(chunk_text="test data")

        step = next_state(state, event)
        new_state, effects = step.state, step.effects

        assert new_state.status == "error"
        assert new_state.last_error == "Network failure"
        assert len(effects) == 0

    def test_stream_done_marks_ready(self):
        state = SendHandlerState(handler_type="agent", status="running")
        step = next_state(state, StreamDoneEvent(response={}))
        assert step.state.status == "done"
        assert any(isinstance(e, CompleteJobEffect) and e.terminal_status == "Ready" for e in step.effects)
        assert any(isinstance(e, SendHandlerUIEffect) and e.kind == "status" and e.text == "Ready" for e in step.effects)

    def test_chunk_after_stop_ignored(self):
        state = SendHandlerState(handler_type="agent", status="stopped")
        event = StreamChunkEvent(chunk_text="late chunk")
        step = next_state(state, event)
        assert step.state.status == "stopped"
        assert len(step.effects) == 0

    def test_error_after_stop_ignored(self):
        state = SendHandlerState(handler_type="web", status="stopped")
        event = ErrorEvent(error=Exception("late failure"))
        step = next_state(state, event)
        assert step.state.status == "stopped"
        assert len(step.effects) == 0

    def test_stop_after_done_is_noop(self):
        state = SendHandlerState(handler_type="web", status="done")
        step = next_state(state, StopRequestedEvent())
        assert step.state.status == "done"
        assert step.state.last_error is None
        assert step.effects == []

    def test_stop_after_stopped_is_noop(self):
        state = SendHandlerState(handler_type="web", status="stopped")
        step = next_state(state, StopRequestedEvent())
        assert step.state.status == "stopped"
        assert step.state.last_error is None
        assert step.effects == []

    def test_error_after_done_is_noop(self):
        state = SendHandlerState(handler_type="web", status="done")
        step = next_state(state, ErrorEvent(error=Exception("late")))
        assert step.state.status == "done"
        assert step.state.last_error is None
        assert step.effects == []

    def test_stream_done_unknown_handler_type_emits_ready(self):
        state = SendHandlerState(handler_type="other", status="running")
        step = next_state(state, StreamDoneEvent(response={}))
        assert step.state.status == "done"
        assert any(isinstance(e, CompleteJobEffect) and e.terminal_status == "Ready" for e in step.effects)
        assert any(isinstance(e, SendHandlerUIEffect) and e.kind == "status" and e.text == "Ready" for e in step.effects)


class TestSendHandlerHelpers:
    def test_spawn_effects_image(self):
        effects = spawn_effects_for_start("image", "draw a cat", None, "image")
        assert any(isinstance(e, SpawnDirectImageEffect) for e in effects)

    def test_spawn_effects_web(self):
        effects = spawn_effects_for_start("web", "search python", None, "web")
        assert any(isinstance(e, SpawnWebWorkerEffect) for e in effects)

    def test_spawn_effects_agent(self):
        effects = spawn_effects_for_start("agent", "do work", "m", "writer")
        assert any(isinstance(e, SpawnAgentWorkerEffect) for e in effects)

    @pytest.mark.parametrize(
        "value, value_2",
        [
            pytest.param("web", "Research Chat error: boom", id="test_ui_lines_web"),
            pytest.param("image", "Operation failed: boom", id="test_ui_lines_other"),
        ],
    )
    def test_ui_lines_web(self, value, value_2):
        status, append = ui_lines_for_handler_error(value, "boom")
        assert status == "Error"
        assert value_2 in append


    def test_stop_effects_exclude_spawns(self):
        assert stop_effects_exclude_spawns([SendHandlerUIEffect("status", "Stopped"), CompleteJobEffect("Stopped")])
        assert not stop_effects_exclude_spawns([SpawnWebWorkerEffect("q", None)])
        assert stop_effects_exclude_spawns("not-a-list")


class TestEffectInterpreterThinking:
    def test_append_keeps_thinking_flag(self):
        appended = []

        class Host:
            def _append_response(self, text, is_thinking=False, role="assistant"):
                appended.append((text, is_thinking, role))

            def _set_status(self, text):
                return None

        state = SendHandlerState(handler_type="web", status="running")
        interpreter = EffectInterpreter(Host())
        interpreter.interpret(SendHandlerUIEffect("append", "search step", is_thinking=True), state)
        interpreter.interpret(SendHandlerUIEffect("append", "answer", is_thinking=False, role="assistant"), state)
        assert appended == [("search step", True, "assistant"), ("answer", False, "assistant")]

    def test_interpret_unknown_effect_raises(self):
        state = SendHandlerState(handler_type="web", status="running")
        interpreter = EffectInterpreter(None)
        with pytest.raises(AssertionError):
            interpreter.interpret(cast(Any, object()), state)
