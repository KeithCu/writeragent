# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""deal / CrossHair / Hypothesis verification for pure chat FSMs (send + audio).

CrossHair marked slow (excluded from default ``make test``).
Hypothesis: light budgets under ``make verify``; deep via ``make vhs``.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from plugin.chatbot.audio_recorder_state import (
    AudioRecorderState,
    ErrorOccurredEvent,
    ReportErrorEffect,
    next_state as audio_next_state,
)
from plugin.chatbot.send_state import (
    SendButtonState,
    SendEvent,
    SendEventKind,
    UpdateUIEffect,
    next_state as send_next_state,
)
from plugin.chatbot.state_machine import (
    CompleteJobEffect,
    SendHandlerState,
    SpawnAgentWorkerEffect,
    StopRequestedEvent,
    next_state as send_handler_next_state,
    stop_effects_exclude_spawns,
)
from plugin.chatbot.tool_loop_state import (
    EventKind,
    ExitLoopEffect,
    SpawnToolWorkerEffect,
    ToolLoopEvent,
    ToolLoopState,
    next_state as tool_loop_next_state,
    stopped_effects_exclude_tool_spawns,
)
from tests.chatbot.fsm_hyp_support import (
    audio_recorder_events,
    audio_recorder_states,
    fsm_hypothesis_max_examples,
    send_button_states,
    send_events,
    send_handler_events,
    send_handler_states,
    tool_loop_events,
    tool_loop_states,
)

_CROSSHAIR_ERROR_RE = re.compile(r": error:")


def _find_crosshair() -> str | None:
    crosshair_path = shutil.which("crosshair")
    if crosshair_path:
        return crosshair_path
    venv_bin_ch = Path(".venv/bin/crosshair")
    if venv_bin_ch.exists():
        return str(venv_bin_ch)
    return None


