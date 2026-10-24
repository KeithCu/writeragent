import queue
import sys
import threading
import types
from unittest.mock import MagicMock, Mock, patch

import pytest


from plugin.chatbot.tool_loop_actions import ToolLoopEffectInterpreter, build_tool_execute_fn, current_turn  # noqa: E402
from plugin.chatbot.tool_loop_state import (  # noqa: E402
    AddMessageEffect,
    ExitLoopEffect,
    MessageRole,
    SpawnLLMWorkerEffect,
    SpawnToolWorkerEffect,
    ToolLoopUIEffect,
    TriggerNextToolEffect,
    UIEffectKind,
    UpdateDocumentContextEffect,
)
from plugin.framework.stream_batch import StreamQueueKind  # noqa: E402


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
        self._tool_calling = self  # fake host plays the composed ToolCallingMixin role
        self.ctx = MagicMock()
        self.session = FakeSession()
        self.image_model_selector = None
        self.audio_wav_path = None
        self._terminal_status = "Ready"
        self.appended = []
        self.statuses = []
        self.refreshed_tools = 0
        self.spawned_llm = []
        self.spawned_final = []
        self.document = Mock()
        from plugin.chatbot.tool_loop_actions import begin_send_turn

        turn = begin_send_turn(self, "chat", model=Mock(name="doc"))
        turn.queue = queue.Queue()
        turn.active_client = Mock()
        turn.active_max_tokens = 100
        turn.active_tools = [{"function": {"name": "tool"}}]
        turn.active_execute_tool_fn = Mock(return_value='{"status": "ok"}')
        turn.active_query_text = "question"
        turn.active_supports_status = False

    @property
    def _active_client(self):
        from plugin.chatbot.tool_loop_actions import current_turn

        turn = current_turn(self)
        return turn.active_client if turn else None

    @_active_client.setter
    def _active_client(self, value):
        from plugin.chatbot.tool_loop_actions import current_turn

        turn = current_turn(self)
        if turn is not None:
            turn.active_client = value

    @property
    def _active_max_tokens(self):
        from plugin.chatbot.tool_loop_actions import current_turn

        turn = current_turn(self)
        return turn.active_max_tokens if turn else 0

    @_active_max_tokens.setter
    def _active_max_tokens(self, value):
        from plugin.chatbot.tool_loop_actions import current_turn

        turn = current_turn(self)
        if turn is not None:
            turn.active_max_tokens = value

    @property
    def _active_tools(self):
        from plugin.chatbot.tool_loop_actions import current_turn

        turn = current_turn(self)
        return turn.active_tools if turn else None

    @_active_tools.setter
    def _active_tools(self, value):
        from plugin.chatbot.tool_loop_actions import current_turn

        turn = current_turn(self)
        if turn is not None:
            turn.active_tools = value

    @property
    def _active_execute_tool_fn(self):
        from plugin.chatbot.tool_loop_actions import current_turn

        turn = current_turn(self)
        return turn.active_execute_tool_fn if turn else None

    @_active_execute_tool_fn.setter
    def _active_execute_tool_fn(self, value):
        from plugin.chatbot.tool_loop_actions import current_turn

        turn = current_turn(self)
        if turn is not None:
            turn.active_execute_tool_fn = value

    @property
    def _active_query_text(self):
        from plugin.chatbot.tool_loop_actions import current_turn

        turn = current_turn(self)
        return turn.active_query_text if turn else None

    @_active_query_text.setter
    def _active_query_text(self, value):
        from plugin.chatbot.tool_loop_actions import current_turn

        turn = current_turn(self)
        if turn is not None:
            turn.active_query_text = value

    @property
    def _active_supports_status(self):
        from plugin.chatbot.tool_loop_actions import current_turn

        turn = current_turn(self)
        return turn.active_supports_status if turn else False

    @_active_supports_status.setter
    def _active_supports_status(self, value):
        from plugin.chatbot.tool_loop_actions import current_turn

        turn = current_turn(self)
        if turn is not None:
            turn.active_supports_status = value

    @property
    def _active_q(self):
        from plugin.chatbot.tool_loop_actions import current_turn

        turn = current_turn(self)
        return None if turn is None else turn.queue

    @_active_q.setter
    def _active_q(self, value):
        from plugin.chatbot.tool_loop_actions import current_turn

        turn = current_turn(self)
        if turn is not None:
            turn.queue = value

    @property
    def _active_model(self):
        from plugin.chatbot.tool_loop_actions import current_turn

        turn = current_turn(self)
        return None if turn is None else turn.model

    @_active_model.setter
    def _active_model(self, value):
        from plugin.chatbot.tool_loop_actions import current_turn

        turn = current_turn(self)
        if turn is not None:
            turn.model = value

    @property
    def _active_batched_q(self):
        from plugin.chatbot.tool_loop_actions import current_turn

        turn = current_turn(self)
        return None if turn is None else turn.batcher

    @_active_batched_q.setter
    def _active_batched_q(self, value):
        from plugin.chatbot.tool_loop_actions import current_turn

        turn = current_turn(self)
        if turn is not None:
            turn.batcher = value

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
    assert interpreter.execute(ToolLoopUIEffect(kind=UIEffectKind.STATUS, text="Ready")) is False
    assert host.statuses == ["Ready"]
    assert host._terminal_status == "Ready"

    interpreter.execute(ToolLoopUIEffect(kind=UIEffectKind.APPEND, text="hello"))
    assert host.appended == [("hello", False, "assistant")]

    interpreter.execute(AddMessageEffect(role=MessageRole.ASSISTANT, content="answer"))
    interpreter.execute(AddMessageEffect(role=MessageRole.TOOL, call_id="call_1", content='{"ok": true}'))
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


