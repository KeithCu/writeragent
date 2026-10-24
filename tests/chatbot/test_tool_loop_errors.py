import pytest
import json
import queue
import sys
from typing import Any
from unittest.mock import MagicMock, patch

from plugin.framework.stream_batch import StreamQueueKind
from plugin.chatbot.tool_loop_actions import build_tool_execute_fn, current_turn, take_stop_partial
from plugin.chatbot.tool_loop import ToolCallingMixin
from plugin.chatbot.tool_loop_state import ToolLoopState
from plugin.chatbot.audio_recorder_state import AudioRecorderState
from plugin.chatbot.send_state import SendButtonState
from plugin.chatbot.sidebar_state import SidebarCompositeState

class MockSession:
    def __init__(self):
        self.messages = [{"role": "system", "content": "test"}]
        self.document_context = ""
        self.db = None

    def set_system_context(self, base_prompt, doc_text=""):
        self.document_context = doc_text
        self.messages[0]["content"] = f"{base_prompt}\n\n[DOCUMENT CONTENT]\n{doc_text}\n[END DOCUMENT]"

    def refresh_document_context(self, model, ctx):
        self.set_system_context("base", "doc text")

    def add_user_message(self, text):
        self.messages.append({"role": "user", "content": text})

    def add_assistant_message(self, content=None, tool_calls=None, reasoning_replay=None):
        pass

class MockDummyToolCallingClass(ToolCallingMixin):
    def __init__(self):
        self._tool_calling = self  # fake panel plays the composed ToolCallingMixin role
        self.ctx = MagicMock()
        self.session = MockSession()
        self.sidebar_state = SidebarCompositeState(
            send=SendButtonState(False, False, False, False, False),
            tool_loop=None,
            audio=AudioRecorderState(status="idle"),
        )
        self.model_selector = None
        self.image_model_selector = None
        self.client = MagicMock()
        self.audio_wav_path = None
        self.stop_requested = False
        self._get_document_model = MagicMock(return_value=None)
        self.responses = []
        self.statuses = []
        self._terminal_status = None

    def resolve_stop_checker(self):
        return lambda: self.stop_requested

    def _append_response(self, text, is_thinking=False, role="assistant"):
        self.responses.append(text)

    def _set_status(self, text):
        self.statuses.append(text)

@pytest.fixture
def mock_get_tools(monkeypatch):
    # Stand in a mock plugin.main so we can patch plugin.main.get_tools.
    # monkeypatch.setitem restores (or removes) the sys.modules entry on teardown.
    class MockMain:
        pass
    monkeypatch.setitem(sys.modules, "plugin.main", MockMain())

    with patch("plugin.main.get_tools", create=True) as mock_gt:
        registry = MagicMock()
        mock_gt.return_value = registry
        yield registry

@pytest.fixture
def test_instance():
    instance = MockDummyToolCallingClass()

    # Mock some configs used in the main logic to avoid full system dependency
    with patch("plugin.chatbot.tool_loop.get_config") as mock_get_config, \
         patch("plugin.chatbot.tool_loop.get_api_config") as mock_get_api_config, \
         patch("plugin.chatbot.tool_loop.validate_api_config") as mock_validate_api_config:

        mock_get_config.side_effect = lambda key: "10" if "tokens" in key or "context" in key else "test"
        mock_get_api_config.return_value = {"chat_max_tool_rounds": 1}
        mock_validate_api_config.return_value = (True, "")

        from plugin.chatbot.tool_loop_actions import begin_send_turn

        begin_send_turn(instance, "chat", model=MagicMock())
        yield instance

def test_tool_execution_error_handling(test_instance, mock_get_tools):
    # Setup mock to simulate a tool returning error dicts when execute is called
    registry = mock_get_tools
    registry.get_schemas.return_value = [{"name": "test_tool"}]
    execute_fn = build_tool_execute_fn(test_instance, "writer", None, None, MagicMock())

    with patch("plugin.chatbot.tool_loop_actions.agent_log") as mock_agent_log:
        # Test 1: Tool execution error payload
        err_payload = {"status": "error", "code": "TOOL_EXECUTION_ERROR", "message": "Specific tool error"}
        registry.execute.return_value = err_payload

        res = execute_fn("test_tool", {"arg": "val"}, None, test_instance.ctx)

        parsed_res = json.loads(res)
        assert parsed_res == err_payload
        mock_agent_log.assert_not_called()

        # Test 2: Registry error payload
        reg_payload = {
            "status": "error",
            "code": "TOOL_REGISTRY_ERROR",
            "message": "Failed to execute tool 'test_tool'",
            "details": {"tool_name": "test_tool", "error": "Something unexpected", "type": "ValueError"},
        }
        registry.execute.return_value = reg_payload

        res = execute_fn("test_tool", {"arg": "val"}, None, test_instance.ctx)
        parsed_res = json.loads(res)

        assert parsed_res["status"] == "error"
        assert parsed_res["code"] == "TOOL_REGISTRY_ERROR"
        assert parsed_res["code"] != "TOOL_UNEXPECTED_ERROR"
        assert parsed_res["details"]["tool_name"] == "test_tool"
        mock_agent_log.assert_not_called()

