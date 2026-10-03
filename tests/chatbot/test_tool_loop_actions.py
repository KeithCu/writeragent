import queue
import sys
import types
from unittest.mock import MagicMock, Mock, patch

import pytest


from plugin.chatbot.tool_loop_actions import ToolLoopEffectInterpreter, build_tool_execute_fn  # noqa: E402
from plugin.chatbot.tool_loop_state import (  # noqa: E402
    AddMessageEffect,
    ExitLoopEffect,
    SpawnLLMWorkerEffect,
    SpawnToolWorkerEffect,
    ToolLoopUIEffect,
    TriggerNextToolEffect,
    UpdateDocumentContextEffect,
)
from plugin.framework.async_stream import StreamQueueKind  # noqa: E402


class FakeSession:
    def __init__(self):
        self.assistant_messages = []
        self.tool_results = []
        self.system_context = None
        self.refresh_calls = []

    def add_assistant_message(self, content=None, tool_calls=None, reasoning_replay=None):
        self.assistant_messages.append(
            {
                "content": content,
                "tool_calls": tool_calls,
                "reasoning_replay": reasoning_replay,
            }
        )

    def add_tool_result(self, call_id, content):
        self.tool_results.append((call_id, content))

    def set_system_context(self, base_prompt, doc_text=""):
        self.system_context = (base_prompt, doc_text)

    def refresh_document_context(self, model, ctx):
        self.refresh_calls.append((model, ctx))
        self.set_system_context("base prompt", "fresh doc")


class FakeHost:
    def __init__(self):
        self.ctx = MagicMock()
        self.session = FakeSession()
        self.image_model_selector = None
        self.audio_wav_path = None
        self._active_q = queue.Queue()
        self._active_batched_q = None
        self._active_client = Mock()
        self._active_max_tokens = 100
        self._active_tools = [{"function": {"name": "tool"}}]
        self._active_execute_tool_fn = Mock(return_value='{"status": "ok"}')
        self._active_model = Mock()
        self._active_query_text = "question"
        self._active_supports_status = False
        self._current_tool_call_id = None
        self._terminal_status = "Ready"
        self.appended = []
        self.statuses = []
        self.refreshed_tools = 0
        self.spawned_llm = []
        self.spawned_final = []
        self.document = Mock()

    def _append_response(self, text, is_thinking=False, role="assistant"):
        self.appended.append((text, is_thinking, role))

    def _set_status(self, text):
        self.statuses.append(text)

    def _get_document_model(self):
        return self.document

    def _refresh_active_tools_for_session(self):
        self.refreshed_tools += 1

    def _spawn_llm_worker(self, q, client, max_tokens, tools, round_num, query_text=None):
        self.spawned_llm.append((q, client, max_tokens, tools, round_num, query_text))

    def _spawn_final_stream(self, q, client, max_tokens):
        self.spawned_final.append((q, client, max_tokens))

    def resolve_stop_checker(self):
        return lambda: False


def test_interpreter_handles_session_ui_queue_and_exit_effects():
    host = FakeHost()
    interpreter = ToolLoopEffectInterpreter(host)

    assert interpreter.execute(ExitLoopEffect()) is True
    assert interpreter.execute(ToolLoopUIEffect(kind="status", text="Ready")) is False
    assert host.statuses == ["Ready"]
    assert host._terminal_status == "Ready"

    interpreter.execute(ToolLoopUIEffect(kind="append", text="hello"))
    assert host.appended == [("hello", False, "assistant")]

    interpreter.execute(AddMessageEffect(role="assistant", content="answer"))
    interpreter.execute(AddMessageEffect(role="tool", call_id="call_1", content='{"ok": true}'))
    assert host.session.assistant_messages[0]["content"] == "answer"
    assert host.session.tool_results == [("call_1", '{"ok": true}')]

    interpreter.execute(TriggerNextToolEffect())
    assert host._active_q.get_nowait() == (StreamQueueKind.NEXT_TOOL,)


