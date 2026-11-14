"""Tests for per-send cancellation (Stop button + sub-agent HTTP)."""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest

from plugin.chatbot.smol_agent import WriterAgentSmolModel
from plugin.doc.document_research_specialized import DelegateReadDocument
from plugin.framework.client.llm_client import LlmClient
from plugin.framework.queue_executor import MainThread, main
from plugin.framework.send_cancellation import SendCancellation, SendCancelled, agent_session, is_agent_active, is_cancelled, wait_for_approval


def test_wait_for_approval_set_event_wins_over_stop():
    """A signaled approval must not lose to a stop that lands in the same wake."""
    event = threading.Event()
    event.set()
    cancel = SendCancellation()
    cancel.cancel()
    assert wait_for_approval(event, cancel, timeout=0.01) is True


def test_wait_for_approval_stop_while_unset_returns_false():
    """Sidebar close latches stop and never sets the approval event."""
    event = threading.Event()
    cancel = SendCancellation()
    cancel.cancel()
    assert wait_for_approval(event, cancel, timeout=0.01) is False
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


def test_register_client_dedupes_same_client():
    scope = SendCancellation()
    client = MagicMock()
    scope.register_client(client)
    scope.register_client(client)
    scope.cancel()
    client.stop.assert_called_once()


def test_register_client_duplicate_after_cancel_stops_once():
    scope = SendCancellation()
    scope.cancel()
    client = MagicMock()
    scope.register_client(client)
    scope.register_client(client)
    client.stop.assert_called_once()


@pytest.mark.parametrize("cancel_between", [False, True], ids=["before_cancel", "after_cancel"])
def test_register_on_cancel_dedupes_bound_method(cancel_between: bool) -> None:
    class _Adapter:
        def __init__(self) -> None:
            self.calls = 0

        def stop(self) -> None:
            self.calls += 1

    adapter = _Adapter()
    scope = SendCancellation()
    scope.register_on_cancel(adapter.stop)
    if cancel_between:
        scope.cancel()
        assert adapter.calls == 1
    scope.register_on_cancel(adapter.stop)
    if not cancel_between:
        scope.cancel()
    assert adapter.calls == 1


def test_unregister_on_cancel_prevents_hook_execution():
    called = []
    scope = SendCancellation()

    def hook():
        called.append(1)

    scope.register_on_cancel(hook)
    scope.unregister_on_cancel(hook)
    scope.cancel()
    assert called == []


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


def test_writer_agent_smol_model_passes_cancel():
    api = MagicMock()
    scope = SendCancellation()
    model = WriterAgentSmolModel(api, cancel=scope)
    model.generate([{"role": "user", "content": "hi"}])
    _, kwargs = api.request_with_tools.call_args
    assert kwargs.get("cancel") is scope


def test_unbound_cancel_leaves_main_queue():
    """Stop before bind must not wipe MCP/grammar/peer work on main."""
    import queue as queue_mod

    from plugin.framework.queue_executor import _WorkItem

    item = _WorkItem(lambda: None, (), {}, blocking=True)
    main._work_queue.put(item)
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
                leftover.append(main._work_queue.get_nowait())
            except queue_mod.Empty:
                break
        for other in leftover:
            if other is not item:
                main._work_queue.put(other)


def test_bound_cancel_wakes_only_that_scope():
    """Stop cancels marshals tagged with this send and leaves the rest queued."""
    import queue as queue_mod

    from plugin.framework.send_cancellation import _current_send_cancellation

    with patch.object(main, "_poke_main_thread"):
        with agent_session() as scope:
            mine = main._enqueue_work(lambda: None, (), {})
            token = _current_send_cancellation.set(None)
            try:
                other = main._enqueue_work(lambda: None, (), {})
            finally:
                _current_send_cancellation.reset(token)
            scope.cancel()
            assert mine.cancelled is True
            assert isinstance(mine.exception, SendCancelled)
            assert other.cancelled is False
            assert other.event is not None and not other.event.is_set()
    while True:
        try:
            main._work_queue.get_nowait()
        except queue_mod.Empty:
            break


