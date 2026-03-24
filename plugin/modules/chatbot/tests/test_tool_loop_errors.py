import pytest
import json
import logging
from unittest.mock import MagicMock, patch

# Provide mock for uno since we are running outside LibreOffice
import sys
class MockUno:
    pass
sys.modules['uno'] = MockUno()
sys.modules['unohelper'] = MockUno()

from plugin.framework.errors import (
    ToolExecutionError,
    UnoObjectError,
    format_error_payload
)

from plugin.modules.chatbot.tool_loop import ToolCallingMixin
from plugin.modules.chatbot.audio_recorder_state import AudioRecorderState
from plugin.modules.chatbot.send_state import SendButtonState
from plugin.modules.chatbot.sidebar_state import SidebarCompositeState

class MockSession:
    def __init__(self):
        self.messages = [{"role": "system", "content": "test"}]

    def update_document_context(self, text):
        pass

    def add_user_message(self, text):
        pass

    def add_assistant_message(self, content=None, tool_calls=None):
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

    def _append_response(self, text):
        self.responses.append(text)

    def _set_status(self, text):
        self.statuses.append(text)

@pytest.fixture
def mock_get_tools():
    import sys
    # Add a mock plugin.main module so we can patch plugin.main.get_tools
    class MockMain:
        pass
    sys.modules['plugin.main'] = MockMain()

    with patch("plugin.main.get_tools", create=True) as mock_gt:
        registry = MagicMock()
        mock_gt.return_value = registry
        yield registry

@pytest.fixture
def test_instance():
    instance = MockDummyToolCallingClass()

    # Mock some configs used in the main logic to avoid full system dependency
    with patch("plugin.modules.chatbot.tool_loop.get_config") as mock_get_config, \
         patch("plugin.modules.chatbot.tool_loop.get_api_config") as mock_get_api_config, \
         patch("plugin.modules.chatbot.tool_loop.validate_api_config") as mock_validate_api_config, \
         patch("plugin.modules.chatbot.tool_loop.get_chat_system_prompt_for_document") as mock_get_prompt:

        mock_get_config.side_effect = lambda ctx, key: "10" if "tokens" in key or "context" in key else "test"
        mock_get_api_config.return_value = {"chat_max_tool_rounds": 1}
        mock_validate_api_config.return_value = (True, "")
        mock_get_prompt.return_value = "System prompt"

        yield instance

def test_tool_execution_error_handling(test_instance, mock_get_tools):
    # Setup mock to simulate a tool throwing an error when execute_fn is called
    registry = mock_get_tools
    registry.get_schemas.return_value = [{"name": "test_tool"}]

    # Simulate execute function
    with patch("plugin.modules.chatbot.tool_loop.get_document_context_for_chat") as mock_doc_context, \
         patch("plugin.modules.chatbot.tool_loop.agent_log") as mock_agent_log:

        mock_doc_context.return_value = "doc text"

        # Test 1: ToolExecutionError
        registry.execute.side_effect = ToolExecutionError("Specific tool error")

        # Call the private method that defines execute_fn
        # Instead of calling _do_send_chat_with_tools which goes all the way, we extract the execute_fn definition
        # Since _do_send_chat_with_tools defines execute_fn dynamically, we need to run it and intercept

        # We patch _start_tool_calling_async to capture the execute_fn
        captured_execute_fn = []
        def mock_start(*args, **kwargs):
            # execute_fn is the 5th argument in the positional arguments of _start_tool_calling_async
            captured_execute_fn.append(args[4])

        with patch.object(test_instance, "_start_tool_calling_async", mock_start):
            test_instance._do_send_chat_with_tools("test", "test_model", "writer")

        execute_fn = captured_execute_fn[0]

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
    mock_get_tools.get_schemas.return_value = [{"name": "test_tool"}]

    with patch("plugin.modules.chatbot.tool_loop.get_document_context_for_chat") as mock_doc_context:

        # Test 1: UnoObjectError
        mock_doc_context.side_effect = UnoObjectError("Document dead")

        test_instance._do_send_chat_with_tools("test", "test_model", "writer")

        assert test_instance._terminal_status == "Error"
        assert any("[Document closed or unavailable.]" in r for r in test_instance.responses)

        # Test 2: Unexpected Exception
        test_instance.responses.clear()
        mock_doc_context.side_effect = RuntimeError("Something bad")

        test_instance._do_send_chat_with_tools("test", "test_model", "writer")

        assert test_instance._terminal_status == "Error"
        assert any("[Error reading document: Failed to get document context]" in r for r in test_instance.responses)

def test_audio_handling_error(test_instance, mock_get_tools):
    mock_get_tools.get_schemas.return_value = [{"name": "test_tool"}]

    with patch("plugin.modules.chatbot.tool_loop.get_document_context_for_chat") as mock_doc_context, \
         patch("plugin.modules.chatbot.tool_loop.agent_log"):

        mock_doc_context.return_value = "doc text"

        test_instance.audio_wav_path = "/fake/path/audio.wav"

        # Override open to throw IOError
        with patch("builtins.open", side_effect=IOError("Disk full")):
            test_instance._do_send_chat_with_tools("test", "test_model", "writer")

            # The error shouldn't crash the loop
            assert test_instance.audio_wav_path is None
            assert any("You: test" in r for r in test_instance.responses)
            assert test_instance._terminal_status != "Error" # Should not terminate on audio error

        # Override open to throw unexpected error
        test_instance.audio_wav_path = "/fake/path/audio.wav"
        with patch("builtins.open", side_effect=TypeError("Bad arguments")):
            test_instance._do_send_chat_with_tools("test", "test_model", "writer")

            # The error shouldn't crash the loop
            assert test_instance.audio_wav_path is None
            assert any("You: test" in r for r in test_instance.responses)
            assert test_instance._terminal_status != "Error"