def test_update_document_context_effect_reports_error_on_unexpected_refresh_failure():
    host = FakeHost()

    def failing_refresh(model, ctx):
        raise ValueError("Corrupt section tree")

    host.session.refresh_document_context = failing_refresh
    interpreter = ToolLoopEffectInterpreter(host)

    assert interpreter.execute(UpdateDocumentContextEffect()) is True
    assert host.appended[-1][0] == "\n[Error reading document: Corrupt section tree]\n"
    assert host._terminal_status == "Error"
    assert host.statuses[-1] == "Error"


def test_spawn_llm_worker_effect_refreshes_tools_before_spawning():
    host = FakeHost()
    interpreter = ToolLoopEffectInterpreter(host)

    interpreter.execute(SpawnLLMWorkerEffect(round_num=3))

    assert host.refreshed_tools == 1
    assert host.spawned_llm == [(host._active_q, current_turn(host).active_client, 100, current_turn(host).active_tools, 3, "question")]


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
    item = host._active_q.get(timeout=2)
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


def test_web_research_approval_missing_queue_does_not_run_search():
    host = FakeHost()
    host._active_q = None
    execute_fn = build_tool_execute_fn(host, "writer", None, None, MagicMock())
    registry, old_main = _install_fake_main_registry()
    try:
        with patch("plugin.chatbot.tool_loop_actions.get_config_bool", return_value=True):
            execute_fn("web_research", {"query": "paris"}, MagicMock(), MagicMock())
            called_ctx = registry.execute.call_args[0][1]
            approval_result = called_ctx.approval_callback("query", "web_research", {})
            assert approval_result == (False, None)
    finally:
        _restore_main(old_main)


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

    item = host._active_q.get(timeout=2)
    host._active_execute_tool_fn.assert_called_once()
    assert host._active_execute_tool_fn.call_args.args == ("apply_document_content", {"content": "hi"}, host._active_model, host.ctx)
    kwargs = host._active_execute_tool_fn.call_args.kwargs
    assert kwargs["stop_checker"]() is False
    assert "captured_q" not in kwargs
    assert "captured_call_id" not in kwargs
    from plugin.chatbot.tool_loop_actions import current_turn

    assert kwargs["captured_turn"] is current_turn(host)
    assert item == (StreamQueueKind.TOOL_DONE, "call_1", "apply_document_content", '{"content": "hi"}', '{"status": "ok"}')
    assert not hasattr(host, "_current_tool_call_id")


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


def test_sync_tool_returns_before_the_tool_finishes():
    """The drain caller must get back while a sync tool is still running.

    Stop is delivered by pump_ui_idle on that thread. An inline tool held
    the drain until the call returned.
    """
    host = FakeHost()
    started = threading.Event()
    release = threading.Event()

    def execute_tool(*args, **kwargs):
        started.set()
        assert release.wait(timeout=2)
        return '{"status": "ok"}'

    host._active_execute_tool_fn = execute_tool
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
    assert started.wait(timeout=2)
    release.set()
    item = host._active_q.get(timeout=2)
    assert item[0] == StreamQueueKind.TOOL_DONE


@pytest.mark.parametrize(
    "is_async",
    [
        pytest.param(False, id="test_sync_tool_stop_before_body_does_not_run_the_tool"),
        pytest.param(True, id="test_async_tool_stop_before_body_does_not_run_the_tool"),
    ],
)
def test_sync_tool_stop_before_body_does_not_run_the_tool(is_async):
    host = FakeHost()
    host.resolve_stop_checker = lambda: (lambda: True)
    started: list = []
    interpreter = ToolLoopEffectInterpreter(host)
    with patch("plugin.chatbot.tool_loop_actions.run_in_background", side_effect=_capture_background(started)):
        interpreter.execute(
            SpawnToolWorkerEffect(
                call_id="call_1",
                func_name="apply_document_content",
                func_args_str="{}",
                func_args={},
                is_async=is_async,
            )
        )
    started[0]()
    host._active_execute_tool_fn.assert_not_called()
    assert host._active_q.get_nowait()[0] == StreamQueueKind.STOPPED

def test_execute_fn_reraises_document_disposed_payload():
    """execute_safe reports disposal as a dict. Chat must not treat that as a normal result."""
    from plugin.framework.errors import DocumentDisposedError

    host = FakeHost()
    execute_fn = build_tool_execute_fn(host, "writer", None, None, MagicMock())
    registry, old_main = _install_fake_main_registry()
    registry.execute.return_value = {
        "status": "error",
        "code": "DOCUMENT_DISPOSED",
        "message": "Document was closed or disposed by LibreOffice",
    }
    try:
        with pytest.raises(DocumentDisposedError):
            execute_fn("apply_document_content", {}, MagicMock(), MagicMock())
    finally:
        _restore_main(old_main)
    registry.execute.assert_called_once()


def test_sync_document_disposed_dict_queues_error():
    host = FakeHost()
    execute_fn = build_tool_execute_fn(host, "writer", None, None, MagicMock())
    host._active_execute_tool_fn = execute_fn
    registry, old_main = _install_fake_main_registry()
    registry.execute.return_value = {
        "status": "error",
        "code": "DOCUMENT_DISPOSED",
        "message": "Document was closed or disposed by LibreOffice",
    }
    started: list = []
    interpreter = ToolLoopEffectInterpreter(host)
    try:
        with patch("plugin.chatbot.tool_loop_actions.run_in_background", side_effect=_capture_background(started)):
            interpreter.execute(
                SpawnToolWorkerEffect(
                    call_id="call_1",
                    func_name="apply_document_content",
                    func_args_str="{}",
                    func_args={},
                    is_async=False,
                )
            )
        started[0]()
    finally:
        _restore_main(old_main)
    assert host._active_q.get_nowait()[0] == StreamQueueKind.ERROR


