import pytest
import json
from unittest.mock import MagicMock, patch

from plugin.tests.testing_utils import setup_uno_mocks
setup_uno_mocks()

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

# Disabled outside LibreOffice: tool_loop.py catches Exception and imports
# com.sun.star.lang.DisposedException etc., which raises ImportError in pytest.
# def test_document_context_error_handling(test_instance, mock_get_tools):
#     mock_get_tools.get_schemas.return_value = [{"name": "test_tool"}]
#
#     with patch("plugin.chatbot.tool_loop.get_document_context_for_chat") as mock_doc_context:
#
#         # Test 1: UnoObjectError
#         mock_doc_context.side_effect = UnoObjectError("Document dead")
#
#         test_instance._do_send_chat_with_tools("test", "test_model", "writer")
#
#         assert test_instance._terminal_status == "Error"
#         assert any("[Document closed or unavailable.]" in r for r in test_instance.responses)
#
#         # Test 2: Unexpected Exception
#         test_instance.responses.clear()
#         mock_doc_context.side_effect = RuntimeError("Something bad")
#
#         test_instance._do_send_chat_with_tools("test", "test_model", "writer")
#
#         assert test_instance._terminal_status == "Error"
#         assert any("[Error reading document: Failed to get document context]" in r for r in test_instance.responses)

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