def test_cancel_pending_work_wakes_blocking_waiter():
    from plugin.framework.queue_executor import _WorkItem

    item = _WorkItem(lambda: None, (), {}, blocking=True)
    main._work_queue.put(item)
    main.cancel_pending_work()
    assert item.cancelled
    assert item.event is not None
    assert item.event.wait(timeout=1.0)
    assert item.exception is not None


@patch("plugin.framework.queue_executor.main.call")
@patch("plugin.doc.document_research_specialized.run_inner_read_agent")
@patch("plugin.doc.document_research_specialized.open_document_for_read")
@patch("plugin.doc.document_research_specialized.resolve_path_or_name")
def test_delegate_read_runs_inner_agent_off_main_thread(mock_resolve, mock_open, mock_inner, mock_main):
    mock_resolve.return_value = ("/tmp/budget.ods", None)
    mock_open.return_value = (MagicMock(), "calc", None, True)

    inner_in_marshal: list[bool] = []
    in_marshal = [False]

    def fake_inner(*_args, **_kwargs):
        inner_in_marshal.append(in_marshal[0])
        return {"status": "ok", "finished": True, "answer": "Q4=100"}

    mock_inner.side_effect = fake_inner

    captured_fns: list = []

    def capture(fn, *args, timeout=30.0, bound_scope=None, **kwargs):
        del timeout, bound_scope
        captured_fns.append(fn)
        in_marshal[0] = True
        try:
            return fn(*args, **kwargs)
        finally:
            in_marshal[0] = False

    mock_main.side_effect = capture

    tool = DelegateReadDocument()
    ctx = MagicMock()
    ctx.ctx = MagicMock()
    ctx.doc = MagicMock()
    ctx.send_cancellation = None

    result_box: dict[str, object] = {}
    err_box: list[BaseException] = []

    def _run() -> None:
        try:
            result_box["result"] = tool.execute(ctx, path_or_name="budget.ods", task="Q4")
        except BaseException as exc:
            err_box.append(exc)

    worker = threading.Thread(target=_run)
    worker.start()
    worker.join(timeout=2.0)
    assert not worker.is_alive()
    if err_box:
        raise err_box[0]

    assert result_box["result"]["status"] == "ok"
    assert mock_inner.call_count == 1
    assert inner_in_marshal == [False]
    assert mock_open in captured_fns
    assert mock_inner not in captured_fns


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
    item = _WorkItem(lambda: None, (), {}, blocking=True)
    main._work_queue.put(item)
    try:
        with agent_session() as scope:
            scope.register_client(client)

        client.stop.assert_not_called()
        assert not scope.is_cancelled()
        assert not item.cancelled
        assert is_agent_active() is False
    finally:
        # Put back only items this test did not add so leftover items from this test
        # do not pollute subsequent tests on the same worker.
        leftover: list[object] = []
        while True:
            try:
                leftover.append(main._work_queue.get_nowait())
            except queue_mod.Empty:
                break
        for other in leftover:
            if other is not item:
                main._work_queue.put(other)


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

    earlier = _WorkItem(lambda: None, (), {}, blocking=True)
    main._work_queue.put(earlier)
    mine = None
    try:
        with pytest.raises(RuntimeError):
            with agent_session():
                with patch.object(main, "_poke_main_thread"):
                    mine = main._enqueue_work(lambda: None, (), {})
                raise RuntimeError("crash")
        assert mine is not None and mine.cancelled
        assert mine.event is not None
        assert mine.event.wait(timeout=1.0)
        assert isinstance(mine.exception, SendCancelled)
        assert earlier.cancelled is False
    finally:
        while True:
            try:
                main._work_queue.get_nowait()
            except queue_mod.Empty:
                break


def test_cancel_clears_bound_executor_not_unrelated():
    from plugin.framework.queue_executor import _WorkItem

    bound = MainThread()
    other = MainThread()

    scope = SendCancellation()
    bound_item = _WorkItem(lambda: None, (), {}, blocking=True, scope=scope)
    other_item = _WorkItem(lambda: None, (), {}, blocking=True)
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
    ctx.send_cancellation = scope
    agent = MagicMock()
    step1 = MagicMock()
    step1.__class__.__name__ = "ActionStep"
    step2 = MagicMock()
    step2.__class__.__name__ = "ActionStep"

    generator_closed = False

    def fake_run(_task, stream=True):
        nonlocal generator_closed
        try:
            yield step1
            scope.cancel()
            yield step2
            yield FinalAnswerStep(output="should not run")
        finally:
            generator_closed = True

    agent.run.return_value = fake_run("t")
    executor = SmolAgentExecutor(ctx)
    with pytest.raises(ToolExecutionError) as exc_info:
        executor.run(agent, "task")
    assert exc_info.value.code == "USER_STOPPED"
    agent.interrupt.assert_called_once()
    assert generator_closed is True