def test_sync_disposed_payload_queues_error_for_the_spawn_document():
    """A disposed-document result ends the loop on the document the tool started with.

    execute_fn classifies with the ``doc`` argument. The worker must pass that
    same object into ``_queue_tool_failure``. Swapping host._active_model to a
    live document before the failure is scored must not turn it into TOOL_DONE.
    """

    class RuntimeException(Exception):
        pass

    class DisposedException(Exception):
        pass

    class DisposedDoc:
        def getImplementationName(self):
            raise DisposedException("disposed")

    class LiveDoc:
        def getImplementationName(self):
            return "SwXTextDocument"

    host = FakeHost()
    spawn_doc = DisposedDoc()
    host._active_model = spawn_doc
    execute_fn = build_tool_execute_fn(host, "writer", None, None, MagicMock())
    host._active_execute_tool_fn = execute_fn
    registry, old_main = _install_fake_main_registry()

    def registry_execute(name, ctx, **kwargs):
        assert ctx.doc is spawn_doc
        host._active_model = LiveDoc()
        raise RuntimeException("bridge")

    registry.execute.side_effect = registry_execute
    started: list = []
    interpreter = ToolLoopEffectInterpreter(host)
    try:
        with patch("plugin.chatbot.tool_loop_actions.run_in_background", side_effect=_capture_background(started)):
            interpreter.execute(
                SpawnToolWorkerEffect(
                    call_id="call_1",
                    func_name="apply_document_content",
                    func_args_str="{}",
                    func_args={},
                    is_async=False,
                )
            )
        started[0]()
    finally:
        _restore_main(old_main)
    item = host._active_q.get_nowait()
    assert item[0] == StreamQueueKind.ERROR
    assert host._active_q.empty()


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


@pytest.mark.parametrize("bypass", [True, 1, "yes"])
def test_execute_fn_json_bypass_thread_guard_cannot_skip_marshal(bypass):
    """A tool-call dict cannot turn off the main-thread guard.

    What was wrong: execute_fn spread the model/peer JSON into
    ToolRegistry.execute. bypass_thread_guard is keyword-only, so a true
    value skipped execute_safe and ran the body on this worker.
    """
    import json

    from plugin.framework.tool import ToolRegistry

    calls: list[str] = []
    seen: list[dict] = []

    class Probe:
        name = "sync_probe"
        description = "x"
        parameters = {"type": "object", "properties": {"note": {"type": "string"}}}
        uno_services = None
        doc_types = None

        def get_parameters(self, doc_type=None):
            return self.parameters

        def get_description(self, doc_type=None):
            return self.description

        def validate(self, *, doc_type=None, **kwargs):
            return True, None

        def is_async(self):
            return False

        def execute(self, ctx, **kwargs):
            calls.append("execute")
            seen.append(dict(kwargs))
            return {"status": "ok"}

        def execute_safe(self, ctx, **kwargs):
            calls.append("execute_safe")
            seen.append(dict(kwargs))
            return {"status": "ok", "note": kwargs.get("note")}

    class RecordingRegistry(ToolRegistry):
        def __init__(self, services):
            super().__init__(services)
            self.seen = None

        def execute(self, tool_name, ctx, *, bypass_thread_guard=False, **kwargs):
            self.seen = (bypass_thread_guard, dict(kwargs))
            return super().execute(tool_name, ctx, bypass_thread_guard=bypass_thread_guard, **kwargs)

    reg = RecordingRegistry(MagicMock())
    reg.register(Probe())  # type: ignore[arg-type]
    host = FakeHost()
    execute_fn = build_tool_execute_fn(host, "writer", None, None, MagicMock())
    args = {"bypass_thread_guard": bypass, "note": "keep"}
    fake_main = types.ModuleType("plugin.main")
    fake_main.get_tools = lambda: reg  # type: ignore[attr-defined]
    old_main = sys.modules.get("plugin.main")
    sys.modules["plugin.main"] = fake_main
    box: dict = {}

    def fake_marshal(fn):
        box["marshalled"] = True
        return fn()

    def worker():
        box["out"] = execute_fn("sync_probe", args, None, MagicMock())

    try:
        with patch("plugin.framework.tool.execute_on_main_thread", side_effect=fake_marshal):
            thread = threading.Thread(target=worker, name="tool-sync-probe")
            thread.start()
            thread.join()
    finally:
        _restore_main(old_main)

    assert args == {"bypass_thread_guard": bypass, "note": "keep"}
    assert reg.seen == (False, {"note": "keep"})
    assert box.get("marshalled") is True
    assert calls == ["execute_safe"]
    assert seen == [{"note": "keep"}]
    assert json.loads(box["out"]) == {"status": "ok", "note": "keep"}


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
    from plugin.framework.stream_batch import StreamQueueKind

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
    assert put_for_turn(turn, (StreamQueueKind.CHUNK, "Hello"))
    assert first_q.get_nowait() == (StreamQueueKind.CHUNK, "Hello")
    fold_transcript_chunk(host.session, "Hello", "assistant")
    assert turn.open_text() == "Hello"

    turn2 = begin_send_turn(host, "image")
    assert host._turn is turn2
    assert not turn.alive
    assert turn2.alive
    assert turn2.mode == "image"
    assert turn.mode == "chat"
    assert put_for_turn(turn, (StreamQueueKind.CHUNK, " late")) is False
    assert put_for_turn(turn, (StreamQueueKind.STATUS, "late-status")) is False
    assert first_q.empty()
    assert turn.open_text() == "Hello"

    abort_turn(host)
    host.session.messages = []
    persist_assistant_on_turn(host, content="after-clear")
    assert host.session.messages == []
    assert put_for_turn(turn2, (StreamQueueKind.CHUNK, " after-clear")) is False


