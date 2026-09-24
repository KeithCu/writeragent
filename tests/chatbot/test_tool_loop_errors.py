import pytest
import json
from unittest.mock import MagicMock, patch


from plugin.framework.errors import (
    ToolExecutionError,
)

from plugin.chatbot.tool_loop_actions import build_tool_execute_fn
from plugin.chatbot.tool_loop import ToolCallingMixin
from plugin.chatbot.tool_loop_state import ToolLoopState
from plugin.chatbot.audio_recorder_state import AudioRecorderState
from plugin.chatbot.send_state import SendButtonState
from plugin.chatbot.sidebar_state import SidebarCompositeState

class MockSession:
    def __init__(self):
        self.messages = [{"role": "system", "content": "test"}]
        self.document_context = ""

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
def mock_get_tools():
    import sys
    # Save original modules
    original_main = sys.modules.get('plugin.main')
    
    # Add a mock plugin.main module so we can patch plugin.main.get_tools
    class MockMain:
        pass
    sys.modules['plugin.main'] = MockMain()

    try:
        with patch("plugin.main.get_tools", create=True) as mock_gt:
            registry = MagicMock()
            mock_gt.return_value = registry
            yield registry
    finally:
        # Restore original module
        if original_main:
            sys.modules['plugin.main'] = original_main
        else:
            del sys.modules['plugin.main']

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

        yield instance

def test_tool_execution_error_handling(test_instance, mock_get_tools):
    # Setup mock to simulate a tool throwing an error when execute_fn is called
    registry = mock_get_tools
    registry.get_schemas.return_value = [{"name": "test_tool"}]
    execute_fn = build_tool_execute_fn(test_instance, "writer", None, None, MagicMock())

    with patch("plugin.chatbot.tool_loop_actions.agent_log") as mock_agent_log:
        # Test 1: ToolExecutionError
        registry.execute.side_effect = ToolExecutionError("Specific tool error")

        # Execute it and verify the exception handling
        res = execute_fn("test_tool", {"arg": "val"}, None, test_instance.ctx)

        # It should return a json encoded format_error_payload
        parsed_res = json.loads(res)
        assert parsed_res["status"] == "error"
        assert parsed_res["code"] == "TOOL_EXECUTION_ERROR"
        assert parsed_res["message"] == "Specific tool error"
        mock_agent_log.assert_called()

        # Test 2: Unexpected error
        mock_agent_log.reset_mock()
        registry.execute.side_effect = ValueError("Something unexpected")

        res = execute_fn("test_tool", {"arg": "val"}, None, test_instance.ctx)
        parsed_res = json.loads(res)

        assert parsed_res["status"] == "error"
        assert parsed_res["code"] == "TOOL_UNEXPECTED_ERROR"
        assert "Unexpected error executing tool" in parsed_res["message"]
        assert parsed_res["details"]["original_error"] == "Something unexpected"

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
        assert any("[Error reading document: Failed to get document context]" in r for r in test_instance.responses)

def test_audio_handling_error(test_instance, mock_get_tools):
    mock_get_tools.get_schemas.return_value = [{"name": "test_tool"}]

    with patch("plugin.chatbot.tool_loop.agent_log"):

        test_instance.audio_wav_path = "/fake/path/audio.wav"

        # Override open to throw IOError
        with patch("builtins.open", side_effect=IOError("Disk full")):
            test_instance._do_send_chat_with_tools("test", "test_model", "writer")

            # The error shouldn't crash the loop
            assert test_instance.audio_wav_path is None
            assert any("test" in r for r in test_instance.responses)
            assert test_instance._terminal_status != "Error" # Should not terminate on audio error

        # Override open to throw unexpected error
        test_instance.audio_wav_path = "/fake/path/audio.wav"
        with patch("builtins.open", side_effect=TypeError("Bad arguments")):
            test_instance._do_send_chat_with_tools("test", "test_model", "writer")

            # The error shouldn't crash the loop
            assert test_instance.audio_wav_path is None
            assert any("test" in r for r in test_instance.responses)
            assert test_instance._terminal_status != "Error"


def test_stream_error_stt_fallback_does_not_reenter_send(test_instance):
    import dataclasses

    test_instance.audio_wav_path = "/fake/path/audio.wav"
    test_instance._active_query_text = "hello"
    test_instance._active_q = MagicMock()
    test_instance._active_batched_q = None
    test_instance._active_client = MagicMock()
    test_instance._active_max_tokens = 128
    test_instance._active_tools = []
    test_instance._active_model = MagicMock()
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
        patch("plugin.chatbot.tool_loop.get_text_model", return_value="chat-model"),
        patch("plugin.chatbot.tool_loop.get_current_endpoint", return_value="https://example"),
        patch("plugin.chatbot.tool_loop.get_stt_model", return_value="stt-model"),
        patch("plugin.chatbot.tool_loop.set_native_audio_support") as mock_cache,
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