def test_update_document_context_effect_refreshes_session_context():
    host = FakeHost()
    interpreter = ToolLoopEffectInterpreter(host)

    interpreter.execute(UpdateDocumentContextEffect())

    assert host.session.refresh_calls == [(host.document, host.ctx)]
    assert host.session.system_context == ("base prompt", "fresh doc")


def test_update_document_context_effect_stops_when_document_is_gone():
    host = FakeHost()
    host.document = None
    interpreter = ToolLoopEffectInterpreter(host)

    assert interpreter.execute(UpdateDocumentContextEffect()) is True
    assert host.appended[-1][0] == "\n[Document closed or unavailable.]\n"
    assert host._terminal_status == "Error"
    assert host.statuses[-1] == "Error"
    assert host.session.refresh_calls == []


def test_spawn_llm_worker_effect_refreshes_tools_before_spawning():
    host = FakeHost()
    interpreter = ToolLoopEffectInterpreter(host)

    interpreter.execute(SpawnLLMWorkerEffect(round_num=3))

    assert host.refreshed_tools == 1
    assert host.spawned_llm == [(host._active_q, host._active_client, 100, host._active_tools, 3, "question")]


def test_sync_tool_disposed_document_queues_error_not_tool_done():
    class DisposedException(Exception):
        pass

    host = FakeHost()
    host._active_execute_tool_fn.side_effect = DisposedException("document closed")
    interpreter = ToolLoopEffectInterpreter(host)
    interpreter.execute(
        SpawnToolWorkerEffect(
            call_id="call_1",
            func_name="apply_document_content",
            func_args_str="{}",
            func_args={},
            is_async=False,
        )
    )
    item = host._active_q.get_nowait()
    assert item[0] == StreamQueueKind.ERROR
    assert host._active_q.empty()


def test_web_research_approval_setup_failure_does_not_run_search():
    host = FakeHost()
    execute_fn = build_tool_execute_fn(host, "writer", None, None, MagicMock())
    registry, old_main = _install_fake_main_registry()
    try:
        with patch("plugin.chatbot.tool_loop_actions.get_config_bool", side_effect=RuntimeError("config")):
            out = execute_fn("web_research", {"query": "paris"}, MagicMock(), MagicMock())
    finally:
        _restore_main(old_main)
    registry.execute.assert_not_called()
    assert "WEB_RESEARCH_APPROVAL_UNAVAILABLE" in out


def test_execute_fn_reraises_disposed_document():
    class DisposedException(Exception):
        pass

    host = FakeHost()
    execute_fn = build_tool_execute_fn(host, "writer", None, None, MagicMock())
    registry, old_main = _install_fake_main_registry()
    registry.execute.side_effect = DisposedException("gone")
    try:
        with pytest.raises(DisposedException):
            execute_fn("apply_document_content", {"content": "hi"}, MagicMock(), MagicMock())
    finally:
        _restore_main(old_main)


def test_spawn_tool_worker_effect_runs_sync_tool_and_enqueues_result():
    host = FakeHost()
    interpreter = ToolLoopEffectInterpreter(host)

    interpreter.execute(
        SpawnToolWorkerEffect(
            call_id="call_1",
            func_name="apply_document_content",
            func_args_str='{"content": "hi"}',
            func_args={"content": "hi"},
            is_async=False,
        )
    )

    host._active_execute_tool_fn.assert_called_once_with("apply_document_content", {"content": "hi"}, host._active_model, host.ctx)
    assert host._active_q.get_nowait() == (StreamQueueKind.TOOL_DONE, "call_1", "apply_document_content", '{"content": "hi"}', '{"status": "ok"}')
    assert host._current_tool_call_id == "call_1"


def _install_fake_main_registry():
    """Avoid importing plugin.main (UNO-heavy). execute_fn does a local get_tools import."""
    registry = MagicMock()
    registry.execute.return_value = {"status": "ok"}
    fake_main = types.ModuleType("plugin.main")
    fake_main.get_tools = MagicMock(return_value=registry)
    old_main = sys.modules.pop("plugin.main", None)
    sys.modules["plugin.main"] = fake_main
    return registry, old_main