def test_abort_discards_a_late_write():
    """After abort, enqueue is a no-op and a write against a replaced list does not land."""
    import queue

    from plugin.chatbot.tool_loop_actions import abort_turn, begin_send_turn, put_for_turn
    from plugin.framework.stream_batch import StreamQueueKind

    class Session:
        def __init__(self) -> None:
            self.messages: list[dict[str, str]] = [{"role": "user", "content": "q"}]

        def add_assistant_message(self, content=None, tool_calls=None, reasoning_replay=None) -> None:
            self.messages.append({"role": "assistant", "content": content or ""})

    class Host:
        def __init__(self) -> None:
            self.session = Session()

    host = Host()
    turn = begin_send_turn(host, "chat")
    first_q: queue.Queue = queue.Queue()
    turn.queue = first_q
    abort_turn(host)
    assert put_for_turn(turn, (StreamQueueKind.CHUNK, "late")) is False
    assert first_q.empty()
    host.session.messages = []
    turn.persist_assistant(host, content="late-row")
    assert host.session.messages == []


def test_second_send_replaces_the_turn():
    """The previous turn cannot enqueue or append once a new send owns the host."""
    import queue

    from plugin.chatbot.tool_loop_actions import begin_send_turn, put_for_turn
    from plugin.framework.stream_batch import StreamQueueKind

    class Session:
        def __init__(self) -> None:
            self.messages: list[dict[str, str]] = [{"role": "user", "content": "q"}]

        def add_assistant_message(self, content=None, tool_calls=None, reasoning_replay=None) -> None:
            self.messages.append({"role": "assistant", "content": content or ""})

    class Host:
        def __init__(self) -> None:
            self.session = Session()

    host = Host()
    first = begin_send_turn(host, "chat")
    first_q: queue.Queue = queue.Queue()
    first.queue = first_q
    second = begin_send_turn(host, "chat")
    second_q: queue.Queue = queue.Queue()
    second.queue = second_q
    assert host._turn is second
    assert not first.alive
    assert put_for_turn(first, (StreamQueueKind.CHUNK, "stale")) is False
    assert first_q.empty()
    first.persist_assistant(host, content="from-first")
    assert all(message.get("content") != "from-first" for message in host.session.messages)
    second.persist_assistant(host, content="from-second")
    assert any(message.get("content") == "from-second" for message in host.session.messages)


def test_stop_closes_a_pending_tool_call():
    """Stop keeps the assistant row and adds one cancelled tool row. It does not strip tool_calls."""
    from plugin.chatbot.tool_loop_actions import abort_turn, begin_send_turn

    call_done = {"id": "call_done", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}
    call_open = {"id": "call_open", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}

    class Session:
        def __init__(self) -> None:
            self.messages: list[dict] = [
                {"role": "user", "content": "q"},
                {
                    "role": "assistant",
                    "content": "I will look that up.",
                    "tool_calls": [call_done, call_open],
                },
                {"role": "tool", "tool_call_id": "call_done", "content": "found"},
                {"role": "assistant", "content": "Still working", "_open_transcript": True},
            ]

        def add_assistant_message(self, content=None, tool_calls=None, reasoning_replay=None) -> None:
            if self.messages and self.messages[-1].get("_open_transcript"):
                self.messages.pop()
            msg = {"role": "assistant", "content": content or ""}
            if tool_calls:
                msg["tool_calls"] = tool_calls
            self.messages.append(msg)

        def add_tool_result(self, call_id, content) -> None:
            self.messages.append({"role": "tool", "tool_call_id": call_id, "content": content})

    class Host:
        def __init__(self) -> None:
            self.session = Session()

    host = Host()
    turn = begin_send_turn(host, "chat")
    abort_turn(host)
    turn.close_stopped(host, None)
    assistant = next(message for message in host.session.messages if message.get("tool_calls"))
    assert assistant["tool_calls"] == [call_done, call_open]
    assert assistant["content"] == "I will look that up."
    cancelled = [
        message
        for message in host.session.messages
        if message.get("role") == "tool" and message.get("tool_call_id") == "call_open"
    ]
    assert len(cancelled) == 1
    assert "may have completed" in cancelled[0]["content"]
    assert any(
        message.get("role") == "assistant" and "[Stopped by user]" in str(message.get("content") or "")
        for message in host.session.messages
    )
    assert any(message.get("content") == "Still working" for message in host.session.messages)