def test_document_context_error_handling(test_instance, mock_get_tools):
    """Send fails closed when refreshing the live document snapshot fails.

    tool_loop calls ChatSession.refresh_document_context (it does not import
    get_document_context_for_chat). UnoObjectError and other failures both
    set the terminal status; disposal is reported as closed, anything else
    as a document-context error.
    """
    from plugin.framework.errors import UnoObjectError

    mock_get_tools.get_schemas.return_value = [{"name": "test_tool"}]

    with patch.object(test_instance.session, "refresh_document_context") as mock_refresh:
        mock_refresh.side_effect = UnoObjectError("Document dead")

        test_instance._do_send_chat_with_tools("test", "test_model", "writer")

        assert test_instance._terminal_status == "Error"
        assert any("[Document closed or unavailable.]" in r for r in test_instance.responses)

        test_instance.responses.clear()
        test_instance._terminal_status = None
        mock_refresh.side_effect = RuntimeError("Something bad")

        test_instance._do_send_chat_with_tools("test", "test_model", "writer")

        assert test_instance._terminal_status == "Error"
        assert any("[Error reading document: Something bad]" in r for r in test_instance.responses)

        class MockRuntimeException(Exception):
            pass
        MockRuntimeException.__name__ = "RuntimeException"

        class MockDisposedException(Exception):
            pass
        MockDisposedException.__name__ = "DisposedException"

        class LiveDoc:
            def getImplementationName(self):
                return "SwXTextDocument"

        class DeadDoc:
            def getImplementationName(self):
                raise MockDisposedException("doc gone")

        # RuntimeException against live doc -> reading-error, not closed
        test_instance.responses.clear()
        test_instance._terminal_status = None
        mock_refresh.side_effect = MockRuntimeException("bridge broke")
        test_instance._do_send_chat_with_tools("test", LiveDoc(), "writer")

        assert test_instance._terminal_status == "Error"
        assert any("[Error reading document: bridge broke]" in r for r in test_instance.responses)
        assert not any("[Document closed or unavailable.]" in r for r in test_instance.responses)

        # RuntimeException against dead doc -> closed
        test_instance.responses.clear()
        test_instance._terminal_status = None
        mock_refresh.side_effect = MockRuntimeException("bridge broke")
        test_instance._do_send_chat_with_tools("test", DeadDoc(), "writer")

        assert test_instance._terminal_status == "Error"
        assert any("[Document closed or unavailable.]" in r for r in test_instance.responses)

def test_audio_handling_error(test_instance, mock_get_tools):
    mock_get_tools.get_schemas.return_value = [{"name": "test_tool"}]

    with patch("plugin.chatbot.tool_loop.agent_log"):

        from plugin.chatbot.tool_loop_actions import begin_send_turn

        begin_send_turn(test_instance, "chat")
        test_instance.audio_wav_path = "/fake/path/audio.wav"

        # Override open to throw IOError
        with patch("builtins.open", side_effect=IOError("Disk full")):
            test_instance._do_send_chat_with_tools("test", "test_model", "writer")

            # The error shouldn't crash the loop
            assert test_instance.audio_wav_path is None
            assert any("test" in r for r in test_instance.responses)
            assert test_instance._terminal_status != "Error" # Should not terminate on audio error

        # The first send's drain aborted its turn. This call is a new send.
        begin_send_turn(test_instance, "chat")
        test_instance.audio_wav_path = "/fake/path/audio.wav"
        with patch("builtins.open", side_effect=TypeError("Bad arguments")):
            test_instance._do_send_chat_with_tools("test", "test_model", "writer")

            # The error shouldn't crash the loop
            assert test_instance.audio_wav_path is None
            assert any("test" in r for r in test_instance.responses)
            assert test_instance._terminal_status != "Error"


def test_stream_error_stt_fallback_does_not_reenter_send(test_instance):
    import dataclasses

    from plugin.chatbot.tool_loop_actions import current_turn

    test_instance.audio_wav_path = "/fake/path/audio.wav"
    live = current_turn(test_instance)
    live.active_query_text = "hello"
    live.queue = MagicMock()
    live.batcher = None
    live.active_client = MagicMock()
    live.active_max_tokens = 128
    live.active_tools = []
    live.model = MagicMock()
    test_instance.session.messages.append({"role": "user", "content": [{"type": "input_audio"}]})
    test_instance.sidebar_state = dataclasses.replace(
        test_instance.sidebar_state,
        tool_loop=ToolLoopState(round_num=0, pending_tools=[], max_rounds=8, status="Thinking..."),
    )
    test_instance._spawn_llm_worker = MagicMock()
    test_instance._do_send_chat_with_tools = MagicMock()
    test_instance._start_tool_calling_async = MagicMock()
    test_instance._transcribe_audio = MagicMock(return_value="spoken words")

    with (
        patch("plugin.framework.client.model_fetcher.get_text_model", return_value="chat-model"),
        patch("plugin.framework.config.get_current_endpoint", return_value="https://example"),
        patch("plugin.framework.client.model_fetcher.get_stt_model", return_value="stt-model"),
        patch("plugin.framework.client.model_fetcher.set_native_audio_support") as mock_cache,
        patch("plugin.chatbot.tool_loop.os.remove") as mock_remove,
    ):
        recovered = test_instance._handle_stream_error("unsupported modality: audio")

    assert recovered is True
    test_instance._transcribe_audio.assert_called_once_with("/fake/path/audio.wav", "stt-model")
    test_instance._spawn_llm_worker.assert_called_once()
    test_instance._do_send_chat_with_tools.assert_not_called()
    test_instance._start_tool_calling_async.assert_not_called()
    mock_cache.assert_called_once_with("chat-model", "https://example", supported=False)
    mock_remove.assert_called_once_with("/fake/path/audio.wav")
    assert test_instance.audio_wav_path is None
    assert test_instance.session.messages[-1]["role"] == "user"
    assert test_instance.session.messages[-1]["content"] == "hello\nspoken words"
    assert test_instance._spawn_llm_worker.call_args.kwargs["query_text"] == "hello\nspoken words"


