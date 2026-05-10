import sys
import types
from unittest.mock import Mock, MagicMock, patch

from plugin.tests.testing_utils import setup_uno_mocks
setup_uno_mocks()

# Define exact structure for awt to avoid metaclass inheritance issues not covered by setup_uno_mocks
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

setattr(sys.modules['com.sun.star.awt'], 'XTopWindowListener', XTopWindowListener)
setattr(sys.modules['com.sun.star.awt'], 'XWindowListener', XWindowListener)
setattr(sys.modules['com.sun.star.awt'], 'XFocusListener', XFocusListener)
setattr(sys.modules['com.sun.star.awt'], 'XKeyListener', XKeyListener)
setattr(sys.modules['com.sun.star.awt'], 'XMouseListener', XMouseListener)
setattr(sys.modules['com.sun.star.awt'], 'XTextListener', XTextListener)


from plugin.framework.async_stream import StreamQueueKind  # noqa: E402

# Now import the actual ToolCallingMixin class from the module
from plugin.modules.chatbot.tool_loop import ToolCallingMixin  # noqa: E402
from plugin.modules.chatbot.audio_recorder_state import AudioRecorderState  # noqa: E402
from plugin.modules.chatbot.send_state import SendButtonState  # noqa: E402
from plugin.modules.chatbot.sidebar_state import SidebarCompositeState  # noqa: E402

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
        self.sidebar_state = SidebarCompositeState(
            send=SendButtonState(
                False, False, False, False, False,
            ),
            tool_loop=None,
            audio=AudioRecorderState(status="idle"),
        )
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

    results = []
    def mock_drain_impl(q, toolkit, thinking_open, append_fn, on_stream_done=None, **kwargs):
        res = on_stream_done((StreamQueueKind.STREAM_DONE, {"content": "Hello World", "tool_calls": []}))
        results.append(res)

    mock_drain_loop.side_effect = mock_drain_impl

    client = Mock()
    panel._start_tool_calling_async(client, model="mock-model", max_tokens=100, tools=[], execute_tool_fn=Mock())

    assert results[0] is True

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
    results = []
    tool_calls = [
        {"id": "call_abc", "type": "function", "function": {"name": "get_weather", "arguments": "{}"}}
    ]

    def mock_drain_impl(q, toolkit, thinking_open, append_fn, on_stream_done=None, **kwargs):
        nonlocal captured_q
        captured_q = q
        res = on_stream_done((StreamQueueKind.STREAM_DONE, {"content": "Let me check.", "tool_calls": tool_calls}))
        results.append(res)

    mock_drain_loop.side_effect = mock_drain_impl

    client = Mock()
    panel._start_tool_calling_async(client, model="mock-model", max_tokens=100, tools=[], execute_tool_fn=Mock())

    assert results[0] is False

    # Session should have the assistant message with tool calls
    assert len(session.messages) == 1
    assert session.messages[0]["content"] == "Let me check."
    assert session.messages[0]["tool_calls"] == tool_calls

    panel._append_response.assert_called_with("\n")

    # It should have enqueued NEXT_TOOL to dispatch the first tool
    assert not captured_q.empty()
    queued_item = captured_q.get()
    assert queued_item == (StreamQueueKind.NEXT_TOOL,)


@patch('plugin.modules.chatbot.tool_loop.run_stream_drain_loop')
@patch('plugin.modules.chatbot.tool_loop.get_config')
def test_next_tool_advances_round(mock_get_config, mock_drain_loop):
    panel, session = setup_mock_panel()

    results = []
    def mock_drain_impl(q, toolkit, thinking_open, append_fn, on_stream_done=None, **kwargs):
        res = on_stream_done((StreamQueueKind.NEXT_TOOL,))
        results.append(res)

    mock_drain_loop.side_effect = mock_drain_impl

    client = Mock()
    panel._start_tool_calling_async(client, model="mock-model", max_tokens=100, tools=[], execute_tool_fn=Mock())

    assert results[0] is False
    # When pending_tools is empty, it advances the round and spawns another worker
    panel._set_status.assert_called_with("Sending results to AI...")
    panel._spawn_llm_worker.assert_called()