def test_stop_keeps_open_row_instead_of_no_response():
    """Stop aborts the turn and stores the open row, not a placeholder."""
    from plugin.chatbot.rich_text_paste import fold_transcript_chunk
    from plugin.chatbot.tool_loop_actions import (
        abort_turn,
        begin_send_turn,
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
    turn.close_stopped(host, "No response.")
    contents = [message.get("content") for message in host.session.messages]
    assert "Hello" in contents
    assert any(isinstance(content, str) and "[Stopped by user]" in content for content in contents)
    assert turn.put((StreamQueueKind.CHUNK, " more")) is False


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
    started[0]()
    old_fn.assert_called_once()
    assert host._active_execute_tool_fn.call_count == 0
    assert old_fn.call_args.args[2] is old_model
    assert old_fn.call_args.kwargs["stop_checker"] is spawn_checker
    assert old_fn.call_args.kwargs["captured_turn"] is turn
    assert "captured_q" not in old_fn.call_args.kwargs
    assert "captured_call_id" not in old_fn.call_args.kwargs
    host._active_execute_tool_fn.assert_not_called()

def test_async_tool_aborted_before_run_drops_execution():
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

    # New send happens BEFORE the worker starts
    second = begin_send_turn(host, "chat")
    second_q: queue.Queue = queue.Queue()
    second.queue = second_q
    host._active_q = second_q

    started[0]()

    assert old_fn.call_count == 0
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

        # Simulate a new send happening mid-run
        from plugin.chatbot.tool_loop_actions import begin_send_turn
        host._active_model = Mock(name="new-doc")
        host._active_q = queue.Queue()
        begin_send_turn(host, "chat")

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
            patch("plugin.framework.send_cancellation.wait_for_approval", side_effect=fake_wait),
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
    assert first_q.empty()
    assert not getattr(host.session, "tool_streamed_texts", {})


def test_stop_banner_reaches_the_sidebar_after_abort():
    """panel._append_response keeps the stop line and drops a later chunk."""
    import threading

    from plugin.chatbot.audio_recorder_state import AudioRecorderState
    from plugin.chatbot.send_state import SendButtonState
    from plugin.chatbot.sidebar_state import SidebarCompositeState
    from plugin.chatbot.tool_loop_actions import abort_turn, begin_send_turn

    from tests.conftest import make_mock_send_button_listener

    send = make_mock_send_button_listener()
    send.ctx = MagicMock()
    send.rich_text_widget = None
    send.response_control = MagicMock()
    send.response_control.getModel.return_value = MagicMock()
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

        def add_assistant_message(self, content=None, tool_calls=None, reasoning_replay=None) -> None:
            self.messages.append({"role": "assistant", "content": content or ""})

    send.session = Session()
    turn = begin_send_turn(send, "chat")
    abort_turn(send)
    turn.close_stopped(send, None)
    assert any("[Stopped by user]" in str(message.get("content") or "") for message in send.session.messages)

    with (
        patch("plugin.chatbot.panel.threading.current_thread", return_value=threading.main_thread()),
        patch("plugin.chatbot.dialogs.get_control_text", return_value="Hello"),
        patch("plugin.chatbot.dialogs.set_control_text") as mock_set,
    ):
        send._append_response("\n[Stopped by user]\n")
        send._append_response(" late")
    mock_set.assert_not_called()


def test_tool_worker_keeps_spawn_scope_after_next_send():
    """execute_fn must use the scope captured at spawn, not the panel field."""
    from plugin.framework.send_cancellation import SendCancellation, bind_send_stop_checker

    host = FakeHost()
    old = SendCancellation()
    host._send_cancellation = old
    calls = []

    def resolve():
        scope = host._send_cancellation
        calls.append(scope)
        return bind_send_stop_checker(scope, lambda: False)

    host.resolve_stop_checker = resolve
    host._active_execute_tool_fn = build_tool_execute_fn(host, "writer", None, None, MagicMock())
    seen = {}
    registry, old_main = _install_fake_main_registry()

    def registry_execute(_name, ctx, **_kwargs):
        seen["scope"] = ctx.send_cancellation
        seen["checker"] = ctx.stop_checker
        return {"status": "ok"}

    registry.execute.side_effect = registry_execute
    started = []
    interpreter = ToolLoopEffectInterpreter(host)
    try:
        with patch("plugin.chatbot.tool_loop_actions.run_in_background", side_effect=_capture_background(started)):
            interpreter.execute(
                SpawnToolWorkerEffect(
                    call_id="call_old",
                    func_name="apply_document_content",
                    func_args_str="{}",
                    func_args={"content": "hi"},
                    is_async=True,
                )
            )
        assert calls == [old]
        new = SendCancellation()
        host._send_cancellation = new

        def boom():
            calls.append(host._send_cancellation)
            raise AssertionError("resolve_stop_checker inside worker")

        host.resolve_stop_checker = boom
        started[0]()
    finally:
        _restore_main(old_main)

    assert calls == [old]
    assert seen["scope"] is old
    assert seen["checker"]() is False # Because not cancelled
    assert bind_send_stop_checker(new, lambda: False)() is False

def test_spawn_tool_worker_effect_respects_bound_stop(monkeypatch):
    from unittest.mock import MagicMock
    from plugin.chatbot.tool_loop_actions import ToolLoopEffectInterpreter, SpawnToolWorkerEffect
    from plugin.framework.stream_batch import StreamQueueKind

    mock_host = MagicMock()
    mock_host._active_execute_tool_fn = MagicMock(return_value={"status": "ok"})
    mock_host._active_supports_status = False

    # Force stop_checker to return True
    monkeypatch.setattr("plugin.chatbot.tool_loop_actions.capture_send_stop", lambda host: (None, lambda: True))

    interpreter = ToolLoopEffectInterpreter(mock_host)
    effect = SpawnToolWorkerEffect(
        call_id="call_1",
        func_name="my_tool",
        func_args={"arg": "val"},
        func_args_str='{"arg": "val"}',
        is_async=False,
    )

    # Force current_turn to return a mock TurnController
    mock_turn = MagicMock()
    mock_turn.active_execute_tool_fn = MagicMock(return_value={"status": "ok"})
    mock_turn.active_supports_status = False
    mock_turn.put = MagicMock(return_value=True)
    monkeypatch.setattr("plugin.chatbot.tool_loop_actions.current_turn", lambda host: mock_turn)
    monkeypatch.setattr("plugin.chatbot.tool_loop_actions.TurnController", MagicMock)

    # Capture what run_tool passes to emit
    emitted = []
    def mock_emit(item):
        emitted.append(item)

    monkeypatch.setattr("plugin.chatbot.tool_loop_actions.put_for_turn", lambda turn, item: emitted.append(item) or True)

    # Intercept run_in_background to execute synchronously for the test
    def mock_run_in_background(func, *args, **kwargs):
        func()

    monkeypatch.setattr("plugin.chatbot.tool_loop_actions.run_in_background", mock_run_in_background)

    interpreter._spawn_tool_worker(effect)

    assert len(emitted) == 1
    assert emitted[0][0] == StreamQueueKind.STOPPED
    assert mock_host._active_execute_tool_fn.call_count == 0


def test_append_cancelled_tool_rows_reused_call_id_across_turns():
    """A tool call ID reused in a later turn gets a synthetic cancelled row if unanswered."""
    from plugin.chatbot.tool_loop_actions import _CANCELLED_TOOL, _append_cancelled_tool_rows

    messages = [
        {"role": "user", "content": "turn 1 query"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call_0", "function": {"name": "search"}}],
        },
        {"role": "tool", "tool_call_id": "call_0", "content": "turn 1 result"},
        {"role": "assistant", "content": "turn 1 done"},
        {"role": "user", "content": "turn 2 query"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call_0", "function": {"name": "search"}}],
        },
    ]

    added = _append_cancelled_tool_rows(messages)
    assert added == 1
    assert len(messages) == 7
    assert messages[6] == {
        "role": "tool",
        "tool_call_id": "call_0",
        "content": _CANCELLED_TOOL,
    }
    assert messages[2] == {
        "role": "tool",
        "tool_call_id": "call_0",
        "content": "turn 1 result",
    }


def test_append_cancelled_tool_rows_multiple_calls_partial_answer_across_turns():
    """Unanswered tool calls get cancelled rows even when earlier calls with the same ID were answered."""
    from plugin.chatbot.tool_loop_actions import _CANCELLED_TOOL, _append_cancelled_tool_rows

    messages = [
        {"role": "user", "content": "t1"},
        {"role": "assistant", "tool_calls": [{"id": "call_0"}]},
        {"role": "tool", "tool_call_id": "call_0", "content": "t1_done"},
        {"role": "user", "content": "t2"},
        {"role": "assistant", "tool_calls": [{"id": "call_0"}, {"id": "call_1"}]},
        {"role": "tool", "tool_call_id": "call_0", "content": "t2_call0_done"},
    ]

    added = _append_cancelled_tool_rows(messages)
    assert added == 1
    assert messages[-1] == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": _CANCELLED_TOOL,
    }


