# WriterAgent - combined tests for smolagents functionality

from types import ModuleType, SimpleNamespace
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

# =============================================================================
# UNO mocks for librarian handoff tests
# =============================================================================


class _UnoBase:
    pass


class _XActionListener:
    pass


class _XTextListener:
    pass


class _XWindowListener:
    pass


class _XItemListener:
    pass


class _XEventListener:
    pass


sys.modules.setdefault("uno", MagicMock())
unohelper_module = sys.modules.setdefault("unohelper", MagicMock())
unohelper_module.Base = _UnoBase

com_module = sys.modules.setdefault("com", ModuleType("com"))
sun_module = sys.modules.setdefault("com.sun", ModuleType("com.sun"))
star_module = sys.modules.setdefault("com.sun.star", ModuleType("com.sun.star"))
awt_module = sys.modules.setdefault("com.sun.star.awt", ModuleType("com.sun.star.awt"))
lang_module = sys.modules.setdefault("com.sun.star.lang", ModuleType("com.sun.star.lang"))
setattr(com_module, "sun", sun_module)
setattr(sun_module, "star", star_module)
setattr(star_module, "awt", awt_module)
setattr(star_module, "lang", lang_module)
setattr(awt_module, "XActionListener", _XActionListener)
setattr(awt_module, "XTextListener", _XTextListener)
setattr(awt_module, "XWindowListener", _XWindowListener)
setattr(awt_module, "XItemListener", _XItemListener)
setattr(lang_module, "XEventListener", _XEventListener)


# =============================================================================
# Imports for all test groups
# =============================================================================

from plugin.chatbot.panel import SendButtonListener, format_grammar_status
from plugin.chatbot.smol_agent import (
    SmolToolAdapter,
    WriterAgentSmolModel,
    build_toolcalling_agent,
    to_smol_inputs,
)
from plugin.chatbot.librarian import (
    LibrarianOnboardingTool,
    SwitchToDocumentModeTool,
)
from plugin.chatbot.memory import MemoryTool
from plugin.contrib.smolagents.agents import ToolCallingAgent
from plugin.contrib.smolagents.memory import ActionStep, FinalAnswerStep, ToolCall
from plugin.contrib.smolagents.models import ChatMessage, MessageRole
from plugin.contrib.smolagents.monitoring import Timing
from plugin.contrib.smolagents.tools import Tool
from plugin.framework.tool import ToolBase
from plugin.tests.testing_utils import setup_uno_mocks

setup_uno_mocks()


# =============================================================================
# Helper for SendButtonListener tests
# =============================================================================


def _make_listener(*, in_librarian_mode: bool) -> SimpleNamespace:
    query_control = MagicMock()
    query_control.getModel.return_value = True

    return SimpleNamespace(
        ctx=MagicMock(),
        ensure_path_fn=None,
        initial_doc_type=None,
        audio_wav_path=None,
        web_research_checkbox=None,
        direct_image_checkbox=None,
        query_control=query_control,
        _in_librarian_mode=in_librarian_mode,
        _terminal_status="Ready",
        _set_status=MagicMock(),
        _get_document_model=MagicMock(return_value=object()),
        _append_response=MagicMock(),
        _get_doc_type_str=MagicMock(return_value="Writer"),
        _run_librarian=MagicMock(),
        _do_send_chat_with_tools=MagicMock(),
        _do_send_direct_image=MagicMock(),
        _do_send_via_agent_backend=MagicMock(),
    )


# =============================================================================
# ToolCallingAgent prompt examples tests (from test_toolcalling_prompt_examples.py)
# =============================================================================