def test_stream_error_empty_stt_ends_the_drain(test_instance):
    """Empty speech shows the banner and returns None so the drain stops.

    True would keep the drain waiting for a worker that was never spawned.
    """
    import dataclasses

    from plugin.chatbot.tool_loop_actions import current_turn

    test_instance.audio_wav_path = "/fake/path/audio.wav"
    live = current_turn(test_instance)
    live.active_query_text = ""
    live.queue = MagicMock()
    live.batcher = None
    live.active_client = MagicMock()
    live.active_max_tokens = 128
    live.active_tools = []
    test_instance._spawn_llm_worker = MagicMock()
    test_instance._transcribe_audio = MagicMock(return_value="   ")
    test_instance.sidebar_state = dataclasses.replace(
        test_instance.sidebar_state,
        tool_loop=ToolLoopState(round_num=0, pending_tools=[], max_rounds=8, status="Thinking..."),
    )

    with (
        patch("plugin.framework.client.model_fetcher.get_text_model", return_value="chat-model"),
        patch("plugin.framework.config.get_current_endpoint", return_value="https://example"),
        patch("plugin.framework.client.model_fetcher.get_stt_model", return_value="stt-model"),
        patch("plugin.framework.client.model_fetcher.set_native_audio_support"),
        patch("plugin.chatbot.tool_loop.os.remove"),
    ):
        recovered = test_instance._handle_stream_error("unsupported modality: audio")

    assert recovered is None
    test_instance._spawn_llm_worker.assert_not_called()
    assert test_instance.audio_wav_path is None
    assert any("No speech detected" in text for text in test_instance.responses)
    assert test_instance._terminal_status == ""


def test_reused_llm_client_registers_on_current_send_scope(test_instance, mock_get_tools):
    """Packet B2: Stop on send 2+ must close HTTP on the reused LlmClient."""
    from plugin.framework.send_cancellation import SendCancellation, agent_session

    mock_get_tools.get_schemas.return_value = []
    existing = test_instance.client
    existing.stop = MagicMock()
    scope = SendCancellation()
    test_instance._start_tool_calling_async = MagicMock()

    with (
        patch("plugin.chatbot.tool_loop.sync_sidebar_text_model", return_value=None),
        patch("plugin.chatbot.tool_loop.get_config_int", return_value=128),
        patch("plugin.chatbot.tool_loop.get_toolkit", return_value=None),
        patch("plugin.chatbot.tool_loop.LlmClient") as mock_llm_cls,
        patch("plugin.framework.client.model_fetcher.has_native_vision", return_value=False),
        agent_session(scope),
    ):
        test_instance._do_send_chat_with_tools("hello", MagicMock(), "writer")

    mock_llm_cls.assert_not_called()
    assert test_instance.client is existing
    scope.cancel()
    existing.stop.assert_called()


def test_handle_stream_error_payload_dict(test_instance):
    """Drain ERROR items are format_error_payload dicts, not Exception."""
    payload = {
        "status": "error",
        "code": "HTTP_ERROR",
        "message": "HTTP Error 500 from AI Provider: Internal Server Error. mock LLM soak failure",
    }
    recovered = test_instance._handle_stream_error(payload)
    assert recovered is None
    joined = "".join(test_instance.responses)
    assert "[API error:" in joined
    assert "500" in joined
    assert "mock LLM soak failure" in joined
    assert test_instance._terminal_status == "Error"


def test_handle_stream_error_payload_dict_missing_message(test_instance):
    payload = {"status": "error", "code": "HTTP_ERROR"}
    recovered = test_instance._handle_stream_error(payload)
    assert recovered is None
    joined = "".join(test_instance.responses)
    assert "[API error:" in joined
    assert "HTTP_ERROR" in joined
    assert test_instance._terminal_status == "Error"


def test_handle_stream_error_llama_overflow_plain_sentence(test_instance):
    """Issue #570: do not dump the HTTP 500 payload dict into the sidebar."""
    from plugin.framework.client.errors import local_model_overflow_message

    payload = {
        "status": "error",
        "code": "HTTP_ERROR",
        "message": (
            "HTTP Error 500 from AI Provider: Internal Server Error. "
            "llama-server process has terminated: exit status 0xc0000005"
        ),
        "details": {"url": "/v1/chat/completions", "status": 500},
    }
    recovered = test_instance._handle_stream_error(payload)
    assert recovered is None
    joined = "".join(test_instance.responses)
    assert local_model_overflow_message() in joined
    assert "[API error:" not in joined
    assert "{'status'" not in joined
    assert "0xc0000005" not in joined
    assert test_instance._terminal_status == "Error"


def test_handle_stream_error_keeps_named_window_sentence(test_instance):
    named = (
        "The local Ollama/llama.cpp process crashed because the prompt "
        "overflowed a 4K context window."
    )
    payload = {"status": "error", "code": "HTTP_ERROR", "message": named}
    test_instance._handle_stream_error(payload)
    joined = "".join(test_instance.responses)
    assert named in joined
    assert "[API error:" not in joined