def test_cleanup_audio_uses_spawn_captured_model(monkeypatch, tmp_path):
    """_cleanup_audio records native audio support for the model captured at send time, not current model."""
    from plugin.chatbot.tool_loop_actions import CleanupAudioEffect, ToolLoopEffectInterpreter, begin_send_turn

    audio_file = tmp_path / "test.wav"
    audio_file.write_bytes(b"RIFF dummy audio")

    class Host:
        def __init__(self):
            self.session = None
            self.audio_wav_path = str(audio_file)

    host = Host()

    recorded = []
    def fake_set_native_audio_support(model, endpoint, supported=True):
        recorded.append((model, endpoint, supported))

    monkeypatch.setattr("plugin.chatbot.tool_loop_actions.set_native_audio_support", fake_set_native_audio_support)
    turn = begin_send_turn(host, "chat")
    # The send path captures the model the audio was attached for.
    turn.text_model = "spawn-model"
    turn.endpoint = "https://spawn-endpoint"

    interpreter = ToolLoopEffectInterpreter(host)
    interpreter.execute(CleanupAudioEffect())

    # Must have recorded the model captured when the work was spawned
    assert recorded == [("spawn-model", "https://spawn-endpoint", True)]
    assert host.audio_wav_path is None
    assert not audio_file.exists()


def test_cleanup_audio_handles_remove_failure(monkeypatch):
    """_cleanup_audio logs exception when file removal fails and still clears audio_wav_path."""
    from plugin.chatbot.tool_loop_actions import CleanupAudioEffect, ToolLoopEffectInterpreter, begin_send_turn

    class Host:
        def __init__(self):
            self.session = None
            self.audio_wav_path = "/nonexistent/path/to/test.wav"

    host = Host()
    monkeypatch.setattr("plugin.chatbot.tool_loop_actions.set_native_audio_support", lambda *a, **k: None)

    begin_send_turn(host, "chat")
    interpreter = ToolLoopEffectInterpreter(host)

    with patch("os.remove", side_effect=OSError("Permission denied")), patch("plugin.chatbot.tool_loop_actions.log.exception") as mock_log_exc:
        interpreter.execute(CleanupAudioEffect())
        assert mock_log_exc.called
        assert host.audio_wav_path is None


def test_put_for_turn_signature_and_behavior():
    """put_for_turn takes (turn, item) and enqueues on alive turn or returns False when aborted."""
    from plugin.chatbot.tool_loop_actions import TurnController, put_for_turn
    from plugin.framework.stream_batch import StreamQueueKind

    q = queue.Queue()
    turn = TurnController(None, "chat")
    turn.queue = q

    assert put_for_turn(turn, (StreamQueueKind.CHUNK, "hello")) is True
    assert q.get_nowait() == (StreamQueueKind.CHUNK, "hello")

    turn.abort()
    assert put_for_turn(turn, (StreamQueueKind.CHUNK, "late")) is False
    assert q.empty()

    assert put_for_turn(None, (StreamQueueKind.CHUNK, "none")) is False