class TestToolcallingPromptExamples(unittest.TestCase):
    def test_custom_system_prompt_examples_appear_in_rendered_prompt(self):
        model = MagicMock()
        marker = "CUSTOM_EXAMPLES_MARKER_XYZ123"
        agent = ToolCallingAgent(tools=[], model=model, system_prompt_examples=marker)
        prompt = agent.initialize_system_prompt()
        self.assertIn(marker, prompt)
        self.assertNotIn("__EXAMPLES_BLOCK__", prompt)

    def test_default_examples_when_system_prompt_examples_is_none(self):
        from plugin.contrib.smolagents.toolcalling_agent_prompts import DEFAULT_EXAMPLES_BLOCK

        model = MagicMock()
        agent = ToolCallingAgent(tools=[], model=model, system_prompt_examples=None)
        prompt = agent.initialize_system_prompt()
        self.assertIn("Guangzhou", prompt)
        self.assertIn(DEFAULT_EXAMPLES_BLOCK.strip().split("\n")[0], prompt)


# =============================================================================
# Smol model tests (from test_smol_model.py)
# =============================================================================


class _ReplyTool(Tool):
    name = "reply_to_user"
    description = "Reply to the user"
    inputs = {"answer": {"type": "string", "description": "answer", "nullable": True}}
    output_type = "string"

    def forward(self, answer=""):
        return answer


def test_request_with_tools_receives_smol_generated_tools():
    """Smol path preserves the older request shape: prompt tools plus OpenAI schemas on the wire."""
    client = MagicMock()
    client.config = {"model": "test/local"}
    model = WriterAgentSmolModel(client, max_tokens=256)
    client.request_with_tools.return_value = {
        "content": "Action:\n{\n  \"name\": \"reply_to_user\",\n  \"arguments\": {\"answer\": \"hi\"}\n}",
        "tool_calls": None,
        "finish_reason": "stop",
        "images": None,
        "usage": {},
    }
    msgs = [ChatMessage(role=MessageRole.USER, content="hello")]
    model.generate(msgs, tools_to_call_from=[_ReplyTool()])
    assert client.request_with_tools.call_count == 1
    tools = client.request_with_tools.call_args.kwargs.get("tools")
    assert tools and tools[0]["type"] == "function"
    assert tools[0]["function"]["name"] == "reply_to_user"
    assert client.request_with_tools.call_args.kwargs.get("model") == "test/local"


def test_smol_path_strips_control_tokens():
    """Explicit test that token stripping happens in the smolagents path via LlmClient."""
    client = MagicMock()
    client.config = {"model": "test/local"}
    model = WriterAgentSmolModel(client, max_tokens=256)

    # LlmClient.request_with_tools already strips control tokens before returning.
    # We test that the smol path receives clean content.
    clean_content = "Hello from librarian!"
    client.request_with_tools.return_value = {
        "content": clean_content,
        "tool_calls": None,
        "finish_reason": "stop",
        "images": None,
        "usage": {},
    }

    msgs = [ChatMessage(role=MessageRole.USER, content="hello")]
    result = model.generate(msgs, tools_to_call_from=[_ReplyTool()])

    assert "<|" not in result.content
    assert clean_content in result.content
    assert client.request_with_tools.call_count == 1
    assert client.request_with_tools.call_args.kwargs.get("tools")


def test_native_tool_calls_are_converted_by_chatmessage():
    """The adapter should rely on ChatMessage.from_dict instead of manual tool-call mapping."""
    client = MagicMock()
    client.config = {"model": "test/local"}
    model = WriterAgentSmolModel(client, max_tokens=256)
    client.request_with_tools.return_value = {
        "content": "",
        "tool_calls": [
            {
                "id": "call_123",
                "type": "function",
                "function": {
                    "name": "reply_to_user",
                    "arguments": '{"answer": "hi"}',
                },
            }
        ],
        "finish_reason": "tool_calls",
        "images": None,
        "usage": {"prompt_tokens": 7, "completion_tokens": 3},
    }

    msgs = [ChatMessage(role=MessageRole.USER, content="hello")]
    result = model.generate(msgs, tools_to_call_from=[_ReplyTool()])

    assert result.tool_calls is not None
    assert result.tool_calls[0].id == "call_123"
    assert result.tool_calls[0].function.name == "reply_to_user"
    assert result.tool_calls[0].function.arguments == '{"answer": "hi"}'
    assert result.token_usage is not None
    assert result.token_usage.input_tokens == 7
    assert result.token_usage.output_tokens == 3


