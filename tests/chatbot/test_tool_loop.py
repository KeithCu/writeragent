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
from plugin.chatbot.tool_loop import ToolCallingMixin  # noqa: E402
from plugin.chatbot.audio_recorder_state import AudioRecorderState  # noqa: E402
from plugin.chatbot.send_state import SendButtonState  # noqa: E402
from plugin.chatbot.sidebar_state import SidebarCompositeState  # noqa: E402

class MockSession:
    def __init__(self):
        self.messages = []

    def set_system_context(self, base_prompt, doc_text=""):
        content = f"{base_prompt}\n\n[DOCUMENT CONTENT]\n{doc_text}\n[END DOCUMENT]"
        if not self.messages or self.messages[0]["role"] != "system":
            self.messages.insert(0, {"role": "system", "content": content})
        else:
            self.messages[0]["content"] = content

    def add_assistant_message(self, content=None, tool_calls=None, reasoning_replay=None):
        msg = {"role": "assistant"}
        if content:
            msg["content"] = content
        if tool_calls:
            msg["tool_calls"] = tool_calls
        if reasoning_replay:
            msg.update(reasoning_replay)
        self.messages.append(msg)

    def add_tool_result(self, call_id, result):
        self.messages.append({
            "role": "tool",
            "tool_call_id": call_id,
            "content": result
        })

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
        self._stop_requested_fallback = False
        self._send_cancellation = None
        self.audio_wav_path = None
        self.image_model_selector = Mock()
        self._append_response = Mock()
        self._set_status = Mock()
        self._spawn_llm_worker = Mock()
        self._spawn_final_stream = Mock()
        self._terminal_status = "Ready"

    def resolve_stop_checker(self):
        from plugin.framework.queue_executor import bind_send_stop_checker

        return bind_send_stop_checker(getattr(self, "_send_cancellation", None), lambda: self._stop_requested_fallback)

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

@patch('plugin.chatbot.tool_loop.run_stream_drain_loop')
@patch('plugin.chatbot.tool_loop.get_config')
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


@patch('plugin.chatbot.tool_loop.run_stream_drain_loop')
@patch('plugin.chatbot.tool_loop.get_config')
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


@patch('plugin.chatbot.tool_loop.run_stream_drain_loop')
@patch('plugin.chatbot.tool_loop.get_config')
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

@patch('plugin.chatbot.tool_loop.run_stream_drain_loop')
@patch('plugin.chatbot.tool_loop.get_config')
@patch('plugin.chatbot.tool_loop.update_activity_state')
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


@patch('plugin.chatbot.tool_loop.run_stream_drain_loop')
@patch('plugin.chatbot.tool_loop.get_config')
@patch('plugin.chatbot.tool_loop.update_activity_state')
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

@patch('plugin.chatbot.tool_loop.run_stream_drain_loop')
@patch('plugin.chatbot.tool_loop.get_config')
@patch('plugin.chatbot.tool_loop.update_activity_state')
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


@patch('plugin.chatbot.tool_loop.run_stream_drain_loop')
@patch('plugin.chatbot.tool_loop.get_config')
@patch('plugin.chatbot.tool_loop.update_activity_state')
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


@patch('plugin.chatbot.tool_loop.run_stream_drain_loop')
@patch('plugin.chatbot.tool_loop.get_config')
@patch('plugin.chatbot.tool_loop.update_activity_state')
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

@patch('plugin.chatbot.tool_loop.run_stream_drain_loop')
@patch('plugin.chatbot.tool_loop.get_config')
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

@patch('plugin.chatbot.tool_loop.run_stream_drain_loop')
@patch('plugin.chatbot.tool_loop.get_config')
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

# =============================================================================
# Specialized Delegation Tests (Merged)
# =============================================================================

import pytest
from plugin.framework.tool import ToolRegistry, _is_specialized_domain_tool
from plugin.writer.specialized_base import ToolWriterSpecialBase, SpecializedWorkflowFinished
from plugin.writer.specialized_base import DelegateToSpecializedWriter
from plugin.calc.specialized import DelegateToSpecializedCalc
from plugin.draw.specialized import DelegateToSpecializedDraw
from plugin.calc.base import ToolCalcSpecialBase
from plugin.draw.base import ToolDrawSpecialBase
from plugin.contrib.smolagents.memory import FinalAnswerStep
from plugin.framework.config import get_config_int as _real_get_config_int

class DummyTableTool(ToolWriterSpecialBase):
    name = "dummy_table_tool"
    description = "A dummy table tool."
    parameters = None
    specialized_domain = "tables"

    def execute(self, ctx, **kwargs):
        return {"status": "ok", "message": "Table created"}