def test_close_stopped_second_call_writes_nothing_more():
    from plugin.chatbot.rich_text_paste import fold_transcript_chunk
    from plugin.chatbot.tool_loop_actions import abort_turn, begin_send_turn

    class Session:
        def __init__(self, messages=None) -> None:
            self.messages: list[dict] = messages if messages is not None else [
                {"role": "user", "content": "q"}
            ]

        def add_assistant_message(self, content=None, tool_calls=None, reasoning_replay=None) -> None:
            if self.messages and self.messages[-1].get("_open_transcript"):
                self.messages.pop()
            msg = {"role": "assistant", "content": content or ""}
            if tool_calls:
                msg["tool_calls"] = tool_calls
            self.messages.append(msg)

        def add_tool_result(self, call_id, content) -> None:
            self.messages.append({"role": "tool", "tool_call_id": call_id, "content": content})

    class Host:
        def __init__(self, session=None) -> None:
            self.session = session if session is not None else Session()

    # Empty turn
    host = Host()
    turn = begin_send_turn(host, "chat")
    abort_turn(host)
    turn.close_stopped(host, None)
    count_after_first = len(host.session.messages)
    turn.close_stopped(host, None)
    assert len(host.session.messages) == count_after_first
    assistant_contents = [m["content"] for m in host.session.messages if m.get("role") == "assistant"]
    assert len(assistant_contents) == 2
    assert assistant_contents[0] == "No response."
    assert "[Stopped by user]" in assistant_contents[1]

    # Open row
    session2 = Session([{"role": "user", "content": "q"}])
    host2 = Host(session2)
    turn2 = begin_send_turn(host2, "chat")
    fold_transcript_chunk(session2, "Hello", "assistant")
    abort_turn(host2)
    turn2.close_stopped(host2, "No response.")
    count_after_first2 = len(host2.session.messages)
    turn2.close_stopped(host2, "No response.")
    assert len(host2.session.messages) == count_after_first2
    assistant_contents2 = [m["content"] for m in host2.session.messages if m.get("role") == "assistant"]
    assert assistant_contents2.count("Hello") == 1
    assert sum(1 for c in assistant_contents2 if "[Stopped by user]" in c) == 1
    assert "No response." not in assistant_contents2


def test_image_generate_copies_selector_text_into_args():
    host = FakeHost()
    host.image_model_selector = MagicMock()
    host.image_model_selector.getText.return_value = "flux-test"
    args = {"prompt": "a flag"}
    interpreter = ToolLoopEffectInterpreter(host)
    interpreter.execute(
        SpawnToolWorkerEffect(
            call_id="call_img",
            func_name="image_generate",
            func_args_str='{"prompt": "a flag"}',
            func_args=args,
            is_async=False,
        )
    )
    item = host._active_q.get(timeout=2)
    assert "image_model" not in args
    host._active_execute_tool_fn.assert_called_once()
    called_args = host._active_execute_tool_fn.call_args.args[1]
    assert called_args is not args
    assert called_args == {"prompt": "a flag", "image_model": "flux-test"}
    assert item[0] == StreamQueueKind.TOOL_DONE
    assert item[1] == "call_img"


def test_non_image_tool_does_not_read_image_selector():
    host = FakeHost()
    host.image_model_selector = MagicMock()
    host.image_model_selector.getText.side_effect = RuntimeError("disposed")
    args = {"content": "hi"}
    interpreter = ToolLoopEffectInterpreter(host)
    interpreter.execute(
        SpawnToolWorkerEffect(
            call_id="call_doc",
            func_name="apply_document_content",
            func_args_str='{"content": "hi"}',
            func_args=args,
            is_async=False,
        )
    )
    item = host._active_q.get(timeout=2)
    assert host.image_model_selector.getText.call_count == 0
    host._active_execute_tool_fn.assert_called_once()
    called_args = host._active_execute_tool_fn.call_args.args[1]
    assert called_args is args
    assert item[0] == StreamQueueKind.TOOL_DONE


def test_image_selector_failure_does_not_abort_image_generate():
    host = FakeHost()
    host.image_model_selector = MagicMock()
    host.image_model_selector.getText.side_effect = RuntimeError("disposed")
    args = {"prompt": "a flag"}
    interpreter = ToolLoopEffectInterpreter(host)
    interpreter.execute(
        SpawnToolWorkerEffect(
            call_id="call_img_err",
            func_name="image_generate",
            func_args_str='{"prompt": "a flag"}',
            func_args=args,
            is_async=False,
        )
    )
    item = host._active_q.get(timeout=2)
    assert host.image_model_selector.getText.call_count == 1
    host._active_execute_tool_fn.assert_called_once()
    called_args = host._active_execute_tool_fn.call_args.args[1]
    assert "image_model" not in called_args
    assert called_args == {"prompt": "a flag"}
    assert item[0] == StreamQueueKind.TOOL_DONE


def test_begin_send_turn_snapshots_message_count():
    from plugin.chatbot.tool_loop_actions import TurnController, begin_send_turn

    class Host:
        def __init__(self, messages=None):
            self.session = MagicMock()
            self.session.messages = messages

    host = Host([{"role": "system", "content": "s"}, {"role": "user", "content": "u"}])
    turn = begin_send_turn(host, "chat")
    assert isinstance(turn, TurnController)
    assert turn.send_start_msg_count == 2
    host.session.messages.append({"role": "user", "content": "u2"})
    assert turn.send_start_msg_count == 2
    assert turn.stored_user_row() is True

    host_none = Host(None)
    turn_none = begin_send_turn(host_none, "chat")
    assert turn_none.send_start_msg_count == 0
    assert turn_none.stored_user_row() is False


def test_stored_user_row_ignores_earlier_user_rows():
    from plugin.chatbot.tool_loop_actions import begin_send_turn

    class Host:
        def __init__(self, messages):
            self.session = MagicMock()
            self.session.messages = messages

    host = Host([{"role": "user", "content": "earlier"}])
    turn = begin_send_turn(host, "chat")
    assert turn.stored_user_row() is False
    host.session.messages.append({"role": "assistant", "content": "reply"})
    assert turn.stored_user_row() is False
    host.session.messages.append({"role": "user", "content": "new user"})
    assert turn.stored_user_row() is True


