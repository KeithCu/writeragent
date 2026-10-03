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
    """Clear and a second send bump the generation. Late chunks stay off turn 1."""
    import queue

    from plugin.chatbot.tool_loop_actions import (
        begin_send_turn,
        bump_send_generation,
        chunk_applies,
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
    chunk = first_q.get_nowait()
    assert chunk_applies(host, turn, chunk[1], record=True)
    assert turn.emitted == "Hello"

    turn2 = begin_send_turn(host, "image")
    assert turn2.generation != turn.generation
    assert turn2.mode == "image"
    assert turn.mode == "chat"
    # The first worker still holds its queue. A display put must not land there
    # or on the new turn, and must not extend the first turn's text.
    assert put_for_turn(host, turn, first_q, (StreamQueueKind.CHUNK, " late")) is False
    assert first_q.empty()
    assert chunk_applies(host, turn, " late", record=True) is False
    assert turn.emitted == "Hello"
    host._apply_turn = turn
    persist_assistant_on_turn(host, content="late reply")
    host._apply_turn = None
    assert all(message.get("content") != "late reply" for message in host.session.messages)
    assert chunk_applies(host, turn2, "Second", record=True)
    assert turn2.emitted == "Second"
    assert "Second" not in turn.emitted

    bump_send_generation(host)
    host.session.messages = []
    assert chunk_applies(host, turn, " after-clear", record=True) is False
    assert chunk_applies(host, turn2, " after-clear", record=True) is False
    assert turn.emitted == "Hello"
    assert turn2.emitted == "Second"
    assert host.session.messages == []


def test_stop_keeps_emitted_bytes_instead_of_no_response():
    """Stop bumps the generation and must not replace streamed text."""
    from plugin.chatbot.tool_loop_actions import (
        begin_send_turn,
        bump_send_generation,
        chunk_applies,
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
    assert chunk_applies(host, turn, "Hello", record=True)
    bump_send_generation(host)
    host._apply_turn = turn
    assert stopped_assistant_text(host, None) == "Hello"
    assert stopped_assistant_text(host, "No response.") == "Hello"
    persist_assistant_on_turn(host, content=stopped_assistant_text(host, "No response."))
    assert any(message.get("content") == "Hello" for message in host.session.messages)
    assert chunk_applies(host, turn, " more", record=True) is False
    assert turn.emitted == "Hello"