def _run_crosshair(module: str, timeout: int = 600) -> None:
    crosshair_path = _find_crosshair()
    if not crosshair_path:
        pytest.skip("CrossHair concolic execution engine is not installed.")
    result = subprocess.run(
        [crosshair_path, "check", "-v", "--report_all", module],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    combined = f"{result.stdout}\n{result.stderr}".strip()
    print(f"CrossHair output ({module}):\n{combined}")
    errors = [line for line in combined.splitlines() if _CROSSHAIR_ERROR_RE.search(line)]
    assert not errors, f"CrossHair counterexamples in {module}:\n" + "\n".join(errors)
    if result.returncode == 2:
        pytest.fail(f"CrossHair internal error (exit 2) for {module}:\n{combined}")


def test_send_state_mutual_exclusion_oracle() -> None:
    """Runtime check of send/stop UI mutual exclusion (matches deal.ensure)."""
    states = [
        SendButtonState(False, False, True, False, True),
        SendButtonState(True, False, True, False, True),
        SendButtonState(False, True, False, False, True),
        SendButtonState(False, False, False, True, True),
    ]
    events = [
        SendEvent(SendEventKind.TEXT_UPDATED, {"has_text": True}),
        SendEvent(SendEventKind.RECORD_CLICKED),
        SendEvent(SendEventKind.STOP_REC_CLICKED),
        SendEvent(SendEventKind.SEND_CLICKED),
        SendEvent(SendEventKind.STOP_CLICKED),
        SendEvent(SendEventKind.SEND_COMPLETED),
        SendEvent(SendEventKind.ERROR_OCCURRED),
    ]
    for state in states:
        for event in events:
            tr = send_next_state(state, event)
            assert not (tr.state.is_busy and tr.state.is_recording)
            for e in tr.effects:
                if isinstance(e, UpdateUIEffect):
                    assert not (e.send_enabled and e.stop_enabled)
                    if e.send_enabled:
                        assert not tr.state.is_busy
                    if e.stop_enabled:
                        assert tr.state.is_busy


def test_audio_error_always_reports_error_status() -> None:
    for status in ("idle", "initializing", "recording", "stopping", "error"):
        tr = audio_next_state(AudioRecorderState(status=status), ErrorOccurredEvent("boom"))
        assert tr.state.status == "error"
        assert any(isinstance(e, ReportErrorEffect) for e in tr.effects)


def test_tool_loop_stop_emits_exit_loop() -> None:
    state = ToolLoopState(
        round_num=0,
        pending_tools=[],
        max_rounds=5,
        status="Thinking...",
        is_stopped=False,
    )
    tr = tool_loop_next_state(state, ToolLoopEvent(EventKind.STOP_REQUESTED, {}))
    assert any(isinstance(e, ExitLoopEffect) for e in tr.effects)
    assert tr.state.is_stopped
    assert tr.state.round_num <= max(state.round_num + 1, state.max_rounds)
    assert stopped_effects_exclude_tool_spawns(tr.state, tr.effects)


def test_tool_loop_formatting_helpers() -> None:
    from plugin.chatbot.tool_loop_state import (
        format_delegate_running_chat_line,
        format_empty_model_response_debug,
    )

    debug = format_empty_model_response_debug(1, {"finish_reason": "stop", "content": "hi"})
    assert "round=1" in debug
    assert "finish_reason='stop'" in debug

    del_line = format_delegate_running_chat_line({"domain": "writer", "task": "format table"})
    assert del_line.startswith("[Running delegate (writer):")
    assert del_line.endswith("\n")


def test_send_handler_stop_does_not_spawn_workers() -> None:
    state = SendHandlerState(
        handler_type="agent",
        status="running",
        query_text="hi",
        round_num=0,
        pending_tools=[],
        max_rounds=5,
    )
    tr = send_handler_next_state(state, StopRequestedEvent())
    assert tr.state.round_num <= tr.state.max_rounds
    assert not any(isinstance(e, SpawnAgentWorkerEffect) for e in tr.effects)
    assert any(isinstance(e, CompleteJobEffect) for e in tr.effects)


def _hyp_n(key: str) -> int:
    return fsm_hypothesis_max_examples()[key]


@given(state=send_button_states(), event=send_events())
@settings(max_examples=_hyp_n("send"), deadline=None)
def test_hypothesis_send_state_invariants(state: SendButtonState, event: SendEvent) -> None:
    """Deal ensure: never busy+recording; send/stop UI mutually exclusive."""
    tr = send_next_state(state, event)
    assert not (tr.state.is_busy and tr.state.is_recording)
    for e in tr.effects:
        if isinstance(e, UpdateUIEffect):
            assert not (e.send_enabled and e.stop_enabled)
            if e.send_enabled:
                assert not tr.state.is_busy
            if e.stop_enabled:
                assert tr.state.is_busy


@given(state=audio_recorder_states(), event=audio_recorder_events())
@settings(max_examples=_hyp_n("audio"), deadline=None)
def test_hypothesis_audio_recorder_invariants(state: AudioRecorderState, event) -> None:
    tr = audio_next_state(state, event)
    assert tr.state.status in ("idle", "initializing", "recording", "stopping", "error")
    if isinstance(event, ErrorOccurredEvent):
        assert tr.state.status == "error"
        assert any(isinstance(e, ReportErrorEffect) for e in tr.effects)


@given(state=tool_loop_states(), event=tool_loop_events())
@settings(max_examples=_hyp_n("tool_loop"), deadline=None)
def test_hypothesis_tool_loop_invariants(state: ToolLoopState, event: ToolLoopEvent) -> None:
    """Stopped-latched pending tools never spawn; is_stopped is sticky; pending never shrinks while stopped."""
    tr = tool_loop_next_state(state, event)
    assert tr.state.round_num >= 0
    assert tr.state.round_num <= max(state.round_num + 1, state.max_rounds)
    if state.is_stopped:
        assert tr.state.is_stopped
        assert len(tr.state.pending_tools) >= len(state.pending_tools)
    assert stopped_effects_exclude_tool_spawns(tr.state, tr.effects)
    if event.kind == EventKind.STOP_REQUESTED:
        assert any(isinstance(e, ExitLoopEffect) for e in tr.effects)
        assert tr.state.is_stopped
        assert not any(isinstance(e, SpawnToolWorkerEffect) for e in tr.effects)


@given(state=tool_loop_states(), events=st.lists(tool_loop_events(), min_size=1, max_size=5))
@settings(max_examples=_hyp_n("sequences"), deadline=None)
def test_hypothesis_tool_loop_stop_sequences(state: ToolLoopState, events) -> None:
    """Forced STOP, then a short walk: latch sticks and leftover pending never spawn."""
    stop_tr = tool_loop_next_state(state, ToolLoopEvent(EventKind.STOP_REQUESTED, {}))
    assert stop_tr.state.is_stopped
    assert any(isinstance(e, ExitLoopEffect) for e in stop_tr.effects)
    assert stopped_effects_exclude_tool_spawns(stop_tr.state, stop_tr.effects)
    cur = stop_tr.state
    pending_floor = len(cur.pending_tools)
    for event in events:
        tr = tool_loop_next_state(cur, event)
        assert tr.state.is_stopped
        assert len(tr.state.pending_tools) >= pending_floor
        assert stopped_effects_exclude_tool_spawns(tr.state, tr.effects)
        pending_floor = len(tr.state.pending_tools)
        cur = tr.state


@given(state=send_handler_states(), event=send_handler_events())
@settings(max_examples=_hyp_n("send_handler"), deadline=None)
def test_hypothesis_send_handler_stop_excludes_spawns(state: SendHandlerState, event) -> None:
    tr = send_handler_next_state(state, event)
    if isinstance(event, StopRequestedEvent) and state.status != "error":
        assert stop_effects_exclude_spawns(tr.effects)
        assert any(isinstance(e, CompleteJobEffect) for e in tr.effects)
        assert not any(isinstance(e, SpawnAgentWorkerEffect) for e in tr.effects)
    elif isinstance(event, StopRequestedEvent):
        assert stop_effects_exclude_spawns(tr.effects)


@given(state=send_button_states(), events=st.lists(send_events(), min_size=1, max_size=5))
@settings(max_examples=_hyp_n("sequences"), deadline=None)
def test_hypothesis_send_state_sequences(state: SendButtonState, events) -> None:
    """Short random walks never produce busy+recording."""
    cur = state
    for event in events:
        tr = send_next_state(cur, event)
        assert not (tr.state.is_busy and tr.state.is_recording)
        cur = tr.state


@pytest.mark.slow
def test_crosshair_send_state_if_available() -> None:
    _run_crosshair("plugin/chatbot/send_state.py")


@pytest.mark.slow
def test_crosshair_audio_recorder_state_if_available() -> None:
    _run_crosshair("plugin/chatbot/audio_recorder_state.py")


@pytest.mark.slow
def test_crosshair_state_machine_if_available() -> None:
    # next_state is # crosshair: off; module check covers pure helpers with @deal.
    _run_crosshair("plugin/chatbot/state_machine.py", timeout=300)


@pytest.mark.slow
def test_crosshair_tool_loop_state_if_available() -> None:
    # next_state is # crosshair: off; module check covers pure helpers with @deal.
    _run_crosshair("plugin/chatbot/tool_loop_state.py", timeout=180)