def _prime_active_tool_loop(instance):
    import dataclasses

    from plugin.chatbot.tool_loop_actions import current_turn

    live = current_turn(instance)
    live.queue = MagicMock()
    live.batcher = None
    live._overflow_compact_attempts = 0
    live._last_compact_reason = None
    live._last_compact_tokens_before = None
    live._last_compact_tokens_after = None
    live.active_client = MagicMock()
    live.active_max_tokens = 128
    live.active_tools = []
    live.active_query_text = "hello"
    instance.sidebar_state = dataclasses.replace(
        instance.sidebar_state,
        tool_loop=ToolLoopState(round_num=0, pending_tools=[], max_rounds=8, status="Thinking..."),
    )


def _overflow_payload(message="prompt is too long"):
    return {"status": "error", "code": "HTTP_ERROR", "message": message}


def _handle_stream_error(instance, payload, compaction_enabled=True):
    with patch("plugin.chatbot.tool_loop.get_config_bool_safe", return_value=compaction_enabled):
        return instance._handle_stream_error(payload)


def test_overflow_respawns_worker_with_force_compact(test_instance):
    """Prompt-too-large respawns the worker; drain thread never calls compact_session."""
    _prime_active_tool_loop(test_instance)
    with (
        patch("plugin.chatbot.tool_loop.run_in_background") as mock_bg,
        patch("plugin.chatbot.tool_loop.compact_session") as mock_compact,
    ):
        recovered = _handle_stream_error(test_instance, _overflow_payload())

    assert recovered is True
    mock_bg.assert_called_once()
    mock_compact.assert_not_called()
    from plugin.chatbot.tool_loop_actions import current_turn
    assert current_turn(test_instance)._overflow_compact_attempts == 1
    assert "Compacting conversation..." in test_instance.statuses
    assert not any(str(s).startswith("Thinking") for s in test_instance.statuses)
    assert test_instance._terminal_status is None
    joined = "".join(test_instance.responses)
    assert "[API error:" not in joined
    from plugin.framework.client.errors import local_model_overflow_message

    assert local_model_overflow_message() not in joined


def test_overflow_attempt_3_falls_through(test_instance):
    _prime_active_tool_loop(test_instance)
    from plugin.chatbot.tool_loop_actions import current_turn
    current_turn(test_instance)._overflow_compact_attempts = 3
    test_instance._spawn_llm_worker = MagicMock()

    recovered = _handle_stream_error(test_instance, _overflow_payload())

    assert recovered is not True
    test_instance._spawn_llm_worker.assert_not_called()
    assert "[API error:" in "".join(test_instance.responses)
    assert test_instance._terminal_status == "Error"


def test_process_death_does_not_retry_overflow(test_instance):
    _prime_active_tool_loop(test_instance)
    test_instance._spawn_llm_worker = MagicMock()
    payload = {
        "status": "error",
        "code": "HTTP_ERROR",
        "message": (
            "HTTP Error 500 from AI Provider: Internal Server Error. "
            "llama-server process has terminated: exit status 0xc0000005"
        ),
    }

    recovered = _handle_stream_error(test_instance, payload)

    assert recovered is not True
    test_instance._spawn_llm_worker.assert_not_called()
    from plugin.framework.client.errors import local_model_overflow_message

    assert local_model_overflow_message() in "".join(test_instance.responses)
    assert test_instance._terminal_status == "Error"


@pytest.mark.parametrize(
    "reason,tokens_before,tokens_after",
    [
        ("nothing_to_compact", 1000, 1000),
        ("no_window", 1000, 1000),
        ("failed", 1000, 900),
        ("aborted", 1000, 900),
        ("disabled", 1000, 900),
        ("ok", 1000, 950),  # exact 5% is not a shrink
        ("ok", 1000, 960),
        ("ok", 1000, 951),
    ],
)
def test_overflow_does_not_retry_after_failed_or_tiny_shrink(
    test_instance, reason, tokens_before, tokens_after
):
    _prime_active_tool_loop(test_instance)
    from plugin.chatbot.tool_loop_actions import current_turn
    current_turn(test_instance)._last_compact_reason = reason
    current_turn(test_instance)._last_compact_tokens_before = tokens_before
    current_turn(test_instance)._last_compact_tokens_after = tokens_after
    test_instance._spawn_llm_worker = MagicMock()

    recovered = _handle_stream_error(test_instance, _overflow_payload())

    assert recovered is not True
    test_instance._spawn_llm_worker.assert_not_called()
    assert test_instance._terminal_status == "Error"


def test_compaction_flag_false_does_not_respawn(test_instance):
    _prime_active_tool_loop(test_instance)
    test_instance._spawn_llm_worker = MagicMock()

    recovered = _handle_stream_error(
        test_instance,
        _overflow_payload(
            "HTTP Error 500 from AI Provider: Internal Server Error. "
            "truncating input prompt"
        ),
        compaction_enabled=False,
    )

    assert recovered is not True
    test_instance._spawn_llm_worker.assert_not_called()
    from plugin.framework.client.errors import local_model_overflow_message

    assert local_model_overflow_message() in "".join(test_instance.responses)
    assert test_instance._terminal_status == "Error"


def test_force_compact_skips_host_thinking_status(test_instance):
    with patch("plugin.chatbot.tool_loop.run_in_background"):
        test_instance._spawn_llm_worker(
            MagicMock(), MagicMock(), 128, [], 0, force_compact=True
        )
    assert not any(str(s).startswith("Thinking") for s in test_instance.statuses)

    test_instance.statuses.clear()
    with patch("plugin.chatbot.tool_loop.run_in_background"):
        test_instance._spawn_llm_worker(
            MagicMock(), MagicMock(), 128, [], 0, force_compact=False
        )
    assert any(str(s).startswith("Thinking") for s in test_instance.statuses)


