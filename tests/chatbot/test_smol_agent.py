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
        chat_mode_selector=None,
        sidebar_include_brainstorming=True,
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

    def test_system_prompt_no_image_transformer(self):
        from plugin.contrib.smolagents.toolcalling_agent_prompts import SYSTEM_PROMPT_TEMPLATE

        self.assertNotIn("image_transformer", SYSTEM_PROMPT_TEMPLATE)

    def test_get_examples_block_delegate_uses_specialized_workflow_finished(self):
        from plugin.chatbot.smol_examples import get_examples_block

        block = get_examples_block("writer:shapes")
        self.assertIn("specialized_workflow_finished", block)
        self.assertNotIn('"name": "final_answer"', block)
        self.assertIn("web_search", block)

    def test_get_examples_block_web_research_uses_final_answer(self):
        from plugin.chatbot.smol_examples import get_examples_block

        block = get_examples_block("web_research")
        self.assertIn('"name": "final_answer"', block)
        self.assertNotIn("specialized_workflow_finished", block)

    def test_get_examples_block_librarian_uses_reply_to_user(self):
        from plugin.chatbot.smol_examples import get_examples_block

        block = get_examples_block("librarian")
        self.assertIn("reply_to_user", block)
        self.assertNotIn("specialized_workflow_finished", block)

    def test_get_examples_block_python_uses_sympy_venv_script(self):
        from plugin.chatbot.smol_examples import PYTHON_SPECIALIZED_EXAMPLES, get_examples_block

        block = get_examples_block("calc:python")
        self.assertEqual(block, PYTHON_SPECIALIZED_EXAMPLES)
        self.assertIn("run_venv_python_script", block)
        self.assertIn("sp.prime(1010)", block)
        self.assertIn("SciPy", block)
        self.assertIn("DO NOT import numpy", block)
        self.assertNotIn('"code": "import', block)
        self.assertIn("specialized_workflow_finished", block)

    def test_specialized_agent_prompt_examples_use_finish_tool_name(self):
        from plugin.contrib.smolagents.agents import ToolCallingAgent
        from plugin.contrib.smolagents.toolcalling_agent_prompts import DELEGATE_GENERIC_EXAMPLES_BLOCK
        from plugin.chatbot.smol_examples import get_examples_block

        model = MagicMock()
        agent = ToolCallingAgent(
            tools=[],
            model=model,
            system_prompt_examples=get_examples_block("calc:charts"),
            final_answer_tool_name="specialized_workflow_finished",
        )
        prompt = agent.initialize_system_prompt()
        self.assertIn("specialized_workflow_finished", prompt)
        self.assertEqual(get_examples_block("calc:charts"), DELEGATE_GENERIC_EXAMPLES_BLOCK)


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
            "optional_param": {
                "type": "string",
                "description": "optional",
            },
        },
        "required": ["domain"],
    }
    inputs = to_smol_inputs(schema, style="specialized")
    assert inputs["domain"]["enum"] == ["a", "b"]
    assert inputs["domain"]["type"] == "any"
    assert inputs["domain"]["description"] == "pick"
    assert inputs["domain"]["nullable"] is False
    assert inputs["optional_param"]["nullable"] is True



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


def test_smol_tool_adapter_handles_positional_arguments():
    ctx = MagicMock()
    tool = _StubTool()
    tool.execute = MagicMock(return_value={"status": "ok"})
    adapter = SmolToolAdapter(tool, ctx, safe=False, inputs_style="librarian")

    # Positional string argument should map to the first input key 'p'
    out = adapter.forward("positional_value")
    tool.execute.assert_called_once()
    _, kwargs = tool.execute.call_args
    assert kwargs.get("p") == "positional_value"

    # Positional dict argument should be merged
    tool.execute.reset_mock()
    out = adapter.forward({"p": "dict_value", "extra": 42})
    tool.execute.assert_called_once()
    _, kwargs = tool.execute.call_args
    assert kwargs.get("p") == "dict_value"
    assert kwargs.get("extra") == 42


