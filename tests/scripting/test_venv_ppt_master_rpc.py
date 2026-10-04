# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import io
from unittest.mock import MagicMock, patch

import pytest

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

    with patch("plugin.scripting.host_rpc.execute_tool", return_value={"status": "ok"}) as mock_tool:
        handled = dispatch_worker_response(
            {"type": "tool_call", "id": "abc", "tool": "validate_ppt_master_project", "args": {"project_path": "/tmp/p"}},
            stdin_write=written.append,
        )

    assert handled is True
    mock_tool.assert_called_once_with(
        "validate_ppt_master_project",
        {"project_path": "/tmp/p"},
        caller="ppt_master_venv",
        allowed_tools=None,
        script_session_id=None,
    )
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


def test_dispatch_llm_request_threads_cancellation_scope_on_the_host():
    from plugin.ppt_master.venv.host_rpc import dispatch_worker_response

    scope = object()
    written: list[bytes] = []
    with patch("plugin.ppt_master.venv.host_rpc.handle_llm_request", return_value={"status": "ok", "result": {"content": "hi"}}) as mock_llm:
        handled = dispatch_worker_response(
            {"type": "llm_request", "id": "1", "messages": [{"role": "user", "content": "x"}]},
            stdin_write=written.append,
            stop_checker=lambda: False,
            cancellation_scope=scope,
        )

    assert handled is True
    payload = mock_llm.call_args[0][0]
    assert payload["_cancellation_scope"] is scope
    resp = read_pickle_frame(io.BytesIO(written[0]), require_dict=True)
    assert resp is not None
    assert "_cancellation_scope" not in resp


def test_handle_llm_request_uses_host_cancellation_scope():
    from plugin.ppt_master.venv.host_rpc import handle_llm_request

    scope = object()
    client = MagicMock()
    client._stopped = False
    client.request_with_tools.return_value = {"role": "assistant", "content": "ok", "tool_calls": None}
    with (
        patch("plugin.framework.queue_executor.execute_on_main_thread", side_effect=lambda fn: fn()),
        patch("plugin.framework.uno_context.get_ctx", return_value=MagicMock()),
        patch("plugin.framework.config.get_api_config", return_value={"model": "m"}),
        patch("plugin.framework.client.llm_client.LlmClient", return_value=client) as mock_llm,
    ):
        out = handle_llm_request(
            {
                "messages": [{"role": "user", "content": "x"}],
                "max_tokens": 16,
                "_cancellation_scope": scope,
                "_stop_checker": lambda: False,
            }
        )

    assert out["status"] == "ok"
    assert mock_llm.call_args.kwargs["cancellation_scope"] is scope


def test_intermediate_llm_frame_forwards_cancellation_scope():
    from plugin.scripting.venv_worker import _maybe_dispatch_intermediate_response

    scope = object()
    with patch(
        "plugin.ppt_master.venv.host_rpc.handle_llm_request",
        return_value={"status": "ok", "result": {"content": "hi"}},
    ) as mock_llm:
        handled = _maybe_dispatch_intermediate_response(
            {"type": "llm_request", "id": "9", "messages": []},
            stdin_write=lambda blob: None,
            cancellation_scope=scope,
            caller="ppt_master_venv",
        )

    assert handled is True
    assert mock_llm.call_args[0][0]["_cancellation_scope"] is scope


def test_dispatch_llm_request_keeps_user_stopped_code():
    """Stop during llm_request must arrive as code USER_STOPPED, not a bare error."""
    from plugin.framework.errors import ToolExecutionError
    from plugin.ppt_master.venv.host_rpc import dispatch_worker_response
    from plugin.ppt_master.venv.ipc import UserStopped, _raise_if_stopped

    client = MagicMock()
    client.request_with_tools.side_effect = ToolExecutionError(
        "LLM request stopped by user.", code="USER_STOPPED"
    )
    written: list[bytes] = []
    with (
        patch("plugin.framework.queue_executor.execute_on_main_thread", side_effect=lambda fn: fn()),
        patch("plugin.framework.uno_context.get_ctx", return_value=MagicMock()),
        patch("plugin.framework.config.get_api_config", return_value={"model": "m"}),
        patch("plugin.framework.client.llm_client.LlmClient", return_value=client),
    ):
        handled = dispatch_worker_response(
            {"type": "llm_request", "id": "stop-1", "messages": [{"role": "user", "content": "x"}]},
            stdin_write=written.append,
        )

    assert handled is True
    resp = read_pickle_frame(io.BytesIO(written[0]), require_dict=True)
    assert resp is not None
    assert resp["status"] == "error"
    assert resp["id"] == "stop-1"
    assert resp["code"] == "USER_STOPPED"
    assert resp["message"] == "LLM request stopped by user."
    with pytest.raises(UserStopped, match="LLM request stopped by user."):
        _raise_if_stopped(resp)