@patch('plugin.modules.chatbot.tool_loop.run_stream_drain_loop')
@patch('plugin.modules.chatbot.tool_loop.get_config')
@patch('plugin.modules.chatbot.tool_loop.update_activity_state')
def test_next_tool_executes_tool(mock_update_activity, mock_get_config, mock_drain_loop):
    panel, session = setup_mock_panel()

    captured_q = None
    results = []
    tool_calls = [
        {"id": "call_abc", "type": "function", "function": {"name": "apply_document_content", "arguments": '{"content": "hi"}'}}
    ]

    def mock_drain_impl(q, toolkit, thinking_open, append_fn, on_stream_done=None, **kwargs):
        nonlocal captured_q
        captured_q = q
        on_stream_done((StreamQueueKind.STREAM_DONE, {"content": None, "tool_calls": tool_calls}))
        
        item = q.get()
        assert item == (StreamQueueKind.NEXT_TOOL,)
        
        res = on_stream_done((StreamQueueKind.NEXT_TOOL,))
        results.append(res)

    mock_drain_loop.side_effect = mock_drain_impl

    execute_tool_mock = Mock()
    execute_tool_mock.return_value = '{"success": true}'

    client = Mock()
    panel._start_tool_calling_async(client, model="mock-model", max_tokens=100, tools=[], execute_tool_fn=execute_tool_mock)

    assert results[0] is False

    panel._set_status.assert_called_with("Running: apply_document_content")
    panel._append_response.assert_called_with("[Running tool: apply_document_content...]\n")

    # Ensure tool execution was synchronous for this tool and was called
    execute_tool_mock.assert_called_once()

    # The synchronous tool execution pushes 'tool_done' to the queue
    assert not captured_q.empty()
    queued_item = captured_q.get()

    assert queued_item[0] == StreamQueueKind.TOOL_DONE
    assert queued_item[1] == "call_abc"
    assert queued_item[2] == "apply_document_content"
    assert queued_item[4] == '{"success": true}'


@patch('plugin.modules.chatbot.tool_loop.run_stream_drain_loop')
@patch('plugin.modules.chatbot.tool_loop.get_config')
@patch('plugin.modules.chatbot.tool_loop.update_activity_state')
def test_multiple_tool_calls_ordering_and_ids(mock_update_activity, mock_get_config, mock_drain_loop):
    panel, session = setup_mock_panel()

    captured_q = None
    results = []
    tool_calls = [
        {"id": "call_1", "type": "function", "function": {"name": "tool_one", "arguments": '{"arg": 1}'}},
        {"id": "call_2", "type": "function", "function": {"name": "tool_two", "arguments": '{"arg": 2}'}}
    ]

    def mock_drain_impl(q, toolkit, thinking_open, append_fn, on_stream_done=None, **kwargs):
        nonlocal captured_q
        captured_q = q
        
        on_stream_done((StreamQueueKind.STREAM_DONE, {"content": "Calling tools", "tool_calls": tool_calls}))
        assert len(session.messages) == 1
        
        item = q.get()
        assert item == (StreamQueueKind.NEXT_TOOL,)
        
        res1 = on_stream_done((StreamQueueKind.NEXT_TOOL,))
        results.append(res1)
        
        tool_done_item1 = q.get()
        res2 = on_stream_done(tool_done_item1)
        results.append(res2)
        
        item = q.get()
        res3 = on_stream_done((StreamQueueKind.NEXT_TOOL,))
        results.append(res3)
        
        tool_done_item2 = q.get()
        res4 = on_stream_done(tool_done_item2)
        results.append(res4)

    mock_drain_loop.side_effect = mock_drain_impl

    execution_order = []
    def mock_execute_tool(name, args, doc, ctx, **kwargs):
        execution_order.append(name)
        return '{"result": "ok", "tool": "%s"}' % name

    execute_tool_mock = Mock(side_effect=mock_execute_tool)

    client = Mock()
    panel._start_tool_calling_async(client, model="mock-model", max_tokens=100, tools=[], execute_tool_fn=execute_tool_mock)

    assert len(results) == 4

    # Verify it added tool result to session
    assert len(session.messages) == 3
    assert session.messages[2]["role"] == "tool"
    assert session.messages[2]["tool_call_id"] == "call_2"