# =============================================================================
# Smol tool adapter tests (from test_smol_tool_adapter.py)
# =============================================================================


def test_to_smol_inputs_librarian_nullable_from_required():
    schema = {
        "type": "object",
        "properties": {
            "a": {"type": "string", "description": "da"},
            "b": {"type": "integer", "description": "db"},
        },
        "required": ["a"],
    }
    inputs = to_smol_inputs(schema, style="librarian")
    assert inputs["a"]["nullable"] is False
    assert inputs["b"]["nullable"] is True
    assert inputs["a"]["type"] == "string"


def test_to_smol_inputs_specialized_preserves_enum_and_default_type():
    schema = {
        "type": "object",
        "properties": {
            "domain": {
                "enum": ["a", "b"],
                "description": "pick",
            },
        },
        "required": ["domain"],
    }
    inputs = to_smol_inputs(schema, style="specialized")
    assert inputs["domain"]["enum"] == ["a", "b"]
    assert inputs["domain"]["type"] == "any"
    assert inputs["domain"]["description"] == "pick"


class _StubTool(ToolBase):
    name = "stub"
    description = "desc"
    parameters = {
        "type": "object",
        "properties": {"p": {"type": "string", "description": "param"}},
        "required": ["p"],
    }

    def execute(self, ctx, **kwargs):
        return {"status": "ok", "p": kwargs.get("p")}

    def is_async(self):
        return False


def test_smol_tool_adapter_unsafe_uses_execute():
    ctx = MagicMock()
    tool = _StubTool()
    tool.execute = MagicMock(return_value={"status": "ok"})
    adapter = SmolToolAdapter(tool, ctx, safe=False, inputs_style="librarian")
    out = adapter.forward(p="v")
    tool.execute.assert_called_once()
    assert out["status"] == "ok"


def test_smol_tool_adapter_safe_async_uses_execute_safe():
    ctx = MagicMock()

    class AsyncTool(_StubTool):
        def is_async(self):
            return True

    tool = AsyncTool()
    tool.execute_safe = MagicMock(return_value={"status": "ok"})
    adapter = SmolToolAdapter(tool, ctx, safe=True, main_thread_sync=True, inputs_style="specialized")
    adapter.forward(p="x")
    tool.execute_safe.assert_called_once()


@patch("plugin.chatbot.smol_agent.ToolCallingAgent")
@patch("plugin.chatbot.smol_agent.WriterAgentSmolModel")
@patch("plugin.chatbot.smol_agent.LlmClient")
@patch("plugin.chatbot.smol_agent.get_config_int")
@patch("plugin.chatbot.smol_agent.get_api_config")
def test_build_toolcalling_agent_wires_max_tokens_and_steps(
    mock_get_api, mock_get_int, mock_llm, mock_wsm, mock_tca
):
    mock_get_api.return_value = {"model": "test/model"}

    def _int(_ctx, key: str) -> int:
        if key == "chat_max_tokens":
            return 512
        if key == "chat_max_tool_rounds":
            return 12
        raise AssertionError(key)

    mock_get_int.side_effect = _int

    class Tiny(Tool):
        name = "tiny"
        description = "d"
        inputs = {"a": {"type": "string", "description": "d", "nullable": True}}
        output_type = "string"

        def forward(self, a=""):
            return a

    ctx = MagicMock()
    ctx.ctx = MagicMock()
    build_toolcalling_agent(
        ctx,
        [Tiny()],
        instructions="inst",
        final_answer_tool_name="reply_to_user",
        examples_block="examples",
        status_callback=None,
    )
    mock_llm.assert_called_once_with({"model": "test/model"}, ctx.ctx)
    assert mock_wsm.call_args.kwargs["max_tokens"] == 512
    tca_kw = mock_tca.call_args.kwargs
    assert tca_kw["max_steps"] == 12
    assert tca_kw["instructions"] == "inst"
    assert tca_kw["final_answer_tool_name"] == "reply_to_user"
    assert tca_kw["system_prompt_examples"] == "examples"