def _restore_main(old_main):
    if old_main is not None:
        sys.modules["plugin.main"] = old_main
    else:
        sys.modules.pop("plugin.main", None)


@pytest.mark.parametrize("doc_type_str", ["draw", "impress"])
def test_execute_fn_marshals_draw_active_page_index(doc_type_str):
    host = FakeHost()
    execute_fn = build_tool_execute_fn(host, doc_type_str, None, None, MagicMock())
    doc = MagicMock()
    registry, old_main = _install_fake_main_registry()
    try:
        with (
            patch("plugin.draw.bridge.DrawBridge") as mock_bridge_cls,
            patch("plugin.chatbot.tool_loop_actions.execute_on_main_thread") as mock_marshal,
        ):
            mock_bridge_cls.return_value.get_active_page_index.return_value = 2
            mock_marshal.side_effect = lambda fn, *args, **kwargs: fn(*args, **kwargs)
            execute_fn(
                "delegate_to_specialized_draw_toolset",
                {"domain": "shapes", "task": "x"},
                doc,
                host.ctx,
            )
        mock_marshal.assert_called_once()
        tctx = registry.execute.call_args[0][1]
        assert tctx.active_page_index == 2
    finally:
        _restore_main(old_main)


def test_execute_fn_skips_draw_bridge_for_writer():
    host = FakeHost()
    execute_fn = build_tool_execute_fn(host, "writer", None, None, MagicMock())
    registry, old_main = _install_fake_main_registry()
    try:
        with patch("plugin.chatbot.tool_loop_actions.execute_on_main_thread") as mock_marshal:
            execute_fn("web_search", {"query": "x"}, MagicMock(), host.ctx)
        mock_marshal.assert_not_called()
        tctx = registry.execute.call_args[0][1]
        assert tctx.active_page_index is None
    finally:
        _restore_main(old_main)


def test_clear_or_second_send_does_not_apply_chunks_onto_the_first_turn():
    """A new send aborts the first turn. Late puts and a clear do not land on it."""
    import queue

    from plugin.chatbot.rich_text_paste import fold_transcript_chunk
    from plugin.chatbot.tool_loop_actions import (
        abort_turn,
        begin_send_turn,
        persist_assistant_on_turn,
        put_for_turn,
    )
    from plugin.framework.async_stream import StreamQueueKind

    class Session:
        def __init__(self) -> None:
            self.messages: list[dict[str, str]] = [{"role": "system", "content": "s"}]

        def add_assistant_message(self, content=None, tool_calls=None, reasoning_replay=None) -> None:
            self.messages.append({"role": "assistant", "content": content or ""})

    class Host:
        def __init__(self) -> None:
            self.session = Session()

    host = Host()
    turn = begin_send_turn(host, "chat")
    assert turn.mode == "chat"
    first_q: queue.Queue = queue.Queue()
    turn.queue = first_q
    assert put_for_turn(host, turn, first_q, (StreamQueueKind.CHUNK, "Hello"))
    assert first_q.get_nowait() == (StreamQueueKind.CHUNK, "Hello")
    fold_transcript_chunk(host.session, "Hello", "assistant")
    assert turn.open_text() == "Hello"

    turn2 = begin_send_turn(host, "image")
    assert host._turn is turn2
    assert not turn.alive
    assert turn2.alive
    assert turn2.mode == "image"
    assert turn.mode == "chat"
    assert put_for_turn(host, turn, first_q, (StreamQueueKind.CHUNK, " late")) is False
    assert put_for_turn(host, turn, first_q, (StreamQueueKind.STATUS, "late-status")) is False
    assert first_q.empty()
    assert turn.open_text() == "Hello"

    abort_turn(host)
    host.session.messages = []
    persist_assistant_on_turn(host, content="after-clear")
    assert host.session.messages == []
    assert put_for_turn(host, turn2, turn2.queue, (StreamQueueKind.CHUNK, " after-clear")) is False