def test_llm_worker_run_never_calls_set_status(test_instance):
    from plugin.chatbot.compaction import CompactResult
    from plugin.framework.stream_batch import StreamQueueKind

    captured = {}

    def capture_run(fn, name=None, dedicated=False):
        captured["fn"] = fn

    client = MagicMock()
    client._stopped = False
    client.stream_request_with_tools.return_value = {"content": "ok"}
    view = [{"role": "system", "content": "view"}]
    q = MagicMock()
    from plugin.chatbot.tool_loop_actions import current_turn

    current_turn(test_instance).queue = q
    compact_inside_lane = {"value": False}

    class _Lane:
        def __init__(self, *args, **kwargs):
            del args, kwargs

        def __enter__(self):
            compact_inside_lane["in"] = True
            return self

        def __exit__(self, *exc):
            compact_inside_lane["in"] = False
            return False

    def fake_compact(*args, **kwargs):
        del args, kwargs
        compact_inside_lane["value"] = compact_inside_lane.get("in", False)
        return CompactResult(False, "below_threshold", 10, 10)

    with (
        patch("plugin.chatbot.tool_loop.run_in_background", side_effect=capture_run),
        patch("plugin.chatbot.tool_loop.llm_request_lane", side_effect=_Lane),
        patch("plugin.chatbot.tool_loop.get_config_bool_safe", return_value=True),
        patch("plugin.chatbot.tool_loop.compact_session", side_effect=fake_compact) as mock_compact,
        patch("plugin.chatbot.tool_loop.resolve_context_window", return_value=8192),
        patch("plugin.chatbot.tool_loop.messages_for_llm", return_value=view),
    ):
        test_instance._spawn_llm_worker(q, client, 128, [{"name": "t"}], 0)
        # Host Thinking is expected on the caller; the background run must not UNO.
        test_instance._set_status = MagicMock()
        captured["fn"]()

    test_instance._set_status.assert_not_called()
    mock_compact.assert_called_once()
    assert mock_compact.call_args.kwargs["force"] is False
    assert mock_compact.call_args.kwargs["max_tokens"] == 128
    assert mock_compact.call_args.kwargs["tools"] == [{"name": "t"}]
    assert compact_inside_lane["value"] is True
    client.stream_request_with_tools.assert_called_once()
    assert client.stream_request_with_tools.call_args[0][0] is view
    q.put.assert_any_call((StreamQueueKind.STREAM_DONE, {"content": "ok"}))


def test_llm_worker_run_force_compact_and_aborted_stops(test_instance):
    from plugin.chatbot.compaction import CompactResult
    from plugin.framework.stream_batch import StreamQueueKind

    captured = {}

    def capture_run(fn, name=None, dedicated=False):
        captured["fn"] = fn

    client = MagicMock()
    # MagicMock._stopped is truthy. The worker treats that as Stop and
    # returns before compact, so the aborted-compact path never runs.
    client._stopped = False
    q = MagicMock()
    from plugin.chatbot.tool_loop_actions import current_turn

    current_turn(test_instance).queue = q
    with (
        patch("plugin.chatbot.tool_loop.run_in_background", side_effect=capture_run),
        patch("plugin.chatbot.tool_loop.llm_request_lane") as mock_lane,
        patch("plugin.chatbot.tool_loop.get_config_bool_safe", return_value=True),
        patch(
            "plugin.chatbot.tool_loop.compact_session",
            return_value=CompactResult(False, "aborted", 10, 10),
        ) as mock_compact,
        patch("plugin.chatbot.tool_loop.resolve_context_window", return_value=8192),
        patch("plugin.chatbot.tool_loop.messages_for_llm", return_value=[]),
    ):
        mock_lane.return_value.__enter__ = MagicMock()
        mock_lane.return_value.__exit__ = MagicMock(return_value=False)
        test_instance._spawn_llm_worker(q, client, 128, [], 1, force_compact=True)
        assert not any(str(s).startswith("Thinking") for s in test_instance.statuses)
        test_instance._set_status = MagicMock()
        captured["fn"]()

    test_instance._set_status.assert_not_called()
    assert mock_compact.call_args.kwargs["force"] is True
    client.stream_request_with_tools.assert_not_called()
    q.put.assert_called_with((StreamQueueKind.STOPPED,))


def test_llm_worker_skips_compact_when_flag_false(test_instance):
    captured = {}

    def capture_run(fn, name=None, dedicated=False):
        captured["fn"] = fn

    client = MagicMock()
    client._stopped = False
    client.stream_request_with_tools.return_value = {}
    view = [{"role": "user", "content": "hi"}]
    with (
        patch("plugin.chatbot.tool_loop.run_in_background", side_effect=capture_run),
        patch("plugin.chatbot.tool_loop.llm_request_lane") as mock_lane,
        patch("plugin.chatbot.tool_loop.get_config_bool_safe", return_value=False),
        patch("plugin.chatbot.tool_loop.compact_session") as mock_compact,
        patch("plugin.chatbot.tool_loop.messages_for_llm", return_value=view),
    ):
        mock_lane.return_value.__enter__ = MagicMock()
        mock_lane.return_value.__exit__ = MagicMock(return_value=False)
        test_instance._spawn_llm_worker(MagicMock(), client, 64, [], 0)
        captured["fn"]()

    mock_compact.assert_not_called()
    assert client.stream_request_with_tools.call_args[0][0] is view


