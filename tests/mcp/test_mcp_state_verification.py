# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""deal / pytest / CrossHair verification for mcp_state.next_state."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from plugin.mcp.mcp_state import (
    EventKind,
    MCPEvent,
    MCPState,
    MCPStateStr,
    SendErrorEffect,
    StreamResponseEffect,
    next_state,
)

_CROSSHAIR_ERROR_RE = re.compile(r": error:")
_CROSSHAIR_TARGET = "plugin.mcp.mcp_state.next_state"


def _find_crosshair() -> str | None:
    crosshair_path = shutil.which("crosshair")
    if crosshair_path:
        return crosshair_path
    venv_bin_ch = Path(".venv/bin/crosshair")
    if venv_bin_ch.exists():
        return str(venv_bin_ch)
    return None


def _idle() -> MCPState:
    return MCPState(status=MCPStateStr.IDLE)


def test_request_missing_tool_name_errors() -> None:
    transition = next_state(_idle(), MCPEvent(kind=EventKind.REQUEST_RECEIVED, data={}))
    assert transition.state.is_error
    assert transition.state.status == MCPStateStr.ERROR
    assert any(isinstance(e, SendErrorEffect) for e in transition.effects)


def test_request_empty_tool_name_errors() -> None:
    transition = next_state(_idle(), MCPEvent(kind=EventKind.REQUEST_RECEIVED, data={"tool_name": ""}))
    assert transition.state.is_error
    assert any(isinstance(e, SendErrorEffect) and e.code == "INVALID_PARAMS" for e in transition.effects)


def test_tool_completed_streams_response() -> None:
    state = MCPState(status=MCPStateStr.EXECUTING_TOOL, tool_name="ping")
    transition = next_state(state, MCPEvent(kind=EventKind.TOOL_COMPLETED, data={"result": {"ok": True}}))
    assert any(isinstance(e, StreamResponseEffect) for e in transition.effects)
    assert transition.state.status == MCPStateStr.STREAMING_RESPONSE


def test_request_error_sets_is_error() -> None:
    transition = next_state(
        _idle(),
        MCPEvent(kind=EventKind.REQUEST_ERROR, data={"message": "fail", "code": "X"}),
    )
    assert transition.state.is_error
    assert transition.state.status == MCPStateStr.ERROR
    assert transition.state.error_code == "X"


@pytest.mark.slow
def test_crosshair_mcp_next_state_fqn_if_available() -> None:
    crosshair_path = _find_crosshair()
    if not crosshair_path:
        pytest.skip("CrossHair concolic execution engine is not installed.")
    result = subprocess.run(
        [crosshair_path, "check", "-v", "--report_all", _CROSSHAIR_TARGET],
        capture_output=True,
        text=True,
        timeout=600,
    )
    combined = f"{result.stdout}\n{result.stderr}".strip()
    print(f"CrossHair output:\n{combined}")
    errors = [line for line in combined.splitlines() if _CROSSHAIR_ERROR_RE.search(line)]
    assert not errors, "CrossHair counterexamples found:\n" + "\n".join(errors)
    if result.returncode == 2:
        pytest.fail(f"CrossHair internal error (exit 2):\n{combined}")