def test_dispatch_llm_request_omits_code_when_handler_has_none():
    from plugin.ppt_master.venv.host_rpc import dispatch_worker_response

    written: list[bytes] = []
    with patch(
        "plugin.ppt_master.venv.host_rpc.handle_llm_request",
        return_value={"status": "error", "message": "LLM request failed"},
    ):
        handled = dispatch_worker_response(
            {"type": "llm_request", "id": "err-1", "messages": [{"role": "user", "content": "x"}]},
            stdin_write=written.append,
        )

    assert handled is True
    resp = read_pickle_frame(io.BytesIO(written[0]), require_dict=True)
    assert resp is not None
    assert resp["status"] == "error"
    assert resp["message"] == "LLM request failed"
    assert "code" not in resp


def test_handle_llm_request_returns_user_stopped():
    from plugin.framework.errors import ToolExecutionError
    from plugin.ppt_master.venv.host_rpc import handle_llm_request

    client = MagicMock()
    client.request_with_tools.side_effect = ToolExecutionError("LLM request stopped by user.", code="USER_STOPPED")
    with (
        patch("plugin.framework.queue_executor.execute_on_main_thread", side_effect=lambda fn: fn()),
        patch("plugin.framework.uno_context.get_ctx", return_value=MagicMock()),
        patch("plugin.framework.config.get_api_config", return_value={"model": "m"}),
        patch("plugin.framework.client.llm_client.LlmClient", return_value=client),
    ):
        out = handle_llm_request({"messages": [{"role": "user", "content": "x"}], "max_tokens": 16})
    assert out["status"] == "error"
    assert out["code"] == "USER_STOPPED"


def test_execute_ppt_master_turn_passes_frame_session_id():
    from plugin.scripting.venv_worker import PythonWorkerManager

    manager = PythonWorkerManager("/usr/bin/python3", {})
    with (
        patch.object(manager, "_acquire_io", return_value=None),
        patch.object(manager, "_release_io"),
        patch.object(manager, "_ensure_warmed_unlocked", return_value=None),
        patch.object(manager, "_execute_ipc_unlocked", return_value={"status": "ok", "result": {"status": "ok"}}) as mock_ipc,
    ):
        manager.execute_ppt_master_turn(
            {"query": "q", "session_id": "ppt_master:file:///deck-b.odp"},
            timeout_sec=5,
        )
    assert mock_ipc.call_args.kwargs["session_id"] == "ppt_master:file:///deck-b.odp"
    assert mock_ipc.call_args.kwargs["data"]["session_id"] == "ppt_master:file:///deck-b.odp"


def test_execute_ppt_master_turn_does_not_pickle_cancellation_scope():
    from plugin.scripting.venv_worker import PythonWorkerManager

    scope = object()
    manager = PythonWorkerManager("/usr/bin/python3", {})
    with (
        patch.object(manager, "_acquire_io", return_value=None),
        patch.object(manager, "_release_io"),
        patch.object(manager, "_ensure_warmed_unlocked", return_value=None),
        patch.object(manager, "_execute_ipc_unlocked", return_value={"status": "ok", "result": {"status": "ok"}}) as mock_ipc,
    ):
        manager.execute_ppt_master_turn({"query": "q"}, timeout_sec=5, cancellation_scope=scope)

    assert mock_ipc.call_args.kwargs["cancellation_scope"] is scope
    assert "cancellation_scope" not in mock_ipc.call_args.kwargs["data"]

def test_handle_llm_request_returns_user_stopped_via_client_stopped():
    from plugin.ppt_master.venv.host_rpc import handle_llm_request

    client = MagicMock()
    client._stopped = True
    client.request_with_tools.return_value = {"role": "assistant", "content": "ok", "tool_calls": None}
    with (
        patch("plugin.framework.queue_executor.execute_on_main_thread", side_effect=lambda fn: fn()),
        patch("plugin.framework.uno_context.get_ctx", return_value=MagicMock()),
        patch("plugin.framework.config.get_api_config", return_value={"model": "m"}),
        patch("plugin.framework.client.llm_client.LlmClient", return_value=client),
    ):
        out = handle_llm_request(
            {
                "messages": [{"role": "user", "content": "x"}],
                "max_tokens": 16,
            }
        )

    assert out["status"] == "error"
    assert out["code"] == "USER_STOPPED"

def test_handle_llm_request_returns_user_stopped_via_stop_checker():
    from plugin.ppt_master.venv.host_rpc import handle_llm_request

    client = MagicMock()
    client._stopped = False
    client.request_with_tools.return_value = {"role": "assistant", "content": "ok", "tool_calls": None}
    with (
        patch("plugin.framework.queue_executor.execute_on_main_thread", side_effect=lambda fn: fn()),
        patch("plugin.framework.uno_context.get_ctx", return_value=MagicMock()),
        patch("plugin.framework.config.get_api_config", return_value={"model": "m"}),
        patch("plugin.framework.client.llm_client.LlmClient", return_value=client),
    ):
        out = handle_llm_request(
            {
                "messages": [{"role": "user", "content": "x"}],
                "max_tokens": 16,
                "_stop_checker": lambda: True
            }
        )

    assert out["status"] == "error"
    assert out["code"] == "USER_STOPPED"
