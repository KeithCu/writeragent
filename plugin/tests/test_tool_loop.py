

import sys
from unittest.mock import Mock, MagicMock, patch

# Mock uno for the sandbox before importing anything that depends on it
# To avoid metaclass conflicts when UNO base classes are inherited,
# we define simple Python class stubs instead of MagicMocks.
class UnoHelperBaseStub(object):
    pass

sys.modules['uno'] = MagicMock()

unohelper_mock = MagicMock()
unohelper_mock.Base = UnoHelperBaseStub
sys.modules['unohelper'] = unohelper_mock

com_mock = MagicMock()
sys.modules['com'] = com_mock
sys.modules['com.sun'] = com_mock
sys.modules['com.sun.star'] = com_mock

# Define exact structure for awt to avoid metaclass inheritance issues
class AwtMock:
    class XActionListener(object):
        pass
    class XTopWindowListener(object):
        pass
    class XWindowListener(object):
        pass
    class XFocusListener(object):
        pass
    class XKeyListener(object):
        pass
    class XMouseListener(object):
        pass
    class XTextListener(object):
        pass

sys.modules['com.sun.star.awt'] = AwtMock()
sys.modules['com.sun.star.beans'] = com_mock
sys.modules['com.sun.star.lang'] = com_mock
sys.modules['com.sun.star.task'] = com_mock
sys.modules['com.sun.star.frame'] = com_mock

# Now import the actual ToolCallingMixin class from the module
from plugin.modules.chatbot.tool_loop import ToolCallingMixin  # noqa: E402

class MockSession:
    def __init__(self):
        self.messages = []

    def add_assistant_message(self, content=None, tool_calls=None):
        msg = {"role": "assistant"}
        if content:
            msg["content"] = content
        if tool_calls:
            msg["tool_calls"] = tool_calls
        self.messages.append(msg)

    def add_tool_result(self, call_id, result):
        self.messages.append({
            "role": "tool",
            "tool_call_id": call_id,
            "content": result
        })

    def update_document_context(self, context_text):
        pass

class FakePanel(ToolCallingMixin):
    """A minimal mock implementation of the Chatbot panel that uses ToolCallingMixin."""
    def __init__(self, ctx, session):
        self.ctx = ctx
        self.session = session
        self.stop_requested = False
        self.audio_wav_path = None
        self.image_model_selector = Mock()
        self._append_response = Mock()
        self._set_status = Mock()
        self._spawn_llm_worker = Mock()
        self._spawn_final_stream = Mock()
        self._terminal_status = "Ready"

def setup_mock_panel():
    ctx = MagicMock()
    # Mock Toolkit for the UI dependency check inside _start_tool_calling_async()
    mock_toolkit = MagicMock()
    mock_sm = MagicMock()
    mock_sm.createInstanceWithContext.return_value = mock_toolkit
    ctx.getServiceManager = lambda: mock_sm

    session = MockSession()
    panel = FakePanel(ctx, session)
    return panel, session

@patch('plugin.modules.chatbot.tool_loop.run_stream_drain_loop')
@patch('plugin.modules.chatbot.tool_loop.get_config')
def test_stream_done_no_tools(mock_get_config, mock_drain_loop):
    panel, session = setup_mock_panel()

    captured_callback = {}
    def mock_drain_impl(q, toolkit, thinking_open, append_fn, on_stream_done=None, **kwargs):
        captured_callback['on_stream_done'] = on_stream_done

    mock_drain_loop.side_effect = mock_drain_impl

    # Run the loop to bind the nested function and capture it
    client = Mock()
    panel._start_tool_calling_async(client, model="mock-model", max_tokens=100, tools=[], execute_tool_fn=Mock())

    on_stream_done = captured_callback.get('on_stream_done')
    assert on_stream_done is not None

    # 1. Test standard stream completion with no tools
    result = on_stream_done(('stream_done', {"content": "Hello World", "tool_calls": []}))

    # When no tools are present, it should exit the loop (return True)
    assert result is True

    # It should have updated the session and the UI
    assert len(session.messages) == 1
    assert session.messages[0] == {"role": "assistant", "content": "Hello World"}

    panel._append_response.assert_called_with("\n")
    panel._set_status.assert_called_with("Ready")


