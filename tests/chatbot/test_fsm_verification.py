# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""deal / CrossHair / Hypothesis verification for pure chat FSMs (send + audio).

CrossHair marked slow (excluded from default ``make test``).
Hypothesis: light budgets under ``make verify``; deep via ``make vhs``.
"""

from __future__ import annotations


import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from plugin.chatbot.audio_recorder_state import (
    AudioRecorderState,
    AudioStatus,
    ErrorOccurredEvent,
    ReportErrorEffect,
    next_state as audio_next_state,
)
from plugin.chatbot.send_state import (
    SendButtonState,
    SendEvent,
    _send_ui_invariants,
    next_state as send_next_state,
)
from plugin.chatbot.tool_loop_state import (
    EventKind,
    ExitLoopEffect,
    ToolLoopEvent,
    ToolLoopState,
    _transition_invariants,
    next_state as tool_loop_next_state,
    stopped_effects_exclude_tool_spawns,
)
from tests.harness.crosshair import assert_crosshair_clean
from tests.chatbot.fsm_hyp_support import (
    audio_recorder_events,
    audio_recorder_states,
    fsm_hypothesis_max_examples,
    send_button_states,
    send_events,
    tool_loop_events,
    tool_loop_states,
)

def test_audio_error_always_reports_error_status() -> None:
    for status in ("idle", "initializing", "recording", "error"):
        tr = audio_next_state(AudioRecorderState(status=AudioStatus(status)), ErrorOccurredEvent("boom"))
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


def _hyp_n(key: str) -> int:
    return fsm_hypothesis_max_examples()[key]


@given(state=send_button_states(), event=send_events())
@settings(max_examples=_hyp_n("send"), deadline=None)
def test_hypothesis_send_state_invariants(state: SendButtonState, event: SendEvent) -> None:
    """Deal ensure: never busy+recording; send/stop UI exclusive outside a take."""
    tr = send_next_state(state, event)
    assert _send_ui_invariants(state, event, tr)


@given(state=audio_recorder_states(), event=audio_recorder_events())
@settings(max_examples=_hyp_n("audio"), deadline=None)
def test_hypothesis_audio_recorder_invariants(state: AudioRecorderState, event) -> None:
    from plugin.chatbot.audio_recorder_state import _audio_transition_invariants

    tr = audio_next_state(state, event)
    assert _audio_transition_invariants(state, event, tr)


@given(state=tool_loop_states(), event=tool_loop_events())
@settings(max_examples=_hyp_n("tool_loop"), deadline=None)
def test_hypothesis_tool_loop_invariants(state: ToolLoopState, event: ToolLoopEvent) -> None:
    """Stop latch, pending monotonicity, spawn exclusion, and the round bound."""
    tr = tool_loop_next_state(state, event)
    assert _transition_invariants(state, event, tr)


@given(state=tool_loop_states(), events=st.lists(tool_loop_events(), min_size=1, max_size=5))
@settings(max_examples=_hyp_n("sequences"), deadline=None)
def test_hypothesis_tool_loop_stop_sequences(state: ToolLoopState, events) -> None:
    """Forced STOP, then a short walk: latch sticks and leftover pending never spawn."""
    stop = ToolLoopEvent(EventKind.STOP_REQUESTED, {})
    stop_tr = tool_loop_next_state(state, stop)
    assert _transition_invariants(state, stop, stop_tr)
    cur = stop_tr.state
    for event in events:
        tr = tool_loop_next_state(cur, event)
        assert _transition_invariants(cur, event, tr)
        cur = tr.state


@given(state=send_button_states(), events=st.lists(send_events(), min_size=1, max_size=5))
@settings(max_examples=_hyp_n("sequences"), deadline=None)
def test_hypothesis_send_state_sequences(state: SendButtonState, events) -> None:
    """Short random walks never produce busy+recording."""
    cur = state
    for event in events:
        tr = send_next_state(cur, event)
        assert _send_ui_invariants(cur, event, tr)
        cur = tr.state


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("plugin/chatbot/send_state.py", id="test_crosshair_send_state_if_available"),
        pytest.param("plugin/chatbot/audio_recorder_state.py", id="test_crosshair_audio_recorder_state_if_available"),
    ],
)
@pytest.mark.slow
def test_crosshair_send_state_if_available(value) -> None:
    assert_crosshair_clean(value, timeout=600)

@pytest.mark.parametrize(
    "value, timeout",
    [
        pytest.param("plugin/chatbot/tool_loop_state.py", 180, id="test_crosshair_tool_loop_state_if_available"),
    ],
)
@pytest.mark.slow
def test_crosshair_tool_loop_state_if_available(value, timeout) -> None:
    # next_state is # crosshair: off; module check covers pure helpers with @deal.
    assert_crosshair_clean(value, timeout=timeout)