def test_stop_keeps_open_row_instead_of_no_response():
    """Stop aborts the turn and stores the open row, not a placeholder."""
    from plugin.chatbot.rich_text_paste import fold_transcript_chunk
    from plugin.chatbot.tool_loop_actions import (
        abort_turn,
        begin_send_turn,
        persist_assistant_on_turn,
        stopped_assistant_text,
    )

    class Session:
        def __init__(self) -> None:
            self.messages: list[dict[str, str]] = [{"role": "system", "content": "s"}]

        def add_assistant_message(self, content=None, tool_calls=None, reasoning_replay=None) -> None:
            self.messages.append({"role": "assistant", "content": content or ""})

    class Host:
        def __init__(self) -> None:
            self.session = Session()

    host = Host()
    turn = begin_send_turn(host, "chat")
    fold_transcript_chunk(host.session, "Hello", "assistant")
    abort_turn(host)
    assert host._turn is turn
    assert stopped_assistant_text(host, None) == "Hello"
    assert stopped_assistant_text(host, "No response.") == "Hello"
    persist_assistant_on_turn(host, content=stopped_assistant_text(host, "No response."))
    assert any(message.get("content") == "Hello" for message in host.session.messages)
    assert turn.accepts_display(host, " more") is False
    assert turn.accepts_display(host, "\n[Stopped by user]\n") is True
    assert turn.accepts_display(host, "see [Stopped by user] later") is False
    saved_messages = host.session.messages
    host.session.messages = []
    assert turn.accepts_display(host, "\n[Stopped by user]\n") is False
    host.session.messages = saved_messages
    turn2 = begin_send_turn(host, "chat")
    assert turn.accepts_display(host, "\n[Stopped by user]\n") is False
    assert turn2.accepts_display(host, "\n[Stopped by user]\n") is True


def _capture_background(started: list):
    def capture(func, *args, **kwargs):
        started.append(func)
        return Mock()

    return capture


def test_async_tool_failure_uses_model_captured_at_spawn():
    """The tool's own document classifies the failure, not a model stored later.

    A bare RuntimeException is disposal only for the document the tool
    started against. The host model can change before the worker runs.
    A new send drops the result instead of delivering it to either queue.
    """
    from plugin.chatbot.tool_loop_actions import begin_send_turn

    class DisposedException(Exception):
        pass

    class RuntimeException(Exception):
        pass

    class DisposedDoc:
        def getImplementationName(self):
            raise DisposedException("disposed")

    class LiveDoc:
        def getImplementationName(self):
            return "SwXTextDocument"

    def run(spawn_model, later_model, expected_kind):
        host = FakeHost()
        turn = begin_send_turn(host, "chat")
        first_q: queue.Queue = queue.Queue()
        turn.queue = first_q
        host._active_q = first_q
        host._active_model = spawn_model
        host._active_execute_tool_fn = Mock(side_effect=RuntimeException("bridge"))
        started: list = []
        interpreter = ToolLoopEffectInterpreter(host)
        with patch("plugin.chatbot.tool_loop_actions.run_in_background", side_effect=_capture_background(started)):
            interpreter.execute(
                SpawnToolWorkerEffect(
                    call_id="call_old",
                    func_name="apply_document_content",
                    func_args_str="{}",
                    func_args={},
                    is_async=True,
                )
            )
        host._active_model = later_model
        started[0]()
        item = first_q.get_nowait()
        assert item[0] == expected_kind
        assert first_q.empty()

    run(DisposedDoc(), LiveDoc(), StreamQueueKind.ERROR)
    run(LiveDoc(), DisposedDoc(), StreamQueueKind.TOOL_DONE)


