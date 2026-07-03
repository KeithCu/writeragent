# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import io
from unittest.mock import MagicMock, patch

from plugin.scripting.ipc import read_pickle_frame


def test_dispatch_worker_event_only():
    from plugin.ppt_master.venv.host_rpc import dispatch_worker_response

    events: list[dict] = []

    handled = dispatch_worker_response(
        {"type": "worker_event", "event": {"kind": "thinking", "text": "step"}},
        stdin_write=MagicMock(),
        on_worker_event=events.append,
    )
    assert handled is True
    assert events == [{"kind": "thinking", "text": "step"}]


def test_dispatch_unknown_frame_returns_false():
    from plugin.ppt_master.venv.host_rpc import dispatch_worker_response

    assert dispatch_worker_response({"status": "ok", "result": {}}, stdin_write=MagicMock()) is False


def test_dispatch_tool_call_writes_response():
    from plugin.ppt_master.venv.host_rpc import dispatch_worker_response

    written: list[bytes] = []

    with patch("plugin.ppt_master.venv.host_rpc.execute_tool_on_main_thread", return_value={"status": "ok"}) as mock_tool:
        handled = dispatch_worker_response(
            {"type": "tool_call", "id": "abc", "tool": "validate_ppt_master_project", "args": {"project_path": "/tmp/p"}},
            stdin_write=written.append,
        )

    assert handled is True
    mock_tool.assert_called_once_with("validate_ppt_master_project", {"project_path": "/tmp/p"})
    assert len(written) == 1
    resp = read_pickle_frame(io.BytesIO(written[0]), require_dict=True)
    assert resp is not None
    assert resp["status"] == "ok"
    assert resp["id"] == "abc"


def test_dispatch_llm_request_forwards_to_handler():
    from plugin.ppt_master.venv.host_rpc import dispatch_worker_response

    written: list[bytes] = []
    llm_result = {
        "status": "ok",
        "result": {"role": "assistant", "content": "hi", "tool_calls": None},
    }

    with patch("plugin.ppt_master.venv.host_rpc.handle_llm_request", return_value=llm_result) as mock_llm:
        handled = dispatch_worker_response(
            {"type": "llm_request", "id": "1", "messages": [{"role": "user", "content": "x"}]},
            stdin_write=written.append,
            stop_checker=lambda: False,
        )

    assert handled is True
    mock_llm.assert_called_once()
    call_payload = mock_llm.call_args[0][0]
    assert call_payload["messages"][0]["content"] == "x"
    assert "_stop_checker" in call_payload

    resp = read_pickle_frame(io.BytesIO(written[0]), require_dict=True)
    assert resp is not None
    assert resp["status"] == "ok"
    assert resp["result"]["content"] == "hi"


def test_handle_llm_request_requires_messages():
    from plugin.ppt_master.venv.host_rpc import handle_llm_request

    out = handle_llm_request({})
    assert out["status"] == "error"
