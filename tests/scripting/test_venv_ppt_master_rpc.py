# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import io
from unittest.mock import MagicMock, patch

import pytest

from plugin.framework.ipc import read_pickle_frame
from plugin.framework.send_cancellation import SendCancellation


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


def test_dispatch_worker_event_callback_raise_is_ignored() -> None:
    from plugin.ppt_master.venv.host_rpc import dispatch_worker_response

    def boom(event: dict) -> None:
        raise KeyError("ui blew up")

    handled = dispatch_worker_response(
        {"type": "worker_event", "event": {"kind": "thinking"}},
        stdin_write=MagicMock(),
        on_worker_event=boom,
    )
    assert handled is True


def test_dispatch_unknown_frame_returns_false():
    from plugin.ppt_master.venv.host_rpc import dispatch_worker_response

    assert dispatch_worker_response({"status": "ok", "result": {}}, stdin_write=MagicMock()) is False


def test_dispatch_tool_call_not_handled_here():
    # tool_call frames are handled by venv_worker._maybe_dispatch_intermediate_response
    # via handle_tool_call_frame before this dispatcher runs, so it ignores them.
    from plugin.ppt_master.venv.host_rpc import dispatch_worker_response

    with patch("plugin.scripting.host_rpc.execute_tool") as mock_tool:
        handled = dispatch_worker_response(
            {"type": "tool_call", "id": "abc", "tool": "validate_ppt_master_project", "args": {"project_path": "/tmp/p"}},
            stdin_write=MagicMock(),
        )

    assert handled is False
    mock_tool.assert_not_called()


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
            cancel=lambda: False,
        )

    assert handled is True
    mock_llm.assert_called_once()
    call_payload = mock_llm.call_args[0][0]
    assert call_payload["messages"][0]["content"] == "x"
    assert "_cancel" in call_payload

    resp = read_pickle_frame(io.BytesIO(written[0]), require_dict=True)
    assert resp is not None
    assert resp["status"] == "ok"
    assert resp["result"]["content"] == "hi"


def test_handle_llm_request_requires_messages():
    from plugin.ppt_master.venv.host_rpc import handle_llm_request

    out = handle_llm_request({})
    assert out["status"] == "error"


def test_dispatch_llm_request_threads_cancel_on_the_host():
    from plugin.ppt_master.venv.host_rpc import dispatch_worker_response

    scope = SendCancellation()
    written: list[bytes] = []
    with patch("plugin.ppt_master.venv.host_rpc.handle_llm_request", return_value={"status": "ok", "result": {"content": "hi"}}) as mock_llm:
        handled = dispatch_worker_response(
            {"type": "llm_request", "id": "1", "messages": [{"role": "user", "content": "x"}]},
            stdin_write=written.append,
            cancel=scope,
        )

    assert handled is True
    payload = mock_llm.call_args[0][0]
    assert payload["_cancel"] is scope
    resp = read_pickle_frame(io.BytesIO(written[0]), require_dict=True)
    assert resp is not None
    assert "_cancel" not in resp


def test_handle_llm_request_uses_host_cancel():
    from plugin.ppt_master.venv.host_rpc import handle_llm_request

    scope = SendCancellation()
    client = MagicMock()
    client._stopped = False
    client.request_with_tools.return_value = {"role": "assistant", "content": "ok", "tool_calls": None}
    with (
        patch("plugin.framework.queue_executor.main.call", side_effect=lambda fn: fn()),
        patch("plugin.framework.uno_context.get_ctx", return_value=MagicMock()),
        patch("plugin.framework.config.get_api_config", return_value={"model": "m"}),
        patch("plugin.framework.client.llm_client.LlmClient", return_value=client) as mock_llm,
    ):
        out = handle_llm_request(
            {
                "messages": [{"role": "user", "content": "x"}],
                "max_tokens": 16,
                "_cancel": scope,
            }
        )

    assert out["status"] == "ok"
    assert mock_llm.call_args.kwargs["cancel"] is scope