# =============================================================================
# Librarian smol tests (from test_librarian_smol.py)
# =============================================================================


class TestLibrarianSmol(unittest.TestCase):
    def test_tool_adapter_initialization(self):
        ctx = MagicMock()
        ctx.ctx = MagicMock()

        memory_tool = MemoryTool()
        switch_tool = SwitchToDocumentModeTool()

        smol_memory = SmolToolAdapter(memory_tool, ctx, safe=False, inputs_style="librarian")
        smol_switch = SmolToolAdapter(switch_tool, ctx, safe=False, inputs_style="librarian")

        # Verify that they are instances of smolagents.tools.BaseTool
        from plugin.contrib.smolagents.tools import BaseTool

        self.assertTrue(isinstance(smol_memory, BaseTool))
        self.assertTrue(isinstance(smol_switch, BaseTool))

        # Verify inputs conversion
        self.assertIn("key", smol_memory.inputs)
        self.assertIn("content", smol_memory.inputs)
        self.assertEqual(smol_memory.inputs["key"]["type"], "string")
        self.assertEqual(smol_memory.inputs["content"]["type"], "string")

        # Verify forward call
        memory_tool.execute = MagicMock(return_value={"status": "ok"})
        smol_memory.forward(key="favorite_color", content="blue")
        memory_tool.execute.assert_called_once()
        args, kwargs = memory_tool.execute.call_args
        self.assertEqual(kwargs["key"], "favorite_color")
        self.assertEqual(kwargs["content"], "blue")

    def test_agent_initialization_with_adapted_tools(self):
        ctx = MagicMock()
        ctx.ctx = MagicMock()
        model = MagicMock()

        tools = [
            SmolToolAdapter(MemoryTool(), ctx, safe=False, inputs_style="librarian"),
            SmolToolAdapter(SwitchToDocumentModeTool(), ctx, safe=False, inputs_style="librarian"),
        ]

        # This shouldn't raise "All elements must be instance of BaseTool"
        agent = ToolCallingAgent(
            tools=tools,
            model=model
        )
        self.assertEqual(len(agent.tools), 3)  # memory, switch, final_answer
        self.assertIn("upsert_memory", agent.tools)
        self.assertIn("switch_to_document_mode", agent.tools)

    def test_switch_mode_extraction(self):
        ctx = MagicMock()
        ctx.ctx = MagicMock()
        ctx.stop_checker.return_value = False

        # Mock ToolCallingAgent to simulate a switch_mode observation
        with patch("plugin.chatbot.smol_agent.ToolCallingAgent") as mock_agent_class:
            mock_agent = mock_agent_class.return_value

            # Simulate steps: one ActionStep with switch_mode
            step1 = ActionStep(step_number=1, timing=Timing(start_time=time.time()))
            step1.observations = "{'status': 'switch_mode', 'message': 'See you in document mode!'}"

            def _switch_then_fail_gen():
                yield step1
                raise AssertionError(
                    "Librarian must stop after switch_mode without consuming further smol steps"
                )

            mock_agent.run.return_value = _switch_then_fail_gen()

            tool = LibrarianOnboardingTool()
            res = tool.execute(ctx, query="switch me")
            if res["status"] == "error":
                print(f"DEBUG: Tool execution error: {res.get('message')}")

            self.assertEqual(res["status"], "switch_mode")
            self.assertEqual(res["result"], "See you in document mode!")

    def test_upsert_memory_calls_chat_append_callback(self):
        ctx = MagicMock()
        ctx.ctx = MagicMock()
        ctx.stop_checker.return_value = False
        chat_append = MagicMock()
        ctx.chat_append_callback = chat_append

        tc = ToolCall(
            name="upsert_memory",
            arguments={"key": "nickname", "content": "Bob"},
            id="c1",
        )
        fa = FinalAnswerStep(output="Hello")

        with patch("plugin.chatbot.smol_agent.ToolCallingAgent") as mock_agent_class:
            mock_agent = mock_agent_class.return_value
            mock_agent.run.return_value = [tc, fa]

            tool = LibrarianOnboardingTool()
            res = tool.execute(ctx, query="hi")

        self.assertEqual(res.get("status"), "ok")
        self.assertNotEqual(res.get("status"), "switch_mode")
        chat_append.assert_called_once()
        line = chat_append.call_args[0][0]
        self.assertIn("Memory update", line)
        self.assertIn("nickname", line)
        self.assertIn("Bob", line)