class DummyShapeTool(ToolWriterSpecialBase):
    name = "dummy_shape_tool"
    description = "A dummy shape tool."
    parameters = {"type": "object", "properties": {}, "required": []}
    specialized_domain = "shapes"

    def execute(self, ctx, **kwargs):
        return {"status": "ok", "message": "ok"}


class DummyCalcSpecialTool(ToolCalcSpecialBase):
    name = "dummy_calc_images_tool"
    description = "A dummy Calc specialized tool."
    parameters = {"type": "object", "properties": {}, "required": []}
    specialized_domain = "images"

    uno_services = ["com.sun.star.sheet.SpreadsheetDocument"]

    def execute(self, ctx, **kwargs):
        return {"status": "ok", "message": "ok"}


class DummyDrawSpecialTool(ToolDrawSpecialBase):
    name = "dummy_draw_special_tool"
    description = "A dummy Draw specialized tool."
    parameters = {"type": "object", "properties": {}, "required": []}
    specialized_domain = "draw_test_domain"

    uno_services = ["com.sun.star.drawing.DrawingDocument"]

    def execute(self, ctx, **kwargs):
        return {"status": "ok", "message": "ok"}


@pytest.fixture
def registry():
    r = ToolRegistry(services={})
    r.register(DummyTableTool())
    r.register(DummyShapeTool())
    r.register(SpecializedWorkflowFinished())
    r.register(DelegateToSpecializedWriter())
    return r


@pytest.fixture
def mock_ctx(registry):
    ctx = MagicMock()
    ctx.services = {"tools": registry}
    ctx.ctx = MagicMock()
    return ctx


def _mock_get_config_int_for_sub_agent(ctx, key):
    if key == "chat_max_tokens":
        return 2048
    if key == "chat_max_tool_rounds":
        return 25
    return _real_get_config_int(ctx, key)


@patch(
    "plugin.chatbot.smol_agent.get_config_int",
    side_effect=_mock_get_config_int_for_sub_agent,
)
@patch("plugin.chatbot.smol_agent.get_api_config", create=True)
@patch("plugin.chatbot.smol_agent.ToolCallingAgent")
@patch("plugin.chatbot.smol_agent.WriterAgentSmolModel")
@patch("plugin.chatbot.smol_agent.LlmClient")
def test_specialized_delegation_sub_agent_mode(
    mock_llm,
    mock_smol_model,
    mock_agent_class,
    mock_get_config,
    _mock_get_config_int,
    registry,
    mock_ctx,
):
    # Sub-agent path (default USE_SUB_AGENT=True in writer/specialized.py)
    mock_ctx.ctx = MagicMock()
    mock_get_config.return_value = {}

    active_domain = "initial_value"

    def mock_set_active_domain(domain):
        nonlocal active_domain
        active_domain = domain

    mock_ctx.set_active_domain_callback = mock_set_active_domain

    # Setup the mocked smolagents agent to yield a FinalAnswerStep
    mock_agent_instance = MagicMock()

    dummy_final_step = FinalAnswerStep(output="Mocked agent summary")
    mock_agent_instance.run.return_value = [dummy_final_step]

    mock_agent_class.return_value = mock_agent_instance

    gateway_tool = registry.get("delegate_to_specialized_writer_toolset")

    mock_ctx.stop_checker = lambda: False

    # Execute the gateway tool
    result = gateway_tool.execute_safe(
        mock_ctx,
        domain="tables",
        task="Create a 3x3 table"
    )

    # Verify callback was NOT called (it remains initial_value)
    assert active_domain == "initial_value"

    # Verify smolagents was executed and returned the summary
    assert result["status"] == "ok"
    assert "completed" in result["message"]
    assert result["result"] == "Mocked agent summary"

    # Verify the tools passed to the sub-agent
    call_args = mock_agent_class.call_args
    assert call_args is not None
    smol_tools = call_args.kwargs.get("tools", [])

    # Sub-agent gets domain tools plus specialized_workflow_finished (active_domain registry rules).
    smol_tool_names = [t.name for t in smol_tools]
    assert "dummy_table_tool" in smol_tool_names
    assert "specialized_workflow_finished" in smol_tool_names


