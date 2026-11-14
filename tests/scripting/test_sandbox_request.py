# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for SandboxRequest wire round-trip and host-only field isolation."""

from __future__ import annotations

import io

from plugin.framework.ipc import (
    DEFAULT_MAX_PAYLOAD_BYTES,
    pack_pickle_frame,
    read_pickle_frame,
)
from plugin.framework.send_cancellation import SendCancellation
from plugin.scripting.venv.sandbox_request import SandboxRequest
from plugin.scripting.venv_worker import ExecRequest


def test_sandbox_request_pickle_roundtrip() -> None:
    req = SandboxRequest(
        timeout_sec=42,
        session_id="calc:doc1",
        data=[[1, 2], [3, 4]],
        code="result = sum(sum(r) for r in data)",
        bindings={"extra": "val"},
        init_script="x = 1",
        init_session_id="calc:doc1:init",
        init_script_hash="abc123",
        sandbox_policy={"network": False, "files": True, "os": False, "extra_modules": ""},
    )
    frame_bytes = pack_pickle_frame(req.wire(), max_payload_bytes=DEFAULT_MAX_PAYLOAD_BYTES)
    decoded = read_pickle_frame(
        io.BytesIO(frame_bytes),
        require_dict=True,
        max_payload_bytes=DEFAULT_MAX_PAYLOAD_BYTES,
    )
    assert SandboxRequest.from_wire(decoded) == req


def test_exec_request_omits_host_only_fields_on_wire() -> None:
    fields = {
        "timeout_sec": 30,
        "session_id": "calc:doc1",
        "data": [1, 2],
        "code": "result = 1",
        "bindings": {"a": 1},
        "init_script": "z = 0",
        "init_session_id": "calc:doc1:init",
        "init_script_hash": "h1",
        "sandbox_policy": {"network": False, "files": False, "os": False, "extra_modules": ""},
    }
    exec_req = ExecRequest(
        **fields,
        cancel=SendCancellation(),
        on_heartbeat=lambda _p: None,
        on_worker_event=lambda _e: None,
        pin="doc:pinned",
        python_tool_domain="calc",
    )
    wire = exec_req.wire()
    for host_key in ("cancel", "on_heartbeat", "on_worker_event", "pin", "python_tool_domain", "caller"):
        assert host_key not in wire
    assert SandboxRequest.from_wire(wire) == SandboxRequest(**fields)


def test_sandbox_request_wire_omits_none_fields() -> None:
    wire = SandboxRequest(timeout_sec=10, code="result = 1").wire()
    assert wire == {"timeout_sec": 10, "code": "result = 1"}