def test_smol_tool_adapter_resolves_dynamic_parameters():
    class DynamicTool(_StubTool):
        def get_parameters(self, doc_type):
            if doc_type == "writer":
                return {
                    "type": "object",
                    "properties": {"writer_param": {"type": "string"}},
                    "required": ["writer_param"],
                }
            return self.parameters

    ctx = MagicMock()
    ctx.doc_type = "writer"
    tool = DynamicTool()
    adapter = SmolToolAdapter(tool, ctx, safe=False, inputs_style="specialized")
    
    assert "writer_param" in adapter.inputs
    assert "p" not in adapter.inputs


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
        if key == "chatbot.max_tool_rounds":
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
    mock_llm.assert_called_once_with({"model": "test/model"}, ctx.ctx, cancellation_scope=ctx.send_cancellation)
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

    def test_librarian_onboarding_tool_passes_existing_memory_to_instructions(self):
        ctx = MagicMock()
        ctx.ctx = MagicMock()
        ctx.stop_checker.return_value = False

        fa = FinalAnswerStep(output="Hello")

        with patch("plugin.chatbot.memory.MemoryStore") as mock_store_class, \
             patch("plugin.chatbot.smol_agent.ToolCallingAgent") as mock_agent_class:
            mock_store = mock_store_class.return_value
            mock_store.read.return_value = '{"favorite_color": "blue", "name": "Alice"}'
            
            mock_agent = mock_agent_class.return_value
            mock_agent.run.return_value = [fa]

            tool = LibrarianOnboardingTool()
            res = tool.execute(ctx, query="hi")
            
            self.assertTrue(mock_agent_class.called)
            kwargs = mock_agent_class.call_args.kwargs
            self.assertIn("instructions", kwargs)
            self.assertIn("[USER PROFILE / MEMORY]", kwargs["instructions"])
            self.assertIn('{"favorite_color": "blue", "name": "Alice"}', kwargs["instructions"])


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

    assert text == "Grammar: done 'This are b…' len 42: 1 issue, 812ms"


def test_format_grammar_status_done_matches_complete_line() -> None:
    """Worker batch completion uses phase ``done``; sidebar should show result suffix like ``complete``."""
    text = format_grammar_status(
        {
            "phase": "done",
            "preview": "The quick brown fox jumps.",
            "length": 120,
            "result": "2 issues, 2 sentences",
            "elapsed_ms": 400,
        }
    )
    assert text == "Grammar: done 'The quick …' len 120: 2 issues, 2 sentences, 400ms"


def test_format_grammar_status_request_language_detect() -> None:
    text = format_grammar_status(
        {
            "phase": "request",
            "preview": "Hello world",
            "length": 11,
            "result": "Detecting language",
        }
    )
    assert text == "Language: detecting 'Hello worl…' len 11"


def test_format_grammar_status_request_grammar_checking() -> None:
    text = format_grammar_status(
        {
            "phase": "request",
            "preview": "Hello world",
            "length": 11,
            "result": "LLM request",
        }
    )
    assert text == "Grammar: checking 'Hello worl…' len 11"


def test_format_grammar_status_failed_language_vs_grammar() -> None:
    lang = format_grammar_status(
        {
            "phase": "failed",
            "preview": "Language detection",
            "length": 19,
            "result": "TimeoutError",
        }
    )
    assert lang == "Language: failed 'Language d…' len 19: TimeoutError"

    grm = format_grammar_status(
        {
            "phase": "failed",
            "preview": "Grammar check",
            "length": 13,
            "result": "ValueError",
        }
    )
    assert grm == "Grammar: failed 'Grammar ch…' len 13: ValueError"


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


class TestSmolMixedToolCalls(unittest.TestCase):
    def test_mixing_regular_tool_and_final_answer_tool_succeeds(self):
        """Verify that we can now mix a regular tool call and a final answer tool call in one turn."""
        from plugin.contrib.smolagents.memory import ActionStep
        from plugin.contrib.smolagents.models import (
            ChatMessage,
            ChatMessageToolCall,
            ChatMessageToolCallFunction,
        )
        from plugin.contrib.smolagents.agents import ActionOutput

        # 1. Setup tools
        ctx = MagicMock()
        stub_tool = _StubTool()
        adapted_stub = SmolToolAdapter(stub_tool, ctx, safe=False)

        # 2. Setup agent
        model = MagicMock()
        agent = ToolCallingAgent(
            tools=[adapted_stub], model=model, final_answer_tool_name="reply_to_user"
        )

        # 3. Mock model to return BOTH calls
        chat_msg = ChatMessage(
            role=MessageRole.ASSISTANT,
            content="",
            tool_calls=[
                ChatMessageToolCall(
                    id="call_1",
                    type="function",
                    function=ChatMessageToolCallFunction(
                        name="stub", arguments={"p": "value1"}
                    ),
                ),
                ChatMessageToolCall(
                    id="call_2",
                    type="function",
                    function=ChatMessageToolCallFunction(
                        name="reply_to_user", arguments={"answer": "Finished!"}
                    ),
                ),
            ],
        )
        model.generate.return_value = chat_msg

        # 4. Run the agent stream for one step
        memory_step = ActionStep(step_number=1, timing=Timing(start_time=time.time()))

        # Before our fix, this would raise AgentExecutionError
        outputs = list(agent._step_stream(memory_step))

        # 5. Verify outputs
        # Should have ActionOutput with is_final_answer=True
        final_outputs = [o for o in outputs if isinstance(o, ActionOutput)]
        self.assertEqual(len(final_outputs), 1)
        self.assertTrue(final_outputs[0].is_final_answer)
        self.assertEqual(final_outputs[0].output, "Finished!")


