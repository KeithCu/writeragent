"""Tests for per-send cancellation (Stop button + sub-agent HTTP)."""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest

from plugin.chatbot.smol_agent import WriterAgentSmolModel
from plugin.doc.document_research_specialized import DelegateReadDocument
from plugin.framework.client.llm_client import LlmClient
from plugin.framework.queue_executor import SendCancellation, QueueExecutor, SendCancelled, agent_session, default_executor, is_agent_active, wait_for_approval


def test_wait_for_approval_set_event_wins_over_stop():
    """A signaled approval must not lose to a stop that lands in the same wake."""
    event = threading.Event()
    event.set()
    assert wait_for_approval(event, lambda: True, timeout=0.01) is True


def test_wait_for_approval_stop_while_unset_returns_false():
    """Sidebar close latches stop and never sets the approval event."""
    event = threading.Event()
    assert wait_for_approval(event, lambda: True, timeout=0.01) is False
    assert event.is_set() is False


def test_wait_for_approval_without_checker_waits_until_set():
    event = threading.Event()
    threading.Thread(target=event.set, daemon=True).start()
    assert wait_for_approval(event, None, timeout=0.05) is True


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


def test_send_cancellation_late_register_after_cancel_stops_immediately():
    """B13: Stop before drain registers the client — register must still call stop()."""
    scope = SendCancellation()
    scope.cancel()
    client = MagicMock()
    scope.register_client(client)
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


def test_unbound_cancel_leaves_default_executor_queue():
    """Stop before bind must not wipe MCP/grammar/peer work on default_executor."""
    import queue as queue_mod

    from plugin.framework.queue_executor import _WorkItem

    item = _WorkItem("unbound-cancel", lambda: None, (), {}, blocking=True)
    default_executor._work_queue.put(item)
    try:
        scope = SendCancellation()
        scope.cancel()
        assert scope.is_cancelled()
        assert item.cancelled is False
        assert item.event is not None and not item.event.is_set()
    finally:
        leftover = []
        while True:
            try:
                leftover.append(default_executor._work_queue.get_nowait())
            except queue_mod.Empty:
                break
        for other in leftover:
            if other is not item:
                default_executor._work_queue.put(other)


def test_bound_cancel_wakes_only_that_scope():
    """Stop cancels marshals tagged with this send and leaves the rest queued."""
    import queue as queue_mod

    from plugin.framework.queue_executor import _current_send_cancellation

    with patch.object(default_executor, "_poke_main_thread"):
        with agent_session() as scope:
            mine = default_executor._enqueue_work(lambda: None, (), {})
            token = _current_send_cancellation.set(None)
            try:
                other = default_executor._enqueue_work(lambda: None, (), {})
            finally:
                _current_send_cancellation.reset(token)
            scope.cancel()
            assert mine.cancelled is True
            assert isinstance(mine.exception, SendCancelled)
            assert other.cancelled is False
            assert other.event is not None and not other.event.is_set()
    while True:
        try:
            default_executor._work_queue.get_nowait()
        except queue_mod.Empty:
            break


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


def test_agent_session_aborts_cancel_registered_clients():
    client = MagicMock()
    with pytest.raises(RuntimeError):
        with agent_session() as scope:
            scope.register_client(client)
            raise RuntimeError("crash during send")
    client.stop.assert_called_once()
    assert is_agent_active() is False


def test_agent_session_success_does_not_cancel():
    import queue as queue_mod

    from plugin.framework.queue_executor import _WorkItem

    client = MagicMock()
    item = _WorkItem("pending", lambda: None, (), {}, blocking=True)
    default_executor._work_queue.put(item)
    try:
        with agent_session() as scope:
            scope.register_client(client)

        client.stop.assert_not_called()
        assert not scope.is_cancelled()
        assert not item.cancelled
        assert is_agent_active() is False
    finally:
        # What was wrong: this item stayed on the process-wide work queue after
        # a passing test. The next test on the same xdist worker saw qsize() != 0.
        # How it happened: the assertion only checks that success does not cancel;
        # nothing dequeued the item.
        # Why this change: put back only items this test did not add, or
        # wait_while_pumping skips its off-main post for the whole timeout.
        leftover: list[object] = []
        while True:
            try:
                leftover.append(default_executor._work_queue.get_nowait())
            except queue_mod.Empty:
                break
        for other in leftover:
            if other is not item:
                default_executor._work_queue.put(other)


def test_agent_session_stop_then_success_does_not_double_cancel():
    client = MagicMock()
    with agent_session() as scope:
        scope.register_client(client)
        scope.cancel()
    client.stop.assert_called_once()


def test_agent_session_abort_cancels_pending_main_thread_work():
    """Abort cancels marshals tagged with this send. Earlier untagged work stays queued."""
    import queue as queue_mod

    from plugin.framework.queue_executor import _WorkItem

    earlier = _WorkItem("earlier", lambda: None, (), {}, blocking=True)
    default_executor._work_queue.put(earlier)
    mine = None
    try:
        with pytest.raises(RuntimeError):
            with agent_session():
                with patch.object(default_executor, "_poke_main_thread"):
                    mine = default_executor._enqueue_work(lambda: None, (), {})
                raise RuntimeError("crash")
        assert mine is not None and mine.cancelled
        assert mine.event is not None
        assert mine.event.wait(timeout=1.0)
        assert isinstance(mine.exception, SendCancelled)
        assert earlier.cancelled is False
    finally:
        while True:
            try:
                default_executor._work_queue.get_nowait()
            except queue_mod.Empty:
                break