def test_intermediate_llm_frame_forwards_cancel():
    from plugin.framework.send_cancellation import SendCancellation
    from plugin.scripting.venv_worker import _maybe_dispatch_intermediate_response

    scope = SendCancellation()
    with patch(
        "plugin.ppt_master.venv.host_rpc.handle_llm_request",
        return_value={"status": "ok", "result": {"content": "hi"}},
    ) as mock_llm:
        handled = _maybe_dispatch_intermediate_response(
            {"type": "llm_request", "id": "9", "messages": []},
            stdin_write=lambda blob: None,
            cancel=scope,
            caller="ppt_master_venv",
        )

    assert handled is True
    assert mock_llm.call_args[0][0]["_cancel"] is scope


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
        patch("plugin.framework.queue_executor.main.call", side_effect=lambda fn: fn()),
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
        patch("plugin.framework.queue_executor.main.call", side_effect=lambda fn: fn()),
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
    import contextlib

    @contextlib.contextmanager
    def fake_io(*args: object, **kwargs: object):
        del args, kwargs
        yield object()

    with (
        patch.object(manager, "_io_session", fake_io),
        patch.object(manager, "_execute_ipc_unlocked", return_value={"status": "ok", "result": {"status": "ok"}}) as mock_ipc,
    ):
        manager.execute_ppt_master_turn(
            {"query": "q", "session_id": "ppt_master:file:///deck-b.odp"},
            timeout_sec=5,
        )
    req = mock_ipc.call_args.args[0]
    assert req.session is not None
    assert req.session.wire() == "ppt_master:file:///deck-b.odp"
    assert req.data["session_id"] == "ppt_master:file:///deck-b.odp"
    assert req.action == "ppt_master_turn"
    assert req.caller == "ppt_master_venv"
    assert req.wire()["session_id"] == "ppt_master:file:///deck-b.odp"
    assert "code" not in req.wire()


def test_execute_ppt_master_turn_does_not_pickle_cancel():
    from plugin.scripting.venv_worker import PythonWorkerManager

    scope = object()
    manager = PythonWorkerManager("/usr/bin/python3", {})
    import contextlib

    @contextlib.contextmanager
    def fake_io(*args: object, **kwargs: object):
        del args, kwargs
        yield object()

    with (
        patch.object(manager, "_io_session", fake_io),
        patch.object(manager, "_execute_ipc_unlocked", return_value={"status": "ok", "result": {"status": "ok"}}) as mock_ipc,
    ):
        manager.execute_ppt_master_turn({"query": "q"}, timeout_sec=5, cancel=scope)

    req = mock_ipc.call_args.args[0]
    assert req.cancel is scope
    assert "cancel" not in req.data
    assert "cancel" not in req.wire()
    assert req.wire()["data"] is req.data


