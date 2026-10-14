# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for PPT-Master venv IPC helpers."""

from __future__ import annotations

import io
import subprocess
import sys
from typing import Any

import pytest

import plugin.ppt_master.venv.ipc as ppt_ipc
import plugin.scripting.ipc as scripting_ipc
from plugin.scripting.ipc import IpcFrameError, pack_pickle_frame, read_pickle_frame


def test_write_frame_uses_claimed_stream_including_size_error(monkeypatch: pytest.MonkeyPatch) -> None:
    claimed = io.BytesIO()
    monkeypatch.setattr(scripting_ipc, "_child_ipc_stream", claimed)

    recorded_streams: list[Any] = []
    calls = {"n": 0}

    def _mock_write_pickle_frame(stream: Any, message: Any, **kwargs: Any) -> None:
        calls["n"] += 1
        recorded_streams.append(stream)
        if calls["n"] == 1:
            raise IpcFrameError("too big")

    monkeypatch.setattr(ppt_ipc, "write_pickle_frame", _mock_write_pickle_frame)

    ppt_ipc._write_frame({"id": "1", "type": "tool_call", "tool": "t", "args": {}})

    assert calls["n"] == 2
    assert len(recorded_streams) == 2
    assert recorded_streams[0] is claimed
    assert recorded_streams[1] is claimed


def test_read_host_response_uses_claimed_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    valid_payload = pack_pickle_frame({"status": "ok", "result": 1})
    monkeypatch.setattr(ppt_ipc, "get_child_ipc_stdin", lambda: io.BytesIO(valid_payload))

    result = ppt_ipc._read_host_response("tool call")
    assert result == {"status": "ok", "result": 1}

    monkeypatch.setattr(ppt_ipc, "get_child_ipc_stdin", lambda: io.BytesIO(b"x"))
    with pytest.raises(ConnectionError, match="tool call"):
        ppt_ipc._read_host_response("tool call")


def test_emit_worker_event_survives_stdout_claim() -> None:
    code = """
import sys
from plugin.scripting.ipc import claim_ipc_channel
from plugin.ppt_master.venv.ipc import emit_worker_event

claim_ipc_channel()
print("STRAY")
sys.stdout.flush()
emit_worker_event({"kind": "note"})
"""
    proc = subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
    )
    stdout, stderr = proc.communicate(timeout=5.0)
    assert proc.returncode == 0
    assert b"STRAY" in stderr
    assert b"STRAY" not in stdout

    decoded = read_pickle_frame(io.BytesIO(stdout))
    assert decoded == {"type": "worker_event", "event": {"kind": "note"}}
    assert not stderr.startswith(stdout)