def test_new_send_drops_the_in_flight_tool_result():
    """A tool that finishes after the next send must not enqueue anywhere."""
    from plugin.chatbot.tool_loop_actions import begin_send_turn

    host = FakeHost()
    turn = begin_send_turn(host, "chat")
    first_q: queue.Queue = queue.Queue()
    turn.queue = first_q
    host._active_execute_tool_fn = Mock(return_value='{"status": "ok"}')
    started: list = []
    interpreter = ToolLoopEffectInterpreter(host)
    with patch("plugin.chatbot.tool_loop_actions.run_in_background", side_effect=_capture_background(started)):
        interpreter.execute(
            SpawnToolWorkerEffect(
                call_id="call_old",
                func_name="web_research",
                func_args_str="{}",
                func_args={"query": "x"},
                is_async=True,
            )
        )
    second = begin_send_turn(host, "chat")
    second_q: queue.Queue = queue.Queue()
    second.queue = second_q
    started[0]()
    assert first_q.empty()
    assert second_q.empty()


def test_async_tool_uses_fn_and_model_captured_at_spawn():
    """A later send must not retarget a tool that already started."""
    from plugin.chatbot.tool_loop_actions import begin_send_turn

    host = FakeHost()
    turn = begin_send_turn(host, "chat")
    first_q: queue.Queue = queue.Queue()
    turn.queue = first_q
    host._active_q = first_q
    old_model = Mock(name="old-model")
    old_fn = Mock(return_value='{"status": "ok"}')
    host._active_model = old_model
    host._active_execute_tool_fn = old_fn

    def spawn_checker() -> bool:
        return False

    def resolve_spawn() -> object:
        return spawn_checker

    host.resolve_stop_checker = resolve_spawn
    started: list = []
    interpreter = ToolLoopEffectInterpreter(host)
    with patch("plugin.chatbot.tool_loop_actions.run_in_background", side_effect=_capture_background(started)):
        interpreter.execute(
            SpawnToolWorkerEffect(
                call_id="call_old",
                func_name="web_research",
                func_args_str="{}",
                func_args={"query": "x"},
                is_async=True,
            )
        )
    host._active_model = Mock(name="new-model")
    host._active_execute_tool_fn = Mock(return_value='{"status": "new"}')

    def later_checker() -> bool:
        return True

    def resolve_later() -> object:
        return later_checker

    host.resolve_stop_checker = resolve_later
    second = begin_send_turn(host, "chat")
    second_q: queue.Queue = queue.Queue()
    second.queue = second_q
    host._active_q = second_q
    started[0]()
    old_fn.assert_called_once()
    assert old_fn.call_args.args[2] is old_model
    assert old_fn.call_args.kwargs["stop_checker"] is spawn_checker
    assert old_fn.call_args.kwargs["captured_turn"] is turn
    assert old_fn.call_args.kwargs["captured_q"] is first_q
    assert old_fn.call_args.kwargs["captured_call_id"] == "call_old"
    host._active_execute_tool_fn.assert_not_called()
    assert first_q.empty()
    assert second_q.empty()