@patch(
    "plugin.chatbot.smol_agent.get_config_int",
    side_effect=_mock_get_config_int_for_sub_agent,
)
@patch("plugin.chatbot.smol_agent.get_api_config", create=True)
@patch("plugin.chatbot.smol_agent.ToolCallingAgent")
@patch("plugin.chatbot.smol_agent.WriterAgentSmolModel")
@patch("plugin.chatbot.smol_agent.LlmClient")
def test_shapes_delegation_includes_canvas_in_instructions(
    mock_llm,
    mock_smol_model,
    mock_agent_class,
    mock_get_config,
    _mock_get_config_int,
    registry,
    mock_ctx,
):
    mock_ctx.ctx = MagicMock()
    mock_get_config.return_value = {}

    doc = MagicMock()

    def supports(svc: str) -> bool:
        return svc == "com.sun.star.text.TextDocument"

    doc.supportsService = supports
    doc.getCurrentController.return_value = None
    style = MagicMock()

    def gv(name: str):
        return {
            "Width": 21_000,
            "Height": 29_700,
            "LeftMargin": 2_000,
            "RightMargin": 2_000,
            "TopMargin": 2_500,
            "BottomMargin": 2_500,
            "IsLandscape": False,
        }[name]

    style.getPropertyValue = gv
    page_styles = MagicMock()
    page_styles.hasByName = lambda n: n == "Standard"
    page_styles.getByName = lambda n: style if n == "Standard" else MagicMock()
    families = MagicMock()
    families.getByName = lambda n: page_styles if n == "PageStyles" else MagicMock()
    doc.getStyleFamilies.return_value = families
    mock_ctx.doc = doc

    mock_agent_instance = MagicMock()
    mock_agent_instance.run.return_value = [FinalAnswerStep(output="Shapes done")]
    mock_agent_class.return_value = mock_agent_instance

    gateway_tool = registry.get("delegate_to_specialized_writer_toolset")
    mock_ctx.stop_checker = lambda: False

    result = gateway_tool.execute_safe(
        mock_ctx,
        domain="shapes",
        task="Add a rectangle",
    )
    assert result["status"] == "ok"
    instructions = mock_agent_class.call_args.kwargs["instructions"]
    assert "Document canvas (Writer)" in instructions
    assert "210.0" in instructions
    smol_tools = mock_agent_class.call_args.kwargs.get("tools", [])
    assert any(t.name == "dummy_shape_tool" for t in smol_tools)


def test_is_specialized_domain_tool_helper():
    t = DummyTableTool()
    assert _is_specialized_domain_tool(t, "tables") is True
    assert _is_specialized_domain_tool(t, "images") is False
    c = DummyCalcSpecialTool()
    assert _is_specialized_domain_tool(c, "images") is True
    d = DummyDrawSpecialTool()
    assert _is_specialized_domain_tool(d, "draw_test_domain") is True


def test_cross_cutting_venv_python_matches_python_domain():
    from plugin.calc.venv_python import RunVenvPythonScript

    r = RunVenvPythonScript()
    assert _is_specialized_domain_tool(r, "python") is True
    assert _is_specialized_domain_tool(r, "tables") is False


def test_run_venv_python_script_in_schemas_for_writer_when_domain_python(registry):
    from plugin.calc.venv_python import RunVenvPythonScript

    registry.register(RunVenvPythonScript())
    mock_writer = MagicMock()

    def supports(svc):
        return svc == "com.sun.star.text.TextDocument"

    mock_writer.supportsService = supports
    schemas = registry.get_schemas("openai", doc=mock_writer, active_domain="python")
    names = [s["function"]["name"] for s in schemas]
    assert "run_venv_python_script" in names
    assert "specialized_workflow_finished" in names
    py_schema = next(s for s in schemas if s["function"]["name"] == "run_venv_python_script")
    props = py_schema["function"]["parameters"].get("properties", {})
    assert "code" in props
    assert "data" not in props
    assert "data_range" not in props
    assert "does not inject" in (py_schema["function"]["description"] or "")


def test_active_domain_schemas_include_calc_and_draw(registry):
    """Calc/Draw specialized tools must appear when active_domain matches (in-place mode)."""
    registry.register(DummyCalcSpecialTool())
    registry.register(DummyDrawSpecialTool())

    mock_sheet = MagicMock()

    def supports(svc):
        return svc == "com.sun.star.sheet.SpreadsheetDocument"

    mock_sheet.supportsService = supports

    schemas = registry.get_schemas("openai", doc=mock_sheet, active_domain="images")
    names = [s["function"]["name"] for s in schemas]
    assert "dummy_calc_images_tool" in names
    assert "specialized_workflow_finished" in names

    mock_draw = MagicMock()

    def supports_draw(svc):
        return svc == "com.sun.star.drawing.DrawingDocument"

    mock_draw.supportsService = supports_draw

    schemas_d = registry.get_schemas("openai", doc=mock_draw, active_domain="draw_test_domain")
    names_d = [s["function"]["name"] for s in schemas_d]
    assert "dummy_draw_special_tool" in names_d
    assert "specialized_workflow_finished" in names_d