# =============================================================================
# Librarian handoff tests (from test_librarian_handoff.py)
# =============================================================================


def test_format_grammar_status_complete() -> None:
    text = format_grammar_status(
        {
            "phase": "complete",
            "preview": "This are bad",
            "length": 42,
            "result": "1 issue",
            "elapsed_ms": 812,
        }
    )

    assert text == "Grammar: done 'This are bad' len 42: 1 issue, 812ms"


def test_do_send_enters_librarian_when_user_memory_missing():
    listener = _make_listener(in_librarian_mode=False)

    with patch("plugin.chatbot.panel.update_activity_state"), patch(
        "plugin.chatbot.dialogs.get_control_text", return_value="Hello"
    ), patch("plugin.chatbot.dialogs.set_control_text"), patch(
        "plugin.framework.config.get_config", return_value=None
    ), patch(
        "plugin.agent_backend.registry.normalize_backend_id", return_value="builtin"
    ), patch("plugin.chatbot.memory.MemoryStore") as mock_store:
        mock_store.return_value.read.return_value = ""
        SendButtonListener._do_send(listener)

    assert listener._in_librarian_mode is True
    listener._run_librarian.assert_called_once()
    listener._do_send_chat_with_tools.assert_not_called()


def test_do_send_stays_in_librarian_mode_without_rechecking_memory():
    listener = _make_listener(in_librarian_mode=True)

    with patch("plugin.chatbot.panel.update_activity_state"), patch(
        "plugin.chatbot.dialogs.get_control_text", return_value="Hello again"
    ), patch("plugin.chatbot.dialogs.set_control_text"), patch(
        "plugin.framework.config.get_config", return_value=None
    ), patch(
        "plugin.agent_backend.registry.normalize_backend_id", return_value="builtin"
    ), patch("plugin.chatbot.memory.MemoryStore") as mock_store:
        SendButtonListener._do_send(listener)

    assert listener._in_librarian_mode is True
    mock_store.assert_not_called()
    listener._run_librarian.assert_called_once()
    listener._do_send_chat_with_tools.assert_not_called()


def test_do_send_uses_document_chat_after_librarian_flag_clears():
    listener = _make_listener(in_librarian_mode=False)

    with patch("plugin.chatbot.panel.update_activity_state"), patch(
        "plugin.chatbot.dialogs.get_control_text", return_value="Work on the document"
    ), patch("plugin.chatbot.dialogs.set_control_text"), patch(
        "plugin.framework.config.get_config", return_value=None
    ), patch(
        "plugin.agent_backend.registry.normalize_backend_id", return_value="builtin"
    ), patch("plugin.chatbot.memory.MemoryStore") as mock_store:
        mock_store.return_value.read.return_value = '{"name": "Keith"}'
        SendButtonListener._do_send(listener)

    assert listener._in_librarian_mode is False
    listener._run_librarian.assert_not_called()
    listener._do_send_chat_with_tools.assert_called_once()