@patch('plugin.modules.chatbot.tool_loop.run_stream_drain_loop')
@patch('plugin.modules.chatbot.tool_loop.get_config')
@patch('plugin.modules.chatbot.tool_loop.update_activity_state')
def test_stop_requested_mid_round(mock_update_activity, mock_get_config, mock_drain_loop):
    panel, session = setup_mock_panel()

    results = []
    tool_calls = [
        {"id": "call_1", "type": "function", "function": {"name": "apply_document_content", "arguments": '{"content": "hi"}'}}
    ]

    def mock_drain_impl(q, toolkit, thinking_open, append_fn, on_stream_done=None, **kwargs):
        on_stream_done((StreamQueueKind.STREAM_DONE, {"content": None, "tool_calls": tool_calls}))
        item = q.get()
        assert item == (StreamQueueKind.NEXT_TOOL,)
        
        panel.stop_requested = True
        res = on_stream_done((StreamQueueKind.NEXT_TOOL,))
        results.append(res)

    mock_drain_loop.side_effect = mock_drain_impl

    execute_tool_mock = Mock()

    client = Mock()
    panel._start_tool_calling_async(client, model="mock-model", max_tokens=100, tools=[], execute_tool_fn=execute_tool_mock)

    assert results[0] is False

    # Verify execute tool was NOT called because StopRequested skips the pending tools
    execute_tool_mock.assert_not_called()

    # Verify that it spawned worker (or final stream), which would then emit the stopped sentinel
    panel._spawn_llm_worker.assert_called()


@patch('plugin.modules.chatbot.tool_loop.run_stream_drain_loop')
@patch('plugin.modules.chatbot.tool_loop.get_config')
@patch('plugin.modules.chatbot.tool_loop.update_activity_state')
def test_malformed_tool_calls_handling(mock_update_activity, mock_get_config, mock_drain_loop):
    panel, session = setup_mock_panel()

    captured_q = None
    results = []
    tool_calls = [{
        "type": "function",
        "function": { "arguments": 'not-valid-json' }
    }]

    def mock_drain_impl(q, toolkit, thinking_open, append_fn, on_stream_done=None, **kwargs):
        nonlocal captured_q
        captured_q = q
        on_stream_done((StreamQueueKind.STREAM_DONE, {"content": None, "tool_calls": tool_calls}))
        item = q.get()
        res = on_stream_done((StreamQueueKind.NEXT_TOOL,))
        results.append(res)

    mock_drain_loop.side_effect = mock_drain_impl

    executed_args = {}
    def mock_execute_tool(name, args, doc, ctx, **kwargs):
        executed_args['name'] = name
        executed_args['args'] = args
        return '{"result": "ok"}'

    execute_tool_mock = Mock(side_effect=mock_execute_tool)

    client = Mock()
    panel._start_tool_calling_async(client, model="mock-model", max_tokens=100, tools=[], execute_tool_fn=execute_tool_mock)

    assert results[0] is False

    # Verify execute tool was called with fallbacks
    assert executed_args['name'] == 'unknown'
    assert executed_args['args'] == {}

    # Check the queue for tool_done and verify fallback values
    tool_done_item = captured_q.get()
    assert tool_done_item[0] == StreamQueueKind.TOOL_DONE
    assert tool_done_item[1] == ""  # Missing ID fallback
    assert tool_done_item[2] == "unknown" # Missing name fallback
    assert tool_done_item[3] == "not-valid-json" # Should be the original string
    assert tool_done_item[4] == '{"result": "ok"}'