@patch('plugin.modules.chatbot.tool_loop.run_stream_drain_loop')
@patch('plugin.modules.chatbot.tool_loop.get_config')
def test_stream_done_with_tools(mock_get_config, mock_drain_loop):
    panel, session = setup_mock_panel()

    captured_q = None
    captured_callback = {}
    def mock_drain_impl(q, toolkit, thinking_open, append_fn, on_stream_done=None, **kwargs):
        nonlocal captured_q
        captured_q = q
        captured_callback['on_stream_done'] = on_stream_done

    mock_drain_loop.side_effect = mock_drain_impl

    # Run the loop to bind the nested function and capture it
    client = Mock()
    panel._start_tool_calling_async(client, model="mock-model", max_tokens=100, tools=[], execute_tool_fn=Mock())

    on_stream_done = captured_callback.get('on_stream_done')
    assert on_stream_done is not None

    # Define tool call data
    tool_calls = [
        {"id": "call_abc", "type": "function", "function": {"name": "get_weather", "arguments": "{}"}}
    ]

    result = on_stream_done(('stream_done', {"content": "Let me check.", "tool_calls": tool_calls}))

    # When tools are present, it should queue them and return False to continue the loop
    assert result is False

    # Session should have the assistant message with tool calls
    assert len(session.messages) == 1
    assert session.messages[0]["content"] == "Let me check."
    assert session.messages[0]["tool_calls"] == tool_calls

    panel._append_response.assert_called_with("\n")

    # It should have enqueued the ('next_tool',) command to dispatch the first tool
    assert not captured_q.empty()
    queued_item = captured_q.get()
    assert queued_item == ("next_tool",)


@patch('plugin.modules.chatbot.tool_loop.run_stream_drain_loop')
@patch('plugin.modules.chatbot.tool_loop.get_config')
def test_next_tool_advances_round(mock_get_config, mock_drain_loop):
    panel, session = setup_mock_panel()

    captured_callback = {}
    def mock_drain_impl(q, toolkit, thinking_open, append_fn, on_stream_done=None, **kwargs):
        captured_callback['on_stream_done'] = on_stream_done

    mock_drain_loop.side_effect = mock_drain_impl

    client = Mock()
    panel._start_tool_calling_async(client, model="mock-model", max_tokens=100, tools=[], execute_tool_fn=Mock())
    on_stream_done = captured_callback.get('on_stream_done')

    # Send 'next_tool' when there are no pending tools
    result = on_stream_done(('next_tool',))

    assert result is False
    # When pending_tools is empty, it advances the round and spawns another worker
    panel._set_status.assert_called_with("Sending results to AI...")
    panel._spawn_llm_worker.assert_called()

@patch('plugin.modules.chatbot.tool_loop.run_stream_drain_loop')
@patch('plugin.modules.chatbot.tool_loop.get_config')
@patch('plugin.modules.chatbot.tool_loop.update_activity_state')
def test_next_tool_executes_tool(mock_update_activity, mock_get_config, mock_drain_loop):
    panel, session = setup_mock_panel()

    captured_q = None
    captured_callback = {}
    def mock_drain_impl(q, toolkit, thinking_open, append_fn, on_stream_done=None, **kwargs):
        nonlocal captured_q
        captured_q = q
        captured_callback['on_stream_done'] = on_stream_done

    mock_drain_loop.side_effect = mock_drain_impl

    execute_tool_mock = Mock()
    execute_tool_mock.return_value = '{"success": true}'

    client = Mock()
    panel._start_tool_calling_async(client, model="mock-model", max_tokens=100, tools=[], execute_tool_fn=execute_tool_mock)
    on_stream_done = captured_callback.get('on_stream_done')

    # First, queue a tool by simulating a stream_done
    tool_calls = [
        {"id": "call_abc", "type": "function", "function": {"name": "apply_document_content", "arguments": '{"content": "hi"}'}}
    ]
    on_stream_done(('stream_done', {"content": None, "tool_calls": tool_calls}))

    # Consume the enqueued 'next_tool' signal that the stream_done just put in
    item = captured_q.get()
    assert item == ("next_tool",)

    # Now simulate processing 'next_tool'
    result = on_stream_done(('next_tool',))

    # Keep looping
    assert result is False

    panel._set_status.assert_called_with("Running: apply_document_content")
    panel._append_response.assert_called_with("[Running tool: apply_document_content...]\n")

    # Ensure tool execution was synchronous for this tool and was called
    execute_tool_mock.assert_called_once()

    # The synchronous tool execution pushes 'tool_done' to the queue
    assert not captured_q.empty()
    queued_item = captured_q.get()

    assert queued_item[0] == "tool_done"
    assert queued_item[1] == "call_abc"
    assert queued_item[2] == "apply_document_content"
    assert queued_item[4] == '{"success": true}'