@patch(
    "plugin.chatbot.smol_agent.get_config_int",
    side_effect=_mock_get_config_int_for_sub_agent,
)
@patch("plugin.chatbot.smol_agent.get_api_config", create=True)
@patch("plugin.chatbot.smol_agent.ToolCallingAgent")
@patch("plugin.chatbot.smol_agent.WriterAgentSmolModel")
@patch("plugin.chatbot.smol_agent.LlmClient")
def test_calc_specialized_delegation_sub_agent(
    mock_llm,
    mock_smol_model,
    mock_agent_class,
    mock_get_config,
    _mock_get_config_int,
    registry,
    mock_ctx,
):
    registry.register(DummyCalcSpecialTool())
    registry.register(DelegateToSpecializedCalc())
    
    mock_agent_instance = MagicMock()
    mock_agent_instance.run.return_value = [FinalAnswerStep(output="Calc done")]
    mock_agent_class.return_value = mock_agent_instance

    gateway_tool = registry.get("delegate_to_specialized_calc_toolset")
    mock_ctx.stop_checker = lambda: False
    
    result = gateway_tool.execute_safe(mock_ctx, domain="images", task="Insert image")
    
    assert result["status"] == "ok"
    assert result["result"] == "Calc done"
    
    # Verify the domain tools passed
    call_args = mock_agent_class.call_args
    smol_tools = call_args.kwargs.get("tools", [])
    smol_tool_names = [t.name for t in smol_tools]
    assert "dummy_calc_images_tool" in smol_tool_names


@patch(
    "plugin.chatbot.smol_agent.get_config_int",
    side_effect=_mock_get_config_int_for_sub_agent,
)
@patch("plugin.chatbot.smol_agent.get_api_config", create=True)
@patch("plugin.chatbot.smol_agent.ToolCallingAgent")
@patch("plugin.chatbot.smol_agent.WriterAgentSmolModel")
@patch("plugin.chatbot.smol_agent.LlmClient")
def test_draw_specialized_delegation_sub_agent(
    mock_llm,
    mock_smol_model,
    mock_agent_class,
    mock_get_config,
    _mock_get_config_int,
    registry,
    mock_ctx,
):
    registry.register(DummyDrawSpecialTool())
    registry.register(DelegateToSpecializedDraw())
    
    mock_agent_instance = MagicMock()
    mock_agent_instance.run.return_value = [FinalAnswerStep(output="Draw done")]
    mock_agent_class.return_value = mock_agent_instance

    gateway_tool = registry.get("delegate_to_specialized_draw_toolset")
    mock_ctx.stop_checker = lambda: False
    
    result = gateway_tool.execute_safe(mock_ctx, domain="draw_test_domain", task="Create shape")
    
    assert result["status"] == "ok"
    assert result["result"] == "Draw done"
    
    # Verify the domain tools passed
    call_args = mock_agent_class.call_args
    smol_tools = call_args.kwargs.get("tools", [])
    smol_tool_names = [t.name for t in smol_tools]
    assert "dummy_draw_special_tool" in smol_tool_names


@patch("plugin.doc.specialized_base.USE_SUB_AGENT", True)
@patch("plugin.doc.specialized_base.build_toolcalling_agent")
@patch("plugin.doc.specialized_base.SmolAgentExecutor")
def test_writer_delegate_python_includes_cross_cutting_venv_tool(mock_executor_cls, mock_build, registry):
    """Writer delegate must pick up Calc-registered ``run_venv_python_script`` (specialized_cross_cutting)."""
    from plugin.calc.venv_python import RunVenvPythonScript
    from plugin.writer.specialized_base import DelegateToSpecializedWriter

    registry.register(RunVenvPythonScript())

    mock_doc = MagicMock()

    def supports(svc):
        return svc == "com.sun.star.text.TextDocument"

    mock_doc.supportsService = supports

    ctx = MagicMock()
    ctx.services = {"tools": registry}
    ctx.doc = mock_doc
    ctx.doc_type = "writer"
    ctx.ctx = MagicMock()
    ctx.status_callback = None
    ctx.append_thinking_callback = None

    mock_build.return_value = MagicMock()
    mock_exec = MagicMock()
    mock_exec.execute_safe.return_value = "done"
    mock_executor_cls.return_value = mock_exec

    gw = DelegateToSpecializedWriter()
    result = gw.execute(ctx, domain="python", task="compute primes")

    assert result["status"] == "ok"
    assert result["result"] == "done"
    instructions = mock_build.call_args.kwargs["instructions"]
    assert "DO NOT import numpy" in instructions
    assert "does not inject spreadsheet" in instructions
    tools_passed = mock_build.call_args[0][1]
    names = [t.name for t in tools_passed]
    assert "run_venv_python_script" in names
    assert "specialized_workflow_finished" in names
    venv_tool = next(t for t in tools_passed if t.name == "run_venv_python_script")
    assert "does not inject spreadsheet" in venv_tool.description
    assert "Optional data_range" not in venv_tool.description