@patch('plugin.modules.chatbot.tool_loop.run_stream_drain_loop')
@patch('plugin.modules.chatbot.tool_loop.get_config')
@patch('plugin.modules.chatbot.tool_loop.update_activity_state')
def test_max_tool_rounds_exhausted(mock_update_activity, mock_get_config, mock_drain_loop):
    panel, session = setup_mock_panel()

    def mock_drain_impl(q, toolkit, thinking_open, append_fn, on_stream_done=None, **kwargs):
        tool_calls_1 = [{"id": "call_1", "type": "function", "function": {"name": "dummy", "arguments": "{}"}}]
        on_stream_done((StreamQueueKind.STREAM_DONE, {"content": "step 1", "tool_calls": tool_calls_1}))
        q.get()  # next_tool
        on_stream_done((StreamQueueKind.NEXT_TOOL,))
        
        # Simulate tool execution result
        on_stream_done((StreamQueueKind.TOOL_DONE, "call_1", "dummy", "{}", '{"ok": true}', False))
        q.get()  # next_tool
        on_stream_done((StreamQueueKind.NEXT_TOOL,))
        
        # Round 1
        tool_calls_2 = [{"id": "call_2", "type": "function", "function": {"name": "dummy", "arguments": "{}"}}]
        on_stream_done((StreamQueueKind.STREAM_DONE, {"content": "step 2", "tool_calls": tool_calls_2}))
        q.get()  # next_tool
        on_stream_done((StreamQueueKind.NEXT_TOOL,))
        
        on_stream_done((StreamQueueKind.TOOL_DONE, "call_2", "dummy", "{}", '{"ok": true}', False))
        q.get()  # next_tool
        on_stream_done((StreamQueueKind.NEXT_TOOL,))

    mock_drain_loop.side_effect = mock_drain_impl
    execute_tool_mock = Mock()

    client = Mock()
    panel._start_tool_calling_async(client, model="mock-model", max_tokens=100, tools=[], execute_tool_fn=execute_tool_mock, max_tool_rounds=2)

    panel._spawn_llm_worker.assert_called()
    panel._spawn_final_stream.assert_called()

@patch('plugin.modules.chatbot.tool_loop.run_stream_drain_loop')
@patch('plugin.modules.chatbot.tool_loop.get_config')
def test_final_done_handling(mock_get_config, mock_drain_loop):
    panel, session = setup_mock_panel()

    results = []
    def mock_drain_impl(q, toolkit, thinking_open, append_fn, on_stream_done=None, **kwargs):
        res = on_stream_done((StreamQueueKind.FINAL_DONE, 'This is the final word.'))
        results.append(res)

    mock_drain_loop.side_effect = mock_drain_impl

    client = Mock()
    panel._start_tool_calling_async(client, model="mock-model", max_tokens=100, tools=[], execute_tool_fn=Mock())

    assert results[0] is True # Return true means exit loop
    assert panel._terminal_status == "Ready"
    assert len(session.messages) == 1
    assert session.messages[0]["content"] == "This is the final word."

@patch('plugin.modules.chatbot.tool_loop.run_stream_drain_loop')
@patch('plugin.modules.chatbot.tool_loop.get_config')
def test_error_handling_in_loop(mock_get_config, mock_drain_loop):
    panel, session = setup_mock_panel()

    results = []
    exc = Exception("Network failure")

    def mock_drain_impl(q, toolkit, thinking_open, append_fn, on_stream_done=None, **kwargs):
        res = on_stream_done((StreamQueueKind.ERROR, exc))
        results.append(res)

    mock_drain_loop.side_effect = mock_drain_impl

    client = Mock()
    panel._start_tool_calling_async(client, model="mock-model", max_tokens=100, tools=[], execute_tool_fn=Mock())

    assert results[0] is True # Should exit loop


def test_refresh_active_tools_for_session():
    """Avoid @patch(plugin.main.get_tools): importing plugin.main pulls UNO-heavy code."""
    fake_registry = MagicMock()
    fake_registry.get_schemas.return_value = [{"function": {"name": "fresh"}}]
    fake_main = types.ModuleType("plugin.main")
    fake_main.get_tools = MagicMock(return_value=fake_registry)

    old_main = sys.modules.pop("plugin.main", None)
    sys.modules["plugin.main"] = fake_main
    try:
        panel, session = setup_mock_panel()
        panel._active_model = MagicMock()
        session.active_specialized_domain = "tables"
        panel._active_tools = [{"function": {"name": "stale"}}]

        panel._refresh_active_tools_for_session()

        fake_registry.get_schemas.assert_called_once_with(
            "openai", doc=panel._active_model, active_domain="tables"
        )
        assert panel._active_tools == [{"function": {"name": "fresh"}}]
    finally:
        if old_main is not None:
            sys.modules["plugin.main"] = old_main
        else:
            sys.modules.pop("plugin.main", None)