def test_put_flushes_pending_chunks_before_control_item():
    from plugin.chatbot.tool_loop_actions import TurnController
    from plugin.framework.stream_batch import BatchingStreamQueue

    raw_q: queue.Queue = queue.Queue()
    bq = BatchingStreamQueue(raw_q, batch_interval=60.0)
    turn = TurnController(session=None, mode="chat")
    turn.queue = raw_q
    turn.batcher = bq

    assert turn.put((StreamQueueKind.CHUNK, "hel")) is True
    assert turn.put((StreamQueueKind.CHUNK, "lo")) is True
    assert raw_q.empty()

    stream_done_item = (StreamQueueKind.STREAM_DONE, None)
    assert turn.put(stream_done_item) is True
    first = raw_q.get_nowait()
    assert first == (StreamQueueKind.CHUNK, "hello")
    second = raw_q.get_nowait()
    assert second == stream_done_item
    assert raw_q.empty()

    assert turn.put((StreamQueueKind.CHUNK, "tail")) is True
    turn.abort()
    assert raw_q.empty()
    assert turn.put((StreamQueueKind.CHUNK, "more")) is False


def test_new_turn_starts_clean_without_copying_previous_turn_client():
    from plugin.chatbot.tool_loop_actions import begin_send_turn, current_turn

    host = MagicMock()
    host.session = MagicMock()
    host.session.messages = []
    first_turn = begin_send_turn(host, "chat")
    client = MagicMock()
    first_turn.active_client = client

    second_turn = begin_send_turn(host, "chat")
    assert current_turn(host) is second_turn
    assert second_turn.active_client is None
    assert first_turn.active_client is client


def test_turn_controller_can_write_guards():
    from typing import Any
    from plugin.chatbot.tool_loop_actions import TurnController

    class FakeSession:
        def __init__(self) -> None:
            self.messages: list[Any] = []

    class FakeHost:
        def __init__(self, session: Any) -> None:
            self.session = session
            self._turn: Any = None

    session = FakeSession()
    host = FakeHost(session)
    turn = TurnController(session, "chat")
    host._turn = turn

    # Normal turn can write
    assert turn.can_write(host) is True

    # Host is None or different
    assert turn.can_write(None) is False
    other_host = FakeHost(session)
    assert turn.can_write(other_host) is False

    # Closed by document prevents write
    turn.closed_by_document = True
    assert turn.can_write(host) is False
    turn.closed_by_document = False
    assert turn.can_write(host) is True

    # None session prevents write
    turn_no_sess = TurnController(None, "chat")
    host_no_sess = FakeHost(None)
    host_no_sess._turn = turn_no_sess
    assert turn_no_sess.can_write(host_no_sess) is False

    # Replaced messages list (e.g. Clear) prevents write
    session.messages = []
    assert turn.can_write(host) is False

    # Aborting the turn still allows can_write (needed for Stop to persist stop line)
    session2 = FakeSession()
    host2 = FakeHost(session2)
    turn2 = TurnController(session2, "chat")
    host2._turn = turn2
    turn2.abort()
    assert turn2.alive is False
    assert turn2.can_write(host2) is True


def test_interpreter_handles_tool_running_and_result_effects():
    from plugin.chatbot.tool_loop_state import MessageRole, UIEffectKind

    host = FakeHost()
    interpreter = ToolLoopEffectInterpreter(host)

    # Tool running effect
    eff_running = ToolLoopUIEffect(kind=UIEffectKind.TOOL_RUNNING, func_name="read_cell_range", func_args={})
    assert interpreter.execute(eff_running) is False
    assert host.statuses[-1] == "Running: read_cell_range"
    assert host.appended[-1] == ("[Running tool: read_cell_range...]\n", False, "assistant")

    # Tool result effect
    eff_result = ToolLoopUIEffect(
        kind=UIEffectKind.TOOL_RESULT,
        func_name="read_cell_range",
        func_args={},
        result_data={"status": "ok", "message": "done"},
    )
    assert interpreter.execute(eff_result) is False
    assert host.appended[-1] == ("[read_cell_range: done]\n", False, "assistant")

    # apply_document_content zero-replaced result appends debug line
    eff_zero = ToolLoopUIEffect(
        kind=UIEffectKind.TOOL_RESULT,
        func_name="apply_document_content",
        func_args={},
        func_args_str='{"target": "search"}',
        result_data={"status": "error", "replaced_count": 0},
    )
    interpreter.execute(eff_zero)
    assert any("[Debug: params" in item[0] for item in host.appended)

    # Empty model debug effect
    eff_empty = ToolLoopUIEffect(
        kind=UIEffectKind.EMPTY_MODEL_DEBUG,
        round_num=1,
        response={"finish_reason": "stop", "content": None},
    )
    interpreter.execute(eff_empty)
    assert any("finish_reason='stop'" in item[0] for item in host.appended)

    # Empty model AddMessageEffect
    eff_empty_msg = AddMessageEffect(
        role=MessageRole.ASSISTANT,
        round_num=1,
        response={"finish_reason": "stop", "content": None},
    )
    interpreter.execute(eff_empty_msg)
    last_msg = host.session.assistant_messages[-1]
    assert "No text from model" in last_msg["content"]
    assert "finish_reason='stop'" in last_msg["content"]


def test_turn_session_or_stop():
    from plugin.chatbot.tool_loop_actions import _turn_session_or_stop, abort_turn

    class Host:
        def __init__(self):
            self.session = MagicMock()

    host = Host()
    session = _turn_session_or_stop(host)
    assert session is host.session

    abort_turn(host)
    assert _turn_session_or_stop(host) is None