def test_reused_llm_client_registers_on_current_send_scope(test_instance, mock_get_tools):
    """Packet B2: Stop on send 2+ must close HTTP on the reused LlmClient."""
    from plugin.framework.queue_executor import SendCancellation, agent_session

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
    with (
        patch("plugin.chatbot.tool_loop.get_text_model", return_value="chat-model"),
        patch("plugin.chatbot.tool_loop.get_current_endpoint", return_value="https://example"),
        patch("plugin.chatbot.tool_loop.get_stt_model", return_value=""),
    ):
        recovered = test_instance._handle_stream_error(payload)
    assert recovered is None
    joined = "".join(test_instance.responses)
    assert "[API error:" in joined
    assert "500" in joined
    assert "mock LLM soak failure" in joined
    assert test_instance._terminal_status == "Error"


def test_handle_stream_error_payload_dict_missing_message(test_instance):
    payload = {"status": "error", "code": "HTTP_ERROR"}
    with (
        patch("plugin.chatbot.tool_loop.get_text_model", return_value="chat-model"),
        patch("plugin.chatbot.tool_loop.get_current_endpoint", return_value="https://example"),
        patch("plugin.chatbot.tool_loop.get_stt_model", return_value=""),
    ):
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
    with (
        patch("plugin.chatbot.tool_loop.get_text_model", return_value="qwen2.5:7b"),
        patch("plugin.chatbot.tool_loop.get_current_endpoint", return_value="http://localhost:11434"),
        patch("plugin.chatbot.tool_loop.get_stt_model", return_value=""),
    ):
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
    with (
        patch("plugin.chatbot.tool_loop.get_text_model", return_value="qwen2.5:7b"),
        patch("plugin.chatbot.tool_loop.get_current_endpoint", return_value="http://localhost:11434"),
        patch("plugin.chatbot.tool_loop.get_stt_model", return_value=""),
    ):
        test_instance._handle_stream_error(payload)
    joined = "".join(test_instance.responses)
    assert named in joined
    assert "[API error:" not in joined


def _prime_active_tool_loop(instance):
    import dataclasses

    instance._active_q = MagicMock()
    instance._active_batched_q = None
    instance._active_client = MagicMock()
    instance._active_max_tokens = 128
    instance._active_tools = []
    instance._active_query_text = "hello"
    instance.sidebar_state = dataclasses.replace(
        instance.sidebar_state,
        tool_loop=ToolLoopState(round_num=0, pending_tools=[], max_rounds=8, status="Thinking..."),
    )


def _overflow_payload(message="prompt is too long"):
    return {"status": "error", "code": "HTTP_ERROR", "message": message}


def _handle_stream_error(instance, payload, compaction_enabled=True):
    with (
        patch("plugin.chatbot.tool_loop.get_text_model", return_value="chat-model"),
        patch("plugin.chatbot.tool_loop.get_current_endpoint", return_value="https://example"),
        patch("plugin.chatbot.tool_loop.get_stt_model", return_value=""),
        patch("plugin.chatbot.tool_loop.get_config_bool_safe", return_value=compaction_enabled),
    ):
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
    assert test_instance._overflow_compact_attempts == 1
    assert "Compacting conversation..." in test_instance.statuses
    assert not any(str(s).startswith("Thinking") for s in test_instance.statuses)
    assert test_instance._terminal_status is None
    joined = "".join(test_instance.responses)
    assert "[API error:" not in joined
    from plugin.framework.client.errors import local_model_overflow_message

    assert local_model_overflow_message() not in joined


def test_overflow_attempt_3_falls_through(test_instance):
    _prime_active_tool_loop(test_instance)
    test_instance._overflow_compact_attempts = 3
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
    test_instance._last_compact_reason = reason
    test_instance._last_compact_tokens_before = tokens_before
    test_instance._last_compact_tokens_after = tokens_after
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
    from plugin.framework.async_stream import StreamQueueKind

    captured = {}

    def capture_run(fn, name=None, dedicated=False):
        captured["fn"] = fn

    client = MagicMock()
    client._stopped = False
    client.stream_request_with_tools.return_value = {"content": "ok"}
    view = [{"role": "system", "content": "view"}]
    q = MagicMock()
    compact_inside_lane = {"value": False}

    class _Lane:
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
    from plugin.framework.async_stream import StreamQueueKind

    captured = {}

    def capture_run(fn, name=None, dedicated=False):
        captured["fn"] = fn

    client = MagicMock()
    q = MagicMock()
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


def test_start_tool_calling_resets_overflow_attempts(test_instance):
    test_instance._overflow_compact_attempts = 2
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
        patch("plugin.chatbot.rich_text.finalize_sidebar_assistant_response"),
    ):
        test_instance._start_tool_calling_async(
            MagicMock(), MagicMock(), 128, [], execute_fn
        )
    assert test_instance._overflow_compact_attempts == 0


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