def test_cancel_clears_bound_executor_not_unrelated():
    from plugin.framework.queue_executor import _WorkItem

    bound = QueueExecutor()
    other = QueueExecutor()

    scope = SendCancellation()
    bound_item = _WorkItem("bound", lambda: None, (), {}, blocking=True, scope=scope)
    other_item = _WorkItem("other", lambda: None, (), {}, blocking=True)
    bound._work_queue.put(bound_item)
    other._work_queue.put(other_item)

    scope.bind_executor(bound)
    scope.cancel()

    assert bound_item.cancelled
    assert bound_item.event is not None
    assert bound_item.event.wait(timeout=1.0)
    assert isinstance(bound_item.exception, SendCancelled)

    assert not other_item.cancelled
    assert other._work_queue.qsize() == 1


def test_smol_executor_aborts_before_next_step_when_cancelled():
    from plugin.chatbot.smol_agent import SmolAgentExecutor
    from plugin.contrib.smolagents.memory import FinalAnswerStep
    from plugin.framework.errors import ToolExecutionError

    scope = SendCancellation()
    ctx = MagicMock()
    ctx.stop_checker = scope.is_cancelled
    agent = MagicMock()
    step1 = MagicMock()
    step1.__class__.__name__ = "ActionStep"
    step2 = MagicMock()
    step2.__class__.__name__ = "ActionStep"

    def fake_run(_task, stream=True):
        yield step1
        scope.cancel()
        yield step2
        yield FinalAnswerStep(output="should not run")

    agent.run.return_value = fake_run("t")
    executor = SmolAgentExecutor(ctx)
    with pytest.raises(ToolExecutionError) as exc_info:
        executor.run(agent, "task")
    assert exc_info.value.code == "USER_STOPPED"
    agent.interrupt.assert_called_once()


def test_stop_checker_stays_true_after_panel_clears_scope_reference():
    from plugin.framework.queue_executor import bind_send_stop_checker

    scope = SendCancellation()
    stop_checker = bind_send_stop_checker(scope, lambda: False)
    scope.cancel()
    assert stop_checker() is True


def test_bound_checker_ignores_a_replaced_scope_attribute():
    from plugin.framework.queue_executor import bind_send_stop_checker, capture_send_stop

    class Host:
        def __init__(self, scope):
            self._send_cancellation = scope

        def resolve_stop_checker(self):
            return bind_send_stop_checker(self._send_cancellation, lambda: False)

    old = SendCancellation()
    host = Host(old)
    scope, checker = capture_send_stop(host)
    assert scope is old
    old.cancel()
    new = SendCancellation()
    host._send_cancellation = new
    assert checker() is True
    assert bind_send_stop_checker(new, lambda: False)() is False


def test_bind_send_stop_checker_ors_fallback_before_scope_cancel():
    from plugin.framework.queue_executor import bind_send_stop_checker

    scope = SendCancellation()
    checker = bind_send_stop_checker(scope, lambda: True)
    assert not scope.is_cancelled()
    assert checker() is True


def test_agent_session_reuses_existing_scope():
    existing = SendCancellation()
    existing.cancel()
    with agent_session(existing) as scope:
        assert scope is existing
        assert scope.is_cancelled()
    assert is_agent_active() is False


class TestSendCancellationHooksLocking:
    """Regression tests for _on_cancel_hooks not being lock-protected.

    Before the fix, register_on_cancel appended without a lock and cancel()
    iterated the live list — a concurrent append during iteration was safe only
    under CPython's GIL but incorrect in general and inconsistent with _clients.
    """

    def test_registered_hook_is_called_on_cancel(self):
        # Basic sanity: a registered hook must fire when cancel() is called.
        scope = SendCancellation()
        called = []
        scope.register_on_cancel(lambda: called.append(1))
        scope.cancel()
        assert called == [1], f"Expected hook to be called once, got: {called}"

    def test_multiple_hooks_all_called(self):
        scope = SendCancellation()
        called = []
        scope.register_on_cancel(lambda: called.append("a"))
        scope.register_on_cancel(lambda: called.append("b"))
        scope.cancel()
        assert set(called) == {"a", "b"}, f"Not all hooks were called: {called}"

    def test_concurrent_register_and_cancel_no_lost_hooks(self):
        # Registers hooks from a background thread while cancel() is called
        # concurrently. All hooks registered before cancel() must fire.
        import threading as _threading

        scope = SendCancellation()
        called = []
        barrier = _threading.Barrier(2)
        N = 50

        # Pre-register half the hooks so cancel() definitely sees some.
        for idx in range(N // 2):
            scope.register_on_cancel(lambda i=idx: called.append(i))

        def register_rest():
            barrier.wait()  # synchronise with cancel() caller
            for idx in range(N // 2, N):
                scope.register_on_cancel(lambda i=idx: called.append(i))

        t = _threading.Thread(target=register_rest)
        t.start()
        barrier.wait()  # release both threads simultaneously
        scope.cancel()
        t.join(timeout=2)

        # At minimum the pre-registered hooks must all have been called.
        pre_registered = set(range(N // 2))
        assert pre_registered.issubset(set(called)), (
            f"Some pre-registered hooks were lost: missing {pre_registered - set(called)}"
        )

