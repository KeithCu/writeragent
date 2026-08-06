# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""deal / CrossHair verification for pure chat FSMs (send + audio).

CrossHair marked slow (excluded from default ``make test``).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

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
)
from plugin.chatbot.tool_loop_state import (
    EventKind,
    ExitLoopEffect,
    ToolLoopEvent,
    ToolLoopState,
    next_state as tool_loop_next_state,
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
    assert tr.state.round_num <= max(state.round_num + 1, state.max_rounds)


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
