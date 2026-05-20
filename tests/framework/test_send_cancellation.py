"""Tests for per-send cancellation (Stop button + sub-agent HTTP)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from plugin.chatbot.smol_agent import WriterAgentSmolModel
from plugin.doc.document_research_specialized import DelegateReadDocument
from plugin.framework.client.llm_client import LlmClient
from plugin.framework.queue_executor import SendCancellation, agent_session, default_executor


def test_send_cancellation_stops_registered_clients():
    scope = SendCancellation()
    client = MagicMock()
    scope.register_client(client)
    scope.cancel()
    client.stop.assert_called_once()
    assert scope.is_cancelled()


def test_send_cancellation_cancel_is_idempotent():
    scope = SendCancellation()
    client = MagicMock()
    scope.register_client(client)
    scope.cancel()
    scope.cancel()
    client.stop.assert_called_once()


def test_llm_client_registers_under_agent_session():
    config = {"endpoint": "http://127.0.0.1:5000", "model": "test"}
    with agent_session() as scope:
        client = LlmClient(config, None)
        scope.cancel()
    client.stop()


def test_llm_client_not_registered_outside_agent_session():
    config = {"endpoint": "http://127.0.0.1:5000", "model": "test"}
    outside = LlmClient(config, None)
    with patch.object(outside, "_close_connection") as mock_outside:
        with agent_session() as scope:
            inside = LlmClient(config, None)
            with patch.object(inside, "_close_connection") as mock_inside:
                scope.cancel()
                mock_inside.assert_called_once()
        mock_outside.assert_not_called()


def test_writer_agent_smol_model_passes_stop_checker():
    api = MagicMock()
    checker = MagicMock(return_value=False)
    model = WriterAgentSmolModel(api, stop_checker=checker)
    model.generate([{"role": "user", "content": "hi"}])
    _, kwargs = api.request_with_tools.call_args
    assert kwargs.get("stop_checker") is checker


def test_cancel_pending_work_wakes_blocking_waiter():
    from plugin.framework.queue_executor import _WorkItem

    item = _WorkItem("id", lambda: None, (), {}, blocking=True)
    default_executor._work_queue.put(item)
    default_executor.cancel_pending_work()
    assert item.cancelled
    assert item.event is not None
    assert item.event.wait(timeout=1.0)
    assert item.exception is not None


@patch("plugin.framework.queue_executor.execute_on_main_thread")
@patch("plugin.doc.document_research_specialized.run_inner_read_agent")
@patch("plugin.doc.document_research_specialized.open_document_for_read")
@patch("plugin.doc.document_research_specialized.resolve_path_or_name")
def test_delegate_read_runs_inner_agent_off_main_thread(mock_resolve, mock_open, mock_inner, mock_main):
    mock_resolve.return_value = ("/tmp/budget.ods", None)
    mock_open.return_value = (MagicMock(), "calc", None, True)
    mock_inner.return_value = "Q4=100"

    captured_fns: list = []

    def capture(fn, *args, **kwargs):
        captured_fns.append(fn)
        return fn()

    mock_main.side_effect = capture

    tool = DelegateReadDocument()
    ctx = MagicMock()
    ctx.ctx = MagicMock()
    ctx.doc = MagicMock()
    ctx.stop_checker = lambda: False
    result = tool.execute(ctx, path_or_name="budget.ods", task="Q4")

    assert result["status"] == "ok"
    assert mock_inner.called
    inner_fn_names = [getattr(f, "__name__", "") for f in captured_fns]
    assert not any(name == "run_inner_read_agent" for name in inner_fn_names)
    assert mock_inner.call_count == 1


def test_agent_session_yields_send_cancellation():
    with agent_session() as scope:
        assert isinstance(scope, SendCancellation)
        assert not scope.is_cancelled()