def test_final_stream_compacts_then_sends_view(test_instance):
    from plugin.chatbot.compaction import CompactResult

    captured = {}

    def capture_run(fn, name=None, dedicated=False):
        captured["fn"] = fn

    client = MagicMock()
    client._stopped = False
    view = [{"role": "system", "content": "final-view"}]
    with (
        patch("plugin.chatbot.tool_loop.run_in_background", side_effect=capture_run),
        patch("plugin.chatbot.tool_loop.llm_request_lane") as mock_lane,
        patch("plugin.chatbot.tool_loop.get_config_bool_safe", return_value=True),
        patch(
            "plugin.chatbot.tool_loop.compact_session",
            return_value=CompactResult(False, "below_threshold", 4, 4),
        ) as mock_compact,
        patch("plugin.chatbot.tool_loop.resolve_context_window", return_value=8192),
        patch("plugin.chatbot.tool_loop.messages_for_llm", return_value=view),
    ):
        mock_lane.return_value.__enter__ = MagicMock()
        mock_lane.return_value.__exit__ = MagicMock(return_value=False)
        test_instance._spawn_final_stream(MagicMock(), client, 256)
        test_instance._set_status = MagicMock()
        captured["fn"]()

    test_instance._set_status.assert_not_called()
    mock_compact.assert_called_once()
    assert mock_compact.call_args.kwargs["force"] is False
    assert mock_compact.call_args.kwargs["max_tokens"] == 256
    client.stream_chat_response.assert_called_once()
    assert client.stream_chat_response.call_args[0][0] is view


def test_final_stream_done_and_stop_partial(test_instance):
    captured = {}

    def capture_run(fn, name=None, dedicated=False):
        captured["fn"] = fn

    client = MagicMock()
    client._stopped = False
    view = [{"role": "system", "content": "final-view"}]

    def fake_stream(messages, max_tokens, append_c, append_t, stop_checker=None, status_callback=None):
        append_c("ab")
        append_c("c")

    client.stream_chat_response.side_effect = fake_stream

    turn = current_turn(test_instance)
    assert turn is not None
    q1: queue.Queue[Any] = queue.Queue()
    turn.queue = q1
    turn.batcher = None

    with (
        patch("plugin.chatbot.tool_loop.run_in_background", side_effect=capture_run),
        patch("plugin.chatbot.tool_loop.llm_request_lane") as mock_lane,
        patch("plugin.chatbot.tool_loop.get_config_bool_safe", return_value=False),
        patch("plugin.chatbot.tool_loop.messages_for_llm", return_value=view),
    ):
        mock_lane.return_value.__enter__ = MagicMock()
        mock_lane.return_value.__exit__ = MagicMock(return_value=False)
        test_instance._spawn_final_stream(q1, client, 256)
        original_set_status = test_instance._set_status
        mock_set_status = MagicMock()
        test_instance._set_status = mock_set_status
        captured["fn"]()
        test_instance._set_status = original_set_status

    mock_set_status.assert_not_called()
    assert "\nAI: " in test_instance.responses
    assert "Finishing..." in test_instance.statuses
    client.stream_request_with_tools.assert_not_called()

    items1: list[Any] = []
    while not q1.empty():
        items1.append(q1.get_nowait())

    assert (StreamQueueKind.CHUNK, "ab") in items1
    assert (StreamQueueKind.CHUNK, "c") in items1
    assert items1[-1] == (StreamQueueKind.FINAL_DONE, "abc")
    assert not any(item[0] == StreamQueueKind.STREAM_DONE for item in items1)

    # 2. Second run on a fresh queue: aborted mid-stream
    q2: queue.Queue[Any] = queue.Queue()
    turn.queue = q2
    turn.batcher = None
    client._stopped = False

    def fake_stream_abort(messages, max_tokens, append_c, append_t, stop_checker=None, status_callback=None):
        append_c("ab")
        client._stopped = True

    client.stream_chat_response.side_effect = fake_stream_abort

    with (
        patch("plugin.chatbot.tool_loop.run_in_background", side_effect=capture_run),
        patch("plugin.chatbot.tool_loop.llm_request_lane") as mock_lane,
        patch("plugin.chatbot.tool_loop.get_config_bool_safe", return_value=False),
        patch("plugin.chatbot.tool_loop.messages_for_llm", return_value=view),
    ):
        mock_lane.return_value.__enter__ = MagicMock()
        mock_lane.return_value.__exit__ = MagicMock(return_value=False)
        test_instance._spawn_final_stream(q2, client, 256)
        captured["fn"]()

    items2: list[Any] = []
    while not q2.empty():
        items2.append(q2.get_nowait())

    assert items2[-1] == (StreamQueueKind.STOPPED,)
    assert not any(item[0] == StreamQueueKind.FINAL_DONE for item in items2)
    assert take_stop_partial(current_turn(test_instance)) == "ab"

    # 3. Stop pressed before the final stream starts: no lane, no request.
    q3: queue.Queue[Any] = queue.Queue()
    turn.queue = q3
    turn.batcher = None
    client._stopped = True
    client.stream_chat_response.reset_mock()

    with (
        patch("plugin.chatbot.tool_loop.run_in_background", side_effect=capture_run),
        patch("plugin.chatbot.tool_loop.llm_request_lane") as mock_lane,
        patch("plugin.chatbot.tool_loop.get_config_bool_safe", return_value=False),
        patch("plugin.chatbot.tool_loop.messages_for_llm", return_value=view),
    ):
        test_instance._spawn_final_stream(q3, client, 256)
        captured["fn"]()
        mock_lane.assert_not_called()

    client.stream_chat_response.assert_not_called()
    assert q3.get_nowait() == (StreamQueueKind.STOPPED,)