def test_smol_executor_early_handler_return_interrupts_and_closes_generator():
    from plugin.chatbot.smol_agent import SmolAgentExecutor
    from plugin.contrib.smolagents.memory import ToolCall

    ctx = MagicMock()
    ctx.send_cancellation = None
    agent = MagicMock()
    tool_step = ToolCall(name="test_tool", arguments={}, id="call_1")

    generator_closed = False

    def fake_run(_task, stream=True):
        nonlocal generator_closed
        try:
            yield tool_step
            yield "unreachable"
        finally:
            generator_closed = True

    agent.run.return_value = fake_run("t")
    executor = SmolAgentExecutor(ctx)
    result = executor.run(agent, "task", tool_call_handler=lambda step: {"status": "early_exit"})
    assert result == {"status": "early_exit"}
    agent.interrupt.assert_called_once()
    assert generator_closed is True


def test_register_client_requires_stop_method():
    scope = SendCancellation()

    class _NoStop:
        pass

    with pytest.raises(AttributeError):
        scope.register_client(_NoStop())  # type: ignore[arg-type]


def test_agent_session_reuses_existing_scope():
    existing = SendCancellation()
    existing.cancel()
    with agent_session(existing) as scope:
        assert scope is existing
        assert scope.is_cancelled()
    assert is_agent_active() is False


class TestSendCancellationHooksLocking:
    """_on_cancel_hooks is lock-protected across register_on_cancel and cancel.

    Otherwise iterating the live list while another thread appends races,
    inconsistent with _clients.
    """

    @pytest.mark.parametrize("tags", [[1], ["a", "b"]], ids=["single", "multiple"])
    def test_registered_hooks_called_on_cancel(self, tags: list[object]) -> None:
        scope = SendCancellation()
        called: list[object] = []
        for tag in tags:
            scope.register_on_cancel(lambda t=tag: called.append(t))
        scope.cancel()
        assert set(called) == set(tags) and len(called) == len(tags), (
            f"Expected hooks {tags} to be called once each, got: {called}"
        )

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


def test_is_cancelled_predicate():
    """is_cancelled checks scope is not None and scope.is_cancelled()."""
    assert is_cancelled(None) is False
    scope = SendCancellation()
    assert is_cancelled(scope) is False
    scope.cancel()
    assert is_cancelled(scope) is True


def test_scope_unset_is_shared():
    """Verify _SCOPE_UNSET is identical across modules under a fresh import order."""
    import subprocess
    import sys

    code = (
        "import plugin.framework.send_cancellation as sc\n"
        "import plugin.framework.queue_executor as qe\n"
        "assert sc._SCOPE_UNSET is qe._SCOPE_UNSET\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True, timeout=60)


def test_claim_cancel_sets_flag_before_hooks():
    scope = SendCancellation()
    hook1_calls: list[int] = []
    hook2_calls: list[int] = []

    def hook1():
        hook1_calls.append(1)

    def hook2():
        hook2_calls.append(1)

    scope.register_on_cancel(hook1)
    run_cancel = scope.claim_cancel()
    assert run_cancel is not None
    assert scope.is_cancelled() is True
    assert hook1_calls == []

    # The closure runs it once
    run_cancel()
    assert hook1_calls == [1]

    # cancel() does not run it again
    scope.cancel()
    assert hook1_calls == [1]

    # register_on_cancel of that same hook does not run it again
    scope.register_on_cancel(hook1)
    assert hook1_calls == [1]

    # A different hook registered after claim runs once immediately and the closure does not run it a second time
    scope.register_on_cancel(hook2)
    assert hook2_calls == [1]
    run_cancel()
    assert hook2_calls == [1]