def test_handle_llm_request_returns_user_stopped_via_client_stopped():
    from plugin.ppt_master.venv.host_rpc import handle_llm_request

    client = MagicMock()
    client.stopped.return_value = True
    client.request_with_tools.return_value = {"role": "assistant", "content": "ok", "tool_calls": None}
    with (
        patch("plugin.framework.queue_executor.main.call", side_effect=lambda fn: fn()),
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

def test_handle_llm_request_returns_user_stopped_via_cancel():
    from plugin.framework.send_cancellation import SendCancellation
    from plugin.ppt_master.venv.host_rpc import handle_llm_request

    scope = SendCancellation()
    scope.cancel()
    client = MagicMock()
    client.stopped.return_value = False
    client.request_with_tools.return_value = {"role": "assistant", "content": "ok", "tool_calls": None}
    with (
        patch("plugin.framework.queue_executor.main.call", side_effect=lambda fn: fn()),
        patch("plugin.framework.uno_context.get_ctx", return_value=MagicMock()),
        patch("plugin.framework.config.get_api_config", return_value={"model": "m"}),
        patch("plugin.framework.client.llm_client.LlmClient", return_value=client),
    ):
        out = handle_llm_request(
            {
                "messages": [{"role": "user", "content": "x"}],
                "max_tokens": 16,
                "_cancel": scope,
            }
        )

    assert out["status"] == "error"
    assert out["code"] == "USER_STOPPED"


def test_handle_llm_request_catches_call_on_main_failure():
    """Failure during main.call(get_ctx) must be caught and returned as an error."""
    from plugin.ppt_master.venv.host_rpc import handle_llm_request

    with patch(
        "plugin.framework.queue_executor.main.call",
        side_effect=RuntimeError("main thread queue failure"),
    ):
        out = handle_llm_request(
            {
                "messages": [{"role": "user", "content": "hello"}],
                "max_tokens": 16,
            }
        )

    assert out["status"] == "error"
    assert out["code"] == "TOOL_EXECUTION_ERROR"
    assert "main thread queue failure" in out["message"]


def test_dispatch_llm_request_handles_get_ctx_failure():
    """If get_ctx via main.call fails, dispatch writes an error frame instead of raising."""
    from plugin.ppt_master.venv.host_rpc import dispatch_worker_response

    written: list[bytes] = []
    with patch(
        "plugin.framework.queue_executor.main.call",
        side_effect=RuntimeError("main thread queue failure"),
    ):
        handled = dispatch_worker_response(
            {"type": "llm_request", "id": "err-ctx", "messages": [{"role": "user", "content": "x"}]},
            stdin_write=written.append,
        )

    assert handled is True
    assert len(written) == 1
    resp = read_pickle_frame(io.BytesIO(written[0]), require_dict=True)
    assert resp is not None
    assert resp["status"] == "error"
    assert resp["id"] == "err-ctx"
    assert resp["code"] == "TOOL_EXECUTION_ERROR"
    assert "main thread queue failure" in resp["message"]


def test_handle_llm_request_uno_frame_skips_str():
    """UNO exception during host llm_request reads .Message without calling str() or repr()."""
    import plugin.framework.logging  # noqa: F401  # ensures the log-record factory is installed
    from plugin.ppt_master.venv.host_rpc import dispatch_worker_response

    calls: list[str] = []

    class HostileUno(Exception):
        __pyunostruct__ = "com.sun.star.uno.RuntimeException"
        Message = "boom"

        def __str__(self) -> str:
            calls.append("str")
            raise AssertionError("str() called on hostile UNO exception")

        def __repr__(self) -> str:
            calls.append("repr")
            raise AssertionError("repr() called on hostile UNO exception")

    written: list[bytes] = []
    with patch(
        "plugin.framework.queue_executor.main.call",
        side_effect=HostileUno(),
    ):
        handled = dispatch_worker_response(
            {"type": "llm_request", "id": "err-uno", "messages": [{"role": "user", "content": "x"}]},
            stdin_write=written.append,
        )

    assert handled is True
    assert len(written) == 1
    resp = read_pickle_frame(io.BytesIO(written[0]), require_dict=True)
    assert resp is not None
    assert resp["status"] == "error"
    assert resp["id"] == "err-uno"
    assert resp["code"] == "TOOL_EXECUTION_ERROR"
    assert resp["message"] == "boom"
    assert calls == []

    class HostileUnoEmpty(Exception):
        __pyunostruct__ = "com.sun.star.uno.RuntimeException"
        Message = ""

        def __str__(self) -> str:
            calls.append("str")
            raise AssertionError("str() called on hostile UNO exception with empty message")

        def __repr__(self) -> str:
            calls.append("repr")
            raise AssertionError("repr() called on hostile UNO exception with empty message")

    written_empty: list[bytes] = []
    with patch(
        "plugin.framework.queue_executor.main.call",
        side_effect=HostileUnoEmpty(),
    ):
        handled_empty = dispatch_worker_response(
            {"type": "llm_request", "id": "err-uno-empty", "messages": [{"role": "user", "content": "x"}]},
            stdin_write=written_empty.append,
        )

    assert handled_empty is True
    assert len(written_empty) == 1
    resp_empty = read_pickle_frame(io.BytesIO(written_empty[0]), require_dict=True)
    assert resp_empty is not None
    assert resp_empty["status"] == "error"
    assert resp_empty["id"] == "err-uno-empty"
    assert resp_empty["code"] == "TOOL_EXECUTION_ERROR"
    assert resp_empty["message"] == "HostileUnoEmpty"
    assert calls == []