def test_start_tool_calling_resets_overflow_attempts(test_instance):
    test_instance._spawn_llm_worker = MagicMock()
    test_instance._refresh_active_tools_for_session = MagicMock()

    def execute_fn(name, args, call_id, ctx):
        del name, args, call_id, ctx
        return "{}"

    with (
        patch("plugin.chatbot.tool_loop.get_config_int", return_value=8),
        patch("plugin.chatbot.tool_loop.get_config", return_value=False),
        patch("plugin.chatbot.tool_loop.get_toolkit", return_value=MagicMock()),
        patch("plugin.chatbot.tool_loop.run_stream_drain_loop"),
        patch("plugin.chatbot.tool_loop_actions.finalize_sidebar_assistant_response"),
    ):
        from plugin.chatbot.tool_loop_actions import begin_send_turn
        begin_send_turn(test_instance, "chat")
        test_instance._start_tool_calling_async(
            MagicMock(), MagicMock(), 128, [], execute_fn
        )
    from plugin.chatbot.tool_loop_actions import current_turn
    assert current_turn(test_instance)._overflow_compact_attempts == 0


def test_overflow_does_not_retry_when_stop_requested(test_instance):
    _prime_active_tool_loop(test_instance)
    test_instance.stop_requested = True
    test_instance._spawn_llm_worker = MagicMock()

    recovered = _handle_stream_error(test_instance, _overflow_payload())

    assert recovered is not True
    test_instance._spawn_llm_worker.assert_not_called()
    assert test_instance._terminal_status == "Error"


def test_overflow_does_not_retry_when_stop_checker_active(test_instance):
    _prime_active_tool_loop(test_instance)
    test_instance.resolve_stop_checker = MagicMock(return_value=lambda: True)
    test_instance._spawn_llm_worker = MagicMock()

    recovered = _handle_stream_error(test_instance, _overflow_payload())

    assert recovered is not True
    test_instance._spawn_llm_worker.assert_not_called()
    assert test_instance._terminal_status == "Error"



def test_stream_error_on_dead_turn_still_deletes_wav(test_instance, tmp_path):
    from plugin.chatbot.tool_loop_actions import current_turn

    wav = tmp_path / "rec.wav"
    wav.write_bytes(b"RIFF")
    test_instance.audio_wav_path = str(wav)
    current_turn(test_instance).abort()

    assert _handle_stream_error(test_instance, _overflow_payload("boom")) is None
    assert test_instance.audio_wav_path is None
    assert not wav.exists()