class _WebSearchStubTool(ToolBase):
    name = "web_search"
    description = "Search the web"
    parameters = {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "query"}},
        "required": ["query"],
    }

    def execute(self, ctx, **kwargs):
        return {"status": "ok", "query": kwargs.get("query")}

    def is_async(self):
        return False


class TestSmolCallingToolsEchoParsing(unittest.TestCase):
    def test_calling_tools_python_repr_parsed_as_web_search_not_final_answer(self):
        """Regression: memory-style 'Calling tools:' + Python repr must not become final_answer."""
        from plugin.contrib.smolagents.agents import ActionOutput, ToolOutput
        from plugin.contrib.smolagents.models import Model

        mimicked = (
            "Calling tools:\n"
            "[{'id': 'call_019e8619a1967b11b7fccbbf', 'type': 'function', "
            "'function': {'name': 'web_search', 'arguments': {'query': 'test query'}}}]"
        )

        class _MinimalModel(Model):
            def generate(self, messages, **kwargs):
                raise NotImplementedError

        model = _MinimalModel(model_id="test")
        ctx = MagicMock()
        web_tool = SmolToolAdapter(_WebSearchStubTool(), ctx, safe=False)
        agent = ToolCallingAgent(
            tools=[web_tool],
            model=model,
            final_answer_tool_name="final_answer",
        )

        chat_msg = ChatMessage(
            role=MessageRole.ASSISTANT,
            content=mimicked,
            tool_calls=None,
        )
        model.generate = MagicMock(return_value=chat_msg)

        memory_step = ActionStep(step_number=1, timing=Timing(start_time=time.time()))
        outputs = list(agent._step_stream(memory_step))

        tool_outputs = [o for o in outputs if isinstance(o, ToolOutput)]
        final_outputs = [o for o in outputs if isinstance(o, ActionOutput)]
        self.assertEqual(len(tool_outputs), 1)
        self.assertFalse(final_outputs[0].is_final_answer if final_outputs else True)
        self.assertEqual(memory_step.tool_calls[0].name, "web_search")
        self.assertEqual(memory_step.tool_calls[0].arguments, {"query": "test query"})


class TestSmolImplicitFinalAnswerJson(unittest.TestCase):
    def test_answer_only_json_blob_becomes_final_answer_not_double_wrapped(self):
        """Regression: Mercury-style {\"answer\": [html...]} must not wrap raw JSON as final_answer."""
        from plugin.contrib.smolagents.agents import ActionOutput
        from plugin.contrib.smolagents.default_tools import FinalAnswerTool
        from plugin.contrib.smolagents.models import Model

        mercury_blob = '{"answer": ["<h1>Title</h1>", "<p>Para</p>"]}'

        class _MinimalModel(Model):
            def generate(self, messages, **kwargs):
                raise NotImplementedError

        model = _MinimalModel(model_id="test")
        model.generate = MagicMock(
            return_value=ChatMessage(
                role=MessageRole.ASSISTANT,
                content=mercury_blob,
                tool_calls=None,
            )
        )

        agent = ToolCallingAgent(
            tools=[FinalAnswerTool()],
            model=model,
            final_answer_tool_name="final_answer",
        )

        memory_step = ActionStep(step_number=1, timing=Timing(start_time=time.time()))
        outputs = list(agent._step_stream(memory_step))

        final_outputs = [o for o in outputs if isinstance(o, ActionOutput)]
        self.assertEqual(len(final_outputs), 1)
        self.assertTrue(final_outputs[0].is_final_answer)
        self.assertEqual(final_outputs[0].output, "<h1>Title</h1>\n<p>Para</p>")
        self.assertNotIn('{"answer"', str(final_outputs[0].output))