def test_subagent_append_and_approval_use_the_captured_queue():
    """A new send drops chat lines and approval from the turn that spawned them."""
    from plugin.chatbot.tool_loop_actions import begin_send_turn

    host = FakeHost()
    turn = begin_send_turn(host, "chat")
    first_q: queue.Queue = queue.Queue()
    turn.queue = first_q
    host._active_q = first_q
    old_model = Mock(name="old-doc")
    host._active_model = old_model

    def spawn_checker() -> bool:
        return False

    def resolve_spawn() -> object:
        return spawn_checker

    host.resolve_stop_checker = resolve_spawn
    host._active_execute_tool_fn = build_tool_execute_fn(host, "writer", None, None, MagicMock())
    started: list = []
    registry, old_main = _install_fake_main_registry()
    seen: dict = {}

    def registry_execute(name, ctx, **kwargs):
        seen["doc"] = ctx.doc
        seen["stop"] = ctx.stop_checker
        if ctx.chat_append_callback:
            ctx.chat_append_callback("research line" if name == "web_research" else "opened doc")
        if ctx.approval_callback:
            seen["approval"] = ctx.approval_callback("paris", "web_search", {"query": "paris"})
        return {"status": "ok"}

    registry.execute.side_effect = registry_execute

    def fake_wait(event, checker):
        seen["wait_checker"] = checker
        setattr(event, "approved", True)
        return True

    interpreter = ToolLoopEffectInterpreter(host)
    try:
        with (
            patch("plugin.chatbot.tool_loop_actions.run_in_background", side_effect=_capture_background(started)),
            patch("plugin.chatbot.tool_loop_actions.get_config_bool", return_value=True),
            patch("plugin.framework.queue_executor.wait_for_approval", side_effect=fake_wait),
        ):
            interpreter.execute(
                SpawnToolWorkerEffect(
                    call_id="call_web",
                    func_name="web_research",
                    func_args_str="{}",
                    func_args={"query": "paris"},
                    is_async=True,
                )
            )
            interpreter.execute(
                SpawnToolWorkerEffect(
                    call_id="call_doc",
                    func_name="delegate_to_specialized_writer_toolset",
                    func_args_str="{}",
                    func_args={"domain": "document_research", "task": "read"},
                    is_async=True,
                )
            )
            host._active_model = Mock(name="new-doc")
            host._active_q = queue.Queue()
            new_turn = begin_send_turn(host, "chat")
            new_q: queue.Queue = queue.Queue()
            new_turn.queue = new_q
            host._active_q = new_q

            def later_checker() -> bool:
                return True

            def resolve_later() -> object:
                return later_checker

            host.resolve_stop_checker = resolve_later
            for fn in started:
                fn()
    finally:
        _restore_main(old_main)

    assert seen["doc"] is old_model
    assert seen["stop"] is spawn_checker
    assert "wait_checker" not in seen
    assert seen["approval"] == (False, None)
    assert new_q.empty()
    assert first_q.empty()
    assert not getattr(host.session, "tool_streamed_texts", {})


def test_stop_banner_reaches_the_sidebar_after_abort():
    """panel._append_response keeps the stop line and drops a later chunk."""
    import threading

    from plugin.chatbot.audio_recorder_state import AudioRecorderState
    from plugin.chatbot.panel import SendButtonListener
    from plugin.chatbot.send_state import SendButtonState
    from plugin.chatbot.sidebar_state import SidebarCompositeState
    from plugin.chatbot.tool_loop_actions import abort_turn, begin_send_turn

    with patch.object(SendButtonListener, "__init__", lambda self, *a, **k: None):
        send = SendButtonListener.__new__(SendButtonListener)
    send.ctx = MagicMock()
    send.rich_text_widget = None
    send.response_control = MagicMock()
    send.response_control.getModel.return_value = MagicMock()
    send._plain_text_stripper = None
    send._should_auto_scroll = MagicMock(return_value=False)
    send._scroll_response_to_bottom = MagicMock()
    send.queue_executor = MagicMock()
    send.sidebar_state = SidebarCompositeState(
        send=SendButtonState(True, False, False, False, False),
        tool_loop=None,
        audio=AudioRecorderState(status="idle"),
    )

    class Session:
        def __init__(self) -> None:
            self.messages: list[dict[str, str]] = [{"role": "system", "content": "s"}]

    send.session = Session()
    begin_send_turn(send, "chat")
    abort_turn(send)

    with (
        patch("plugin.chatbot.panel.threading.current_thread", return_value=threading.main_thread()),
        patch("plugin.chatbot.dialogs.get_control_text", return_value="Hello"),
        patch("plugin.chatbot.dialogs.set_control_text") as mock_set,
    ):
        send._append_response("\n[Stopped by user]\n")
        written = mock_set.call_args[0][1]
        mock_set.reset_mock()
        send._append_response(" late")
    assert "[Stopped by user]" in written
    mock_set.assert_not_called()