def test_stream_error_cleans_failed_image_from_history(test_instance):
    """When an image request fails with image-unsupported error, the image is stripped from history."""
    test_instance.session.messages.append({
        "role": "user",
        "content": [
            {"type": "text", "text": "describe the image"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
        ],
    })
    test_instance.session.db = MagicMock()

    with (
        patch("plugin.framework.client.model_fetcher.get_text_model", return_value="gpt-oss-20b:nitro"),
        patch("plugin.framework.config.get_current_endpoint", return_value="https://openrouter.ai/api"),
        patch("plugin.framework.client.model_fetcher.set_native_vision_support") as mock_set_vision,
    ):
        payload = {"message": "No endpoints found that support image input"}
        recovered = test_instance._handle_stream_error(payload)

    assert recovered is None
    mock_set_vision.assert_called_once_with("gpt-oss-20b:nitro", "https://openrouter.ai/api", supported=False)
    # The image block was stripped from user message content in session
    user_msgs = [m for m in test_instance.session.messages if m.get("role") == "user"]
    assert user_msgs[-1]["content"] == "describe the image"
    assert not any(isinstance(m.get("content"), list) for m in test_instance.session.messages)
    # And persisted DB content was updated
    test_instance.session.db.update_last_user_content.assert_called_once_with("describe the image")


def test_text_only_model_does_not_attach_image(test_instance, mock_get_tools):
    """A text-only model like gpt-oss-20b:nitro does not attach selected image."""
    test_instance._get_document_model = MagicMock()
    mock_get_tools.get_schemas.return_value = []
    test_instance._start_tool_calling_async = MagicMock()

    api_config = {
        "endpoint": "https://openrouter.ai/api",
        "model": "gpt-oss-20b:nitro",
        "api_key": "test-key",
        "chat_max_tool_rounds": 1,
    }
    with (
        patch("plugin.chatbot.tool_loop.get_api_config", return_value=api_config),
        patch("plugin.chatbot.tool_loop.validate_api_config", return_value=(True, "")),
        patch("plugin.chatbot.tool_loop.sync_sidebar_text_model", return_value=None),
        patch("plugin.writer.images.image_tools.get_selected_image_base64") as mock_get_img,
    ):
        test_instance._do_send_chat_with_tools("describe the image", MagicMock(), "writer")

    # Image should not be extracted or attached because model lacks vision
    mock_get_img.assert_not_called()
    user_msgs = [m for m in test_instance.session.messages if m.get("role") == "user"]
    assert user_msgs[-1]["content"] == "describe the image"


def test_native_audio_stt_fallback_replaces_db_row():
    """Fallback STT must replace the audio message row with the transcript in the DB."""
    from plugin.chatbot.tool_loop import try_native_audio_stt_fallback

    host = MagicMock()
    host.audio_wav_path = "/fake/a.wav"
    host._terminal_status = "Ready"
    host._turn.alive = True
    host._turn.batcher = None
    host._turn.queue = MagicMock()
    host._turn.text_model = "chat-model"
    host._turn.endpoint = "https://example"
    host._turn.active_query_text = "typed query"
    host._turn.active_client = MagicMock()
    host._turn.active_max_tokens = 100
    host._turn.active_tools = []

    # Session has the audio-included message in memory
    audio_msg = {"role": "user", "content": [{"type": "text", "text": "typed query"}, {"type": "input_audio"}]}
    host.session.messages = [audio_msg]

    db_rows = [{"role": "system", "content": "hello"}, {"role": "user", "content": "[Voice message]"}]
    host.session.db = MagicMock()
    host.session.db.get_messages.return_value = db_rows

    def _stopped(_path, _model):
        return "spoken words"

    host._transcribe_audio.side_effect = _stopped
    with (
        patch("plugin.framework.client.model_fetcher.get_text_model", return_value="chat-model"),
        patch("plugin.framework.config.get_current_endpoint", return_value="https://example"),
        patch("plugin.framework.client.model_fetcher.get_stt_model", return_value="stt-model"),
        patch("plugin.framework.client.model_fetcher.set_native_audio_support") as mock_set_support,
        patch("plugin.audio.stt_service.uses_local_stt", return_value=False),
        patch("plugin.chatbot.tool_loop.os.remove"),
    ):
        recovered = try_native_audio_stt_fallback(host, "unsupported modality: audio")

    assert recovered is True
    mock_set_support.assert_called_once_with("chat-model", "https://example", supported=False)
    assert len(host.session.messages) == 1
    assert host.session.messages[0]["content"] == "typed query\nspoken words"

    host.session.db.update_last_user_content.assert_called_once_with("typed query\nspoken words")
    host.session.add_user_message.assert_not_called()


def test_native_audio_stt_fallback_stop_does_not_spawn_chat():
    """Stop during fallback STT ends the drain without a no-speech banner."""
    from plugin.chatbot.tool_loop import try_native_audio_stt_fallback

    host = MagicMock()
    host.audio_wav_path = "/fake/a.wav"
    host._terminal_status = "Ready"
    host._turn.alive = True
    host._turn.batcher = None
    host._turn.queue = MagicMock()
    host._turn.text_model = "chat-model"
    host._turn.endpoint = "https://example"
    host._turn.active_query_text = "hello"
    host._turn.active_client = MagicMock()
    host._turn.active_max_tokens = 100
    host._turn.active_tools = []

    def _stopped(_path, _model):
        host._terminal_status = "Stopped"
        return ""

    host._transcribe_audio.side_effect = _stopped
    with (
        patch("plugin.framework.client.model_fetcher.get_text_model", return_value="chat-model"),
        patch("plugin.framework.config.get_current_endpoint", return_value="https://example"),
        patch("plugin.framework.client.model_fetcher.get_stt_model", return_value="stt-model"),
        patch("plugin.framework.client.model_fetcher.set_native_audio_support"),
        patch("plugin.audio.stt_service.uses_local_stt", return_value=False),
        patch("plugin.chatbot.tool_loop.os.remove") as mock_remove,
    ):
        recovered = try_native_audio_stt_fallback(host, "unsupported modality: audio")

    assert recovered is None
    host._tool_calling._spawn_llm_worker.assert_not_called()
    mock_remove.assert_not_called()
    assert host.audio_wav_path == "/fake/a.wav"
    texts = [str(call.args[0]) for call in host._append_response.call_args_list]
    assert any("Falling back to STT" in text for text in texts)
    assert not any("No speech detected" in text for text in texts)


def test_native_audio_stt_fallback_uses_turn_model_on_mid_turn_change():
    """If model is changed in combobox mid-turn, fallback marks the turn's model unsupported, not the new one."""
    from plugin.chatbot.tool_loop import try_native_audio_stt_fallback

    host = MagicMock()
    host.audio_wav_path = "/fake/a.wav"
    host._terminal_status = "Ready"
    host._turn.alive = True
    host._turn.batcher = None
    host._turn.queue = MagicMock()
    host._turn.text_model = "turn-captured-model"
    host._turn.endpoint = "https://captured-endpoint"
    host._turn.active_query_text = "typed query"
    host._turn.active_client = MagicMock()
    host._turn.active_max_tokens = 100
    host._turn.active_tools = []

    audio_msg = {"role": "user", "content": [{"type": "text", "text": "typed query"}, {"type": "input_audio"}]}
    host.session.messages = [audio_msg]
    host.session.db = MagicMock()
    host.session.db.get_messages.return_value = [{"role": "user", "content": "[Voice message]"}]
    host._transcribe_audio.return_value = "spoken words"

    with (
        patch("plugin.framework.client.model_fetcher.get_text_model", return_value="newly-selected-model"),
        patch("plugin.framework.config.get_current_endpoint", return_value="https://new-endpoint"),
        patch("plugin.framework.client.model_fetcher.get_stt_model", return_value="stt-model"),
        patch("plugin.framework.client.model_fetcher.set_native_audio_support") as mock_set_support,
        patch("plugin.audio.stt_service.uses_local_stt", return_value=False),
        patch("plugin.chatbot.tool_loop.os.remove"),
    ):
        recovered = try_native_audio_stt_fallback(host, "unsupported modality: audio")

    assert recovered is True
    mock_set_support.assert_called_once_with("turn-captured-model", "https://captured-endpoint", supported=False)
