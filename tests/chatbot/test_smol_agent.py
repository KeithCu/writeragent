# WriterAgent - combined tests for smolagents functionality

from types import SimpleNamespace
import time
from unittest.mock import MagicMock, patch
import pytest

# =============================================================================
# Imports for all test groups
# =============================================================================

from plugin.chatbot.smol_agent import (
    SmolToolAdapter,
    WriterAgentSmolModel,
    build_toolcalling_agent,
    to_smol_inputs,
)
from plugin.chatbot.librarian import (
    LibrarianOnboardingTool,
)
from plugin.chatbot.chat_sidebar_mode import CHAT_MODE_LIBRARIAN, require_sidebar_mode
from plugin.chatbot.sticky_reply import StickyReplyToUserTool
from plugin.chatbot.memory import MemoryTool
from plugin.contrib.smolagents.agents import ToolCallingAgent
from plugin.contrib.smolagents.memory import ActionStep, FinalAnswerStep, ToolCall
from plugin.contrib.smolagents.models import ChatMessage, MessageRole
from plugin.contrib.smolagents.monitoring import Timing
from plugin.contrib.smolagents.tools import Tool
from plugin.framework.tool import ToolBase
from tests.harness.smol_messages import flatten_messages


# =============================================================================
# ToolCallingAgent prompt examples tests (from test_toolcalling_prompt_examples.py)
# =============================================================================


class TestToolcallingPromptExamples:
    def test_custom_system_prompt_examples_appear_in_rendered_prompt(self):
        model = MagicMock()
        marker = "CUSTOM_EXAMPLES_MARKER_XYZ123"
        agent = ToolCallingAgent(tools=[], model=model, system_prompt_examples=marker)
        prompt = agent.initialize_system_prompt()
        assert (marker) in (prompt)
        assert ("__EXAMPLES_BLOCK__") not in (prompt)

    def test_get_examples_block_delegate_uses_specialized_workflow_finished(self):
        from plugin.chatbot.smol_examples import get_examples_block

        block = get_examples_block("writer:shapes")
        assert '"name": "workflow_finished"' in block
        assert "specialized_workflow_finished" not in block
        assert '"name": "final_answer"' not in block

    def test_get_examples_block_web_research_uses_final_answer(self):
        from plugin.chatbot.smol_examples import get_examples_block

        block = get_examples_block("web_research")
        assert ('"name": "final_answer"') in (block)
        assert ("workflow_finished") not in (block)

    def test_get_examples_block_web_research_matches_parallel_flag(self, monkeypatch):
        from plugin.chatbot import web_pages
        from plugin.chatbot.smol_examples import get_examples_block

        monkeypatch.setattr(web_pages, "PARALLEL_WEB_FETCH", True)
        parallel_block = get_examples_block("web_research")
        assert '"urls"' in parallel_block
        assert '"arguments": {"urls":' in parallel_block

        monkeypatch.setattr(web_pages, "PARALLEL_WEB_FETCH", False)
        serial_block = get_examples_block("web_research")
        assert '"urls"' not in serial_block
        assert '"arguments": {"url": "https://docs.python.org/3/whatsnew/3.13.html"}' in serial_block

    def test_get_examples_block_ppt_master_finishes_the_deck(self):
        from plugin.chatbot.smol_examples import get_examples_block

        block = get_examples_block("ppt-master")
        assert "ppt_master_finished" in block
        assert "final_answer" not in block

    def test_get_examples_block_librarian_uses_reply_to_user(self):
        from plugin.chatbot.smol_examples import get_examples_block

        block = get_examples_block("librarian")
        assert ("reply_to_user") in (block)
        assert ("workflow_finished") not in (block)

    @pytest.mark.parametrize(
        ("key", "expected_key"),
        [
            ("calc:images", "writer:images"),
            ("draw:images", "writer:images"),
            ("calc:python", "writer:python"),
            ("draw:python", "writer:python"),
        ],
    )
    def test_get_examples_block_app_prefixes_share_domain_examples(self, key, expected_key):
        from plugin.chatbot.smol_examples import get_examples_block

        assert get_examples_block(key) == get_examples_block(expected_key)

    def test_specialized_agent_prompt_examples_use_finish_tool_name(self):
        from plugin.chatbot.smol_examples import DELEGATE_GENERIC_EXAMPLES_BLOCK, get_examples_block

        model = MagicMock()
        agent = ToolCallingAgent(
            tools=[],
            model=model,
            system_prompt_examples=get_examples_block("calc:charts"),
            final_answer_tool_name="workflow_finished",
        )
        prompt = agent.initialize_system_prompt()
        assert '"name": "workflow_finished"' in prompt
        assert get_examples_block("calc:charts") == DELEGATE_GENERIC_EXAMPLES_BLOCK

    def test_examples_action_bodies_parse_and_use_allowed_tools(self):
        import json
        from typing import Any
        from plugin.chatbot.smol_examples import get_examples_block

        decoder = json.JSONDecoder()

        dedicated_specs = {
            "librarian": {
                "allowed": {"memory_upsert", "reply_to_user"},
                "finish": "reply_to_user",
            },
            "brainstorming": {
                "allowed": {"get_tree", "brainstorming_save_design_spec", "reply_to_user"},
                "finish": "reply_to_user",
            },
            "writing_plan": {
                "allowed": {"writing_plan_write_section", "reply_to_user"},
                "finish": "reply_to_user",
            },
            "deep_research": {
                "allowed": {"deep_research_search_web", "apply_content", "reply_to_user"},
                "finish": "reply_to_user",
            },
            "web_research": {
                "allowed": {"web_search", "visit_webpage", "read_page", "find_in_page", "final_answer"},
                "finish": "final_answer",
            },
            "ppt-master": {
                "allowed": {"validate_ppt_master_project", "export_presentation_project", "ppt_master_finished"},
                "finish": "ppt_master_finished",
            },
            "writer:python": {
                "allowed": {"python_run_script", "python_delegate_domains", "workflow_finished"},
                "finish": "workflow_finished",
            },
            "writer:images": {
                "allowed": {"image_generate", "workflow_finished"},
                "finish": "workflow_finished",
            },
            "domain_tools:shapes": {
                "allowed": {"python_run_script", "workflow_finished"},
                "finish": "workflow_finished",
            },
        }

        def parse_actions(block: str) -> list[dict[str, Any]]:
            actions = []
            idx = 0
            while True:
                pos = block.find("Action:", idx)
                if pos == -1:
                    break
                json_start = block.find("{", pos + len("Action:"))
                assert json_start != -1
                obj, end_pos = decoder.raw_decode(block[json_start:])
                actions.append(obj)
                idx = json_start + end_pos
            return actions

        for key, spec in dedicated_specs.items():
            block = get_examples_block(key)
            actions = parse_actions(block)
            assert len(actions) > 0
            for action in actions:
                assert "name" in action
                assert "arguments" in action
                assert action["name"] in spec["allowed"], f"Tool {action['name']} not allowed in {key}"
                if action["name"] != "final_answer":
                    assert isinstance(action["arguments"], dict), f"Tool {action['name']} arguments must be dict in {key}"
                else:
                    assert isinstance(action["arguments"], (dict, str))
            assert actions[-1]["name"] == spec["finish"], f"Last action in {key} must be {spec['finish']}"

        # domain_tools:shapes must not contain python_delegate_domains
        shapes_block = get_examples_block("domain_tools:shapes")
        assert "python_delegate_domains" not in shapes_block

        # Generic / non-shapes domain_tools:*
        for generic_key in ("domain_tools:calc", "writer:shapes", "unknown_custom_domain:foo"):
            block = get_examples_block(generic_key)
            actions = parse_actions(block)
            assert len(actions) > 0
            assert actions[-1]["name"] == "workflow_finished"

        # Unknown key falls back to generic examples
        unknown_block = get_examples_block("completely_unknown_key")
        unknown_actions = parse_actions(unknown_block)
        assert unknown_actions[-1]["name"] == "workflow_finished"


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
    with patch("plugin.framework.queue_executor.main.call") as mock_main:
        mock_main.side_effect = lambda fn, *args, **kwargs: fn(*args, **kwargs)
        out = adapter.forward(p="v")
    mock_main.assert_called_once()
    assert mock_main.call_args[0][0] is tool.execute
    tool.execute.assert_called_once()
    assert out["status"] == "ok"


def test_smol_tool_adapter_safe_async_uses_execute_safe():
    ctx = MagicMock()

    class AsyncTool(_StubTool):
        def is_async(self):
            return True

    tool = AsyncTool()
    tool.execute_safe = MagicMock(return_value={"status": "ok"})
    adapter = SmolToolAdapter(tool, ctx, safe=True, inputs_style="specialized")
    with patch("plugin.framework.queue_executor.main.call") as mock_main:
        adapter.forward(p="x")
    mock_main.assert_not_called()
    tool.execute_safe.assert_called_once()


def test_smol_tool_adapter_handles_positional_arguments():
    ctx = MagicMock()
    tool = _StubTool()
    tool.execute = MagicMock(return_value={"status": "ok"})
    adapter = SmolToolAdapter(tool, ctx, safe=False, inputs_style="librarian")

    with patch("plugin.framework.queue_executor.main.call") as mock_main:
        mock_main.side_effect = lambda fn, *args, **kwargs: fn(*args, **kwargs)
        # Positional string argument should map to the first input key 'p'
        adapter.forward("positional_value")
        tool.execute.assert_called_once()
        _, kwargs = tool.execute.call_args
        assert kwargs.get("p") == "positional_value"
        mock_main.assert_called_once()
        assert mock_main.call_args[0][0] is tool.execute

        # Positional dict argument should be merged
        tool.execute.reset_mock()
        mock_main.reset_mock()
        adapter.forward({"p": "dict_value", "extra": 42})
        tool.execute.assert_called_once()
        _, kwargs = tool.execute.call_args
        assert kwargs.get("p") == "dict_value"
        # Unknown keys are dropped and reported, as on ToolRegistry.execute.
        assert "extra" not in kwargs
        mock_main.assert_called_once()


class _EnumTool(_StubTool):
    name = "enum_stub"
    parameters = {
        "type": "object",
        "properties": {
            "position": {"type": "string", "enum": ["before", "after", "end"]},
            "level": {"type": "integer"},
            "note": {"type": "string"},
        },
        "required": [],
    }


def test_smol_tool_adapter_rejects_bad_enum_like_registry():
    """forward runs ToolRegistry's validation: a bad enum never reaches execute."""
    from plugin.framework.tool import ToolRegistry

    ctx = SimpleNamespace(read_only_target=False, caller="specialized", doc_type="writer", uno_services_supported=None)
    tool = _EnumTool()
    tool.execute = MagicMock(return_value={"status": "ok"})
    tool.execute_safe = MagicMock(return_value={"status": "ok"})
    adapter = SmolToolAdapter(tool, ctx, safe=True, inputs_style="specialized")
    with patch("plugin.framework.queue_executor.main.call") as mock_main:
        out = adapter.forward(position="start")
    mock_main.assert_not_called()
    tool.execute.assert_not_called()
    tool.execute_safe.assert_not_called()
    assert out["code"] == "VALIDATION_ERROR"
    assert "position" in out["message"]

    registry = ToolRegistry(services=MagicMock())
    registry.register(tool)
    assert registry.execute("enum_stub", ctx, position="start") == out
    tool.execute_safe.assert_not_called()


def test_smol_tool_adapter_valid_call_runs_with_coerced_args():
    ctx = SimpleNamespace(read_only_target=False, caller="specialized", doc_type="writer")
    tool = _EnumTool()
    tool.execute_safe = MagicMock(return_value={"status": "ok"})
    adapter = SmolToolAdapter(tool, ctx, safe=True, inputs_style="specialized")
    with patch("plugin.framework.queue_executor.main.call") as mock_main:
        mock_main.side_effect = lambda fn, *args, **kwargs: fn(*args, **kwargs)
        out = adapter.forward(position="end", level=2.0, note=None, bogus=1)
    tool.execute_safe.assert_called_once_with(ctx, position="end", level=2)
    assert type(tool.execute_safe.call_args.kwargs["level"]) is int
    assert out == {"status": "ok", "ignored_parameters": ["bogus"]}


def test_smol_tool_adapter_read_only_target_rejects_mutation():
    """forward skips ToolRegistry.execute, so the read-only flag must be checked here."""
    ctx = SimpleNamespace(read_only_target=True, caller="document_research", doc_type="writer")

    class AsyncMutator(_StubTool):
        name = "get_content"
        is_mutation = True

        def is_async(self):
            return True

    for tool_cls in (_StubTool, AsyncMutator):
        tool = tool_cls()
        tool.execute = MagicMock(return_value={"status": "ok"})
        tool.execute_safe = MagicMock(return_value={"status": "ok"})
        adapter = SmolToolAdapter(tool, ctx, safe=False, inputs_style="librarian")
        with patch("plugin.framework.queue_executor.main.call") as mock_main:
            out = adapter.forward(p="v")
        mock_main.assert_not_called()
        tool.execute.assert_not_called()
        tool.execute_safe.assert_not_called()
        assert (out["status"]) == ("error")
        assert (out["code"]) == ("READ_ONLY_TARGET")
        assert ("read-only") in (out["message"])
        assert (out["details"]["tool_name"]) == (tool.name)
        assert (out["details"]["caller"]) == ("document_research")
        assert (out["details"]["doc_type"]) == ("writer")


def test_smol_tool_adapter_read_only_target_allows_non_mutation():
    class FlaggedRead(_StubTool):
        name = "apply_fake"
        is_mutation = False

    ctx = SimpleNamespace(read_only_target=True, caller=None, doc_type="writer")
    tool = FlaggedRead()
    tool.execute = MagicMock(return_value={"status": "ok"})
    adapter = SmolToolAdapter(tool, ctx, safe=False, inputs_style="librarian")
    with patch("plugin.framework.queue_executor.main.call") as mock_main:
        mock_main.side_effect = lambda fn, *args, **kwargs: fn(*args, **kwargs)
        out = adapter.forward(p="v")
    tool.execute.assert_called_once()
    assert (out["status"]) == ("ok")


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


@patch("plugin.chatbot.smol_agent.WriterAgentToolCallingAgent")
@patch("plugin.chatbot.smol_agent.WriterAgentSmolModel")
@patch("plugin.chatbot.smol_agent.LlmClient")
@patch("plugin.chatbot.smol_agent.get_config")
@patch("plugin.chatbot.smol_agent.get_api_config")
def test_build_toolcalling_agent_wires_max_tokens_and_steps(
    mock_get_api, mock_get_int, mock_llm, mock_wsm, mock_tca
):
    mock_get_api.return_value = {"model": "test/model"}

    def _int(key: str) -> int:
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
    )
    mock_llm.assert_called_once_with({"model": "test/model"}, ctx.ctx, cancel=ctx.send_cancellation)
    assert mock_wsm.call_args.kwargs["max_tokens"] == 512
    tca_kw = mock_tca.call_args.kwargs
    assert tca_kw["max_steps"] == 12
    assert tca_kw["instructions"] == "inst"
    assert tca_kw["final_answer_tool_name"] == "reply_to_user"
    assert tca_kw["system_prompt_examples"] == "examples"


def test_create_smol_model_reads_chat_max_tokens_and_explicit_max_tokens_wins():
    from plugin.chatbot.smol_agent import create_smol_model

    ctx = MagicMock()
    ctx.ctx = MagicMock()
    ctx.send_cancellation = MagicMock()
    ctx.status_callback = MagicMock()

    with patch("plugin.chatbot.smol_agent.get_api_config", return_value={"model": "m"}), \
         patch("plugin.chatbot.smol_agent.get_config", return_value=2048), \
         patch("plugin.chatbot.smol_agent.LlmClient"), \
         patch("plugin.chatbot.smol_agent.WriterAgentSmolModel") as mock_wsm:

        create_smol_model(ctx)
        assert mock_wsm.call_args.kwargs["max_tokens"] == 2048

        create_smol_model(ctx, max_tokens=4096)
        assert mock_wsm.call_args.kwargs["max_tokens"] == 4096


def test_run_smol_side_turn_trims_history_and_reports_document_opens():
    from plugin.chatbot.smol_agent import _document_open_tool_handler, run_smol_side_turn
    from plugin.chatbot.smol_examples import get_examples_block

    ctx = MagicMock()
    ctx.ctx = MagicMock()
    executor = MagicMock()
    executor.execute_safe.return_value = "<p>kept</p>"
    with patch("plugin.chatbot.smol_agent.build_toolcalling_agent", return_value=MagicMock()) as mock_build, patch(
        "plugin.chatbot.smol_agent.SmolAgentExecutor", return_value=executor
    ):
        out = run_smol_side_turn(
            ctx,
            query="hello",
            history_text="h" * 4001,
            collector=lambda c: [],
            instructions="inst",
            examples_key="brainstorming",
            tool_call_handler=_document_open_tool_handler(ctx),
            status_message="Brainstorming...",
            stop_message="Brainstorming stopped by user.",
            error_prefix="Brainstorming failed",
        )

    assert out == {"status": "ok", "result": "<p>kept</p>"}
    assert mock_build.call_args.kwargs["examples_block"] == get_examples_block("brainstorming")
    task = executor.execute_safe.call_args.args[1]
    assert task.startswith("### CONVERSATION HISTORY:\n...")
    assert "h" * 4001 not in task
    handler = executor.execute_safe.call_args.kwargs["tool_call_handler"]
    handler(ToolCall(name="research_delegate_read", arguments={"path_or_name": "notes.odt"}, id="1"))
    opened = ctx.chat_append_callback.call_args[0][0]
    assert "research_delegate_read" in opened
    assert "notes.odt" in opened
    ctx.status_callback.assert_any_call("research_delegate_read...")


def test_run_smol_side_turn_without_document_opens_keeps_the_default_handler():
    from plugin.chatbot.smol_agent import run_smol_side_turn

    ctx = MagicMock()
    ctx.ctx = MagicMock()
    executor = MagicMock()
    executor.execute_safe.return_value = "notes"
    with patch("plugin.chatbot.smol_agent.build_toolcalling_agent", return_value=MagicMock()), patch(
        "plugin.chatbot.smol_agent.SmolAgentExecutor", return_value=executor
    ):
        out = run_smol_side_turn(
            ctx,
            query="topic",
            history_text=None,
            collector=lambda c: [],
            instructions="inst",
            examples_key="deep_research",
            status_message="Deep research...",
            stop_message="Deep research stopped by user.",
            error_prefix="Deep research failed",
        )

    assert out == {"status": "ok", "result": "notes"}
    assert executor.execute_safe.call_args.kwargs["tool_call_handler"] is None
    ctx.status_callback.assert_called_once_with("Deep research...")


def test_run_inner_tool_agent():
    from plugin.chatbot.smol_agent import run_inner_tool_agent

    ctx = MagicMock()
    ctx.status_callback = MagicMock()
    tools = [MagicMock()]
    handler = MagicMock()
    built_agent = MagicMock()

    executor = MagicMock()
    executor.execute_safe.return_value = "plain string result"

    with patch("plugin.chatbot.smol_agent.build_toolcalling_agent", return_value=built_agent), patch(
        "plugin.chatbot.smol_agent.SmolAgentExecutor", return_value=executor
    ):
        res = run_inner_tool_agent(
            ctx,
            tools,
            instructions="test instructions",
            examples_key="brainstorming",
            final_answer_tool_name="workflow_finished",
            task="do the task",
            stop_message="Stopped by user.",
            error_prefix="Agent failed",
            tool_call_handler=handler,
        )
        assert res == "plain string result"

        error_dict = {"status": "error", "error": "something broke"}
        executor.execute_safe.return_value = error_dict
        res_err = run_inner_tool_agent(
            ctx,
            tools,
            instructions="test instructions",
            examples_key="brainstorming",
            final_answer_tool_name="workflow_finished",
            task="do the task",
            stop_message="Stopped by user.",
            error_prefix="Agent failed",
            tool_call_handler=handler,
        )
        assert res_err is error_dict


# =============================================================================
# Librarian smol tests (from test_librarian_smol.py)
# =============================================================================


@patch("plugin.chatbot.smol_agent.get_config", side_effect=lambda key: 25 if key == "chatbot.max_tool_rounds" else 1024)
@patch("plugin.chatbot.smol_agent.get_api_config", create=True, return_value={"model": "test/model"})
class TestLibrarianSmol:
    def test_tool_adapter_initialization(self, mock_get_api, mock_get_int):
        ctx = MagicMock()
        ctx.ctx = MagicMock()

        memory_tool = MemoryTool()
        reply_tool = StickyReplyToUserTool(require_sidebar_mode(CHAT_MODE_LIBRARIAN))

        smol_memory = SmolToolAdapter(memory_tool, ctx, safe=False, inputs_style="librarian")
        smol_reply = SmolToolAdapter(reply_tool, ctx, safe=False, inputs_style="librarian")

        # Verify that they are instances of smolagents.tools.BaseTool
        from plugin.contrib.smolagents.tools import BaseTool

        assert (isinstance(smol_memory, BaseTool))
        assert (isinstance(smol_reply, BaseTool))
        assert ("switch_to_document_mode") in (smol_reply.inputs)

        # Verify inputs conversion
        assert ("key") in (smol_memory.inputs)
        assert ("content") in (smol_memory.inputs)
        assert (smol_memory.inputs["key"]["type"]) == ("string")
        assert (smol_memory.inputs["content"]["type"]) == ("string")

        # Verify forward call marshals execute onto the drain thread
        memory_tool.execute = MagicMock(return_value={"status": "ok"})
        with patch("plugin.framework.queue_executor.main.call") as mock_main:
            mock_main.side_effect = lambda fn, *args, **kwargs: fn(*args, **kwargs)
            smol_memory.forward(key="favorite_color", content="blue")
        mock_main.assert_called_once()
        assert (mock_main.call_args[0][0]) is (memory_tool.execute)
        memory_tool.execute.assert_called_once()
        args, kwargs = memory_tool.execute.call_args
        assert (kwargs["key"]) == ("favorite_color")
        assert (kwargs["content"]) == ("blue")

    def test_agent_initialization_with_adapted_tools(self, mock_get_api, mock_get_int):
        ctx = MagicMock()
        ctx.ctx = MagicMock()
        model = MagicMock()

        tools = [
            SmolToolAdapter(MemoryTool(), ctx, safe=False, inputs_style="librarian"),
            SmolToolAdapter(StickyReplyToUserTool(require_sidebar_mode(CHAT_MODE_LIBRARIAN)), ctx, safe=False, inputs_style="librarian"),
        ]

        # This shouldn't raise "All elements must be instance of BaseTool"
        agent = ToolCallingAgent(
            tools=tools,
            model=model,
            final_answer_tool_name="reply_to_user",
        )
        assert (len(agent.tools)) == (2)  # memory + reply_to_user (no stock final_answer)
        assert ("memory_upsert") in (agent.tools)
        assert ("reply_to_user") in (agent.tools)
        assert ("switch_to_document_mode") not in (agent.tools)

    def test_switch_mode_extraction(self, mock_get_api, mock_get_int):
        ctx = MagicMock()
        ctx.ctx = MagicMock()
        ctx.stop_checker.return_value = False

        # Mock ToolCallingAgent to simulate a switch_mode observation
        with patch("plugin.chatbot.smol_agent.WriterAgentToolCallingAgent") as mock_agent_class:
            mock_agent = mock_agent_class.return_value

            fa = FinalAnswerStep(
                output={"status": "switch_mode", "result": "See you in document mode!", "message": "See you in document mode!"}
            )
            mock_agent.run.return_value = [fa]

            tool = LibrarianOnboardingTool()
            res = tool.execute(ctx, query="switch me")
            if res["status"] == "error":
                print(f"DEBUG: Tool execution error: {res.get('message')}")

            assert (res["status"]) == ("switch_mode")
            assert (res["result"]) == ("See you in document mode!")

    def test_upsert_memory_calls_chat_append_callback(self, mock_get_api, mock_get_int):
        ctx = MagicMock()
        ctx.ctx = MagicMock()
        ctx.stop_checker.return_value = False
        chat_append = MagicMock()
        ctx.chat_append_callback = chat_append

        tc = ToolCall(
            name="memory_upsert",
            arguments={"key": "nickname", "content": "Bob"},
            id="c1",
        )
        fa = FinalAnswerStep(output={"status": "ok", "result": "Hello"})

        with patch("plugin.chatbot.smol_agent.WriterAgentToolCallingAgent") as mock_agent_class:
            mock_agent = mock_agent_class.return_value
            mock_agent.run.return_value = [tc, fa]

            tool = LibrarianOnboardingTool()
            res = tool.execute(ctx, query="hi")

        assert (res.get("status")) == ("ok")
        assert (res.get("status")) != ("switch_mode")
        chat_append.assert_called_once()
        line = chat_append.call_args[0][0]
        assert ("Memory update") in (line)
        assert ("nickname") in (line)
        assert ("Bob") in (line)

    def test_librarian_onboarding_tool_passes_existing_memory_to_instructions(self, mock_get_api, mock_get_int):
        ctx = MagicMock()
        ctx.ctx = MagicMock()
        ctx.stop_checker.return_value = False

        fa = FinalAnswerStep(output={"status": "ok", "result": "Hello"})

        with patch("plugin.chatbot.memory.MemoryStore") as mock_store_class, \
             patch("plugin.chatbot.smol_agent.WriterAgentToolCallingAgent") as mock_agent_class:
            mock_store = mock_store_class.return_value
            mock_store.read.return_value = '{"favorite_color": "blue", "name": "Alice"}'
            
            mock_agent = mock_agent_class.return_value
            mock_agent.run.return_value = [fa]

            tool = LibrarianOnboardingTool()
            tool.execute(ctx, query="hi")
            
            assert (mock_agent_class.called)
            kwargs = mock_agent_class.call_args.kwargs
            assert ("instructions") in (kwargs)
            assert ("[USER PROFILE / MEMORY]") in (kwargs["instructions"])
            assert ('{"favorite_color": "blue", "name": "Alice"}') in (kwargs["instructions"])

    def test_librarian_onboarding_caps_long_user_memory(self, mock_get_api, mock_get_int):
        from plugin.framework.constants import CHAT_DOCUMENT_CONTEXT_MAX_CHARS

        ctx = MagicMock()
        ctx.ctx = MagicMock()
        ctx.stop_checker.return_value = False
        long_mem = "U" * (CHAT_DOCUMENT_CONTEXT_MAX_CHARS + 500)
        fa = FinalAnswerStep(output={"status": "ok", "result": "Hello"})

        with patch("plugin.chatbot.memory.MemoryStore") as mock_store_class, patch(
            "plugin.chatbot.smol_agent.WriterAgentToolCallingAgent"
        ) as mock_agent_class:
            mock_store_class.return_value.read.return_value = long_mem
            mock_agent_class.return_value.run.return_value = [fa]

            LibrarianOnboardingTool().execute(ctx, query="hi")

            instructions = mock_agent_class.call_args.kwargs["instructions"]
            assert "[USER PROFILE / MEMORY]" in instructions
            assert long_mem not in instructions
            assert "U" * CHAT_DOCUMENT_CONTEXT_MAX_CHARS in instructions
            assert "[truncated]" in instructions

    def test_librarian_onboarding_suggested_user_name_hint(self, mock_get_api, mock_get_int):
        ctx = MagicMock()
        ctx.ctx = MagicMock()
        ctx.stop_checker.return_value = False
        hint = "UNIQUE_NAME_HINT_XYZ123"
        fa = FinalAnswerStep(output={"status": "ok", "result": "Hello"})

        with patch("plugin.chatbot.memory.MemoryStore") as mock_store_class, \
             patch("plugin.chatbot.smol_agent.WriterAgentToolCallingAgent") as mock_agent_class:
            mock_store_class.return_value.read.return_value = ""
            mock_agent_class.return_value.run.return_value = [fa]

            tool = LibrarianOnboardingTool()
            tool.execute(ctx, query="hi", suggested_user_name=hint)
            with_hint = mock_agent_class.call_args.kwargs["instructions"]

            tool.execute(ctx, query="hi")
            without_hint = mock_agent_class.call_args.kwargs["instructions"]

        assert hint in with_hint
        assert hint not in without_hint

    def test_librarian_stop_calls_interrupt(self, mock_get_api, mock_get_int):
        from plugin.framework.send_cancellation import SendCancellation

        ctx = MagicMock()
        ctx.ctx = MagicMock()
        scope = SendCancellation()
        scope.cancel()
        ctx.send_cancellation = scope

        with patch("plugin.chatbot.memory.MemoryStore") as mock_store_class, patch(
            "plugin.chatbot.smol_agent.WriterAgentToolCallingAgent"
        ) as mock_agent_class:
            mock_store_class.return_value.read.return_value = ""
            mock_agent = mock_agent_class.return_value
            mock_agent.run.return_value = [FinalAnswerStep(output="should not finish")]

            res = LibrarianOnboardingTool().execute(ctx, query="stop now")

        mock_agent.interrupt.assert_called()
        assert res["status"] == "error"
        assert res["code"] == "USER_STOPPED"


class TestSmolMixedToolCalls:
    def test_mixing_regular_tool_and_final_answer_tool_succeeds(self):
        """Verify that we can now mix a regular tool call and a final answer tool call in one turn."""
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
        assert (len(final_outputs)) == (1)
        assert (final_outputs[0].is_final_answer)
        assert (final_outputs[0].output) == ("Finished!")

    def test_two_tool_calls_run_in_order_on_one_thread(self):
        """Several calls in one ChatMessage must not fan out to a thread pool."""
        import threading

        from plugin.contrib.smolagents.agents import ToolOutput
        from plugin.contrib.smolagents.models import (
            ChatMessageToolCall,
            ChatMessageToolCallFunction,
        )

        order: list[tuple[str, int]] = []

        class _OrderTool(ToolBase):
            name = "stub"
            description = "desc"
            parameters = {
                "type": "object",
                "properties": {"p": {"type": "string", "description": "param"}},
                "required": ["p"],
            }

            def execute(self, ctx, **kwargs):
                order.append((str(kwargs.get("p")), threading.get_ident()))
                return {"status": "ok", "p": kwargs.get("p")}

        ctx = MagicMock()
        model = MagicMock()
        agent = ToolCallingAgent(
            tools=[SmolToolAdapter(_OrderTool(), ctx, safe=False)],
            model=model,
            final_answer_tool_name="reply_to_user",
        )
        chat_msg = ChatMessage(
            role=MessageRole.ASSISTANT,
            content="",
            tool_calls=[
                ChatMessageToolCall(
                    id="call_a",
                    type="function",
                    function=ChatMessageToolCallFunction(name="stub", arguments={"p": "first"}),
                ),
                ChatMessageToolCall(
                    id="call_b",
                    type="function",
                    function=ChatMessageToolCallFunction(name="stub", arguments={"p": "second"}),
                ),
            ],
        )
        model.generate.return_value = chat_msg
        memory_step = ActionStep(step_number=1, timing=Timing(start_time=time.time()))
        outputs = list(agent._step_stream(memory_step))

        tool_outputs = [o for o in outputs if isinstance(o, ToolOutput)]
        assert (len(tool_outputs)) == (2)
        assert ([p for p, _tid in order]) == (["first", "second"])
        assert (len({tid for _p, tid in order})) == (1)
        assert (order[0][1]) == (threading.get_ident())


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


class TestSmolCallingToolsEchoParsing:
    def test_calling_tools_python_repr_parsed_as_web_search_not_final_answer(self):
        """Memory-style 'Calling tools:' + Python repr must not become final_answer."""
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
        assert (len(tool_outputs)) == (1)
        assert not (final_outputs[0].is_final_answer if final_outputs else True)
        assert (memory_step.tool_calls[0].name) == ("web_search")
        assert (memory_step.tool_calls[0].arguments) == ({"query": "test query"})


class TestSmolImplicitFinalAnswerJson:
    def test_answer_only_json_blob_becomes_final_answer_not_double_wrapped(self):
        """Mercury-style {\"answer\": [html...]} must not wrap raw JSON as final_answer."""
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
        assert (len(final_outputs)) == (1)
        assert (final_outputs[0].is_final_answer)
        assert (final_outputs[0].output) == ("<h1>Title</h1>\n<p>Para</p>")
        assert ('{"answer"') not in (str(final_outputs[0].output))



class TestSmolToolMarshaling:
    @patch("plugin.framework.queue_executor.main.call")
    @patch("plugin.chatbot.memory.user_config_dir")
    def test_memory_tool_marshalled_to_main_thread(self, mock_user_config_dir, mock_call):
        """Verify MemoryTool executes via main thread marshalling despite not using UNO directly."""
        import json
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            mock_user_config_dir.return_value = tmpdir

            def fake_call(func, *args, **kwargs):
                return func(*args, **kwargs)

            mock_call.side_effect = fake_call

            ctx = MagicMock()
            tool = SmolToolAdapter(MemoryTool(), ctx, safe=False)

            res_write = tool.forward(key="user_memory", content="User likes Python")

            assert (res_write.get("status")) == ("ok")
            assert (mock_call.call_count) == (1)

            memory_file = os.path.join(tmpdir, "memories", "USER.md")
            assert (os.path.exists(memory_file))
            with open(memory_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                assert (data.get("user_memory")) == ("User likes Python")

    @patch("plugin.framework.queue_executor.main.call")
    def test_sticky_reply_tool_marshalled_to_main_thread(self, mock_call):
        """Verify StickyReplyToUserTool is async and executes directly on worker without main-thread marshal."""

        ctx = MagicMock()
        mode = require_sidebar_mode(CHAT_MODE_LIBRARIAN)
        tool = SmolToolAdapter(StickyReplyToUserTool(mode), ctx, safe=False)

        res = tool.forward(answer="All done", switch_to_document_mode=True)

        assert (res.get("status")) == (mode.leave_status)
        assert (res.get("result")) == ("All done")
        assert (res.get("message")) == ("All done")
        assert (mock_call.call_count) == (0)

        res2 = tool.forward(answer="Hello")
        assert res2 == {"status": "ok", "result": "Hello"}
        assert (mock_call.call_count) == (0)

    def test_max_steps_string_with_reply_spec_is_incomplete(self):
        """When agent reaches max steps and returns a string without reply_to_user, status is incomplete."""
        from plugin.chatbot.chat_sidebar_mode import CHAT_MODE_BRAINSTORMING, require_sidebar_mode
        from plugin.chatbot.smol_agent import run_smol_side_turn

        ctx = MagicMock()
        with patch("plugin.chatbot.smol_agent.run_inner_tool_agent", return_value="Reached max steps"):
            res = run_smol_side_turn(
                ctx,
                query="design",
                history_text=None,
                collector=lambda c: [],
                instructions="",
                examples_key="brainstorming",
                mode=require_sidebar_mode(CHAT_MODE_BRAINSTORMING),
                status_message="",
                stop_message="",
                error_prefix="",
            )
            assert res == {"status": "incomplete", "result": "Reached max steps"}


def test_inner_call_recorder_logs_name_result_mutating() -> None:
    from typing import Any
    from plugin.chatbot.smol_agent import InnerCallRecorder
    from plugin.doc.specialized_catalog import InnerToolCall
    from plugin.framework.tool import ToolBase, ToolContext

    class _TinyMutatingTool(ToolBase):
        name = "tiny_mutating_tool"
        description = "Mutating test tool."
        parameters = {"type": "object", "properties": {"val": {"type": "string"}}}
        is_mutation = True

        def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
            return {"status": "ok", "echo": kwargs.get("val")}

    ctx = MagicMock(spec=ToolContext)
    ctx.doc_type = "writer"
    ctx.read_only_target = False
    calls: list[InnerToolCall] = []
    tool = _TinyMutatingTool()
    assert tool.detects_mutation() is True

    recorder = InnerCallRecorder(tool, ctx, calls)
    with patch("plugin.framework.queue_executor.main.call", side_effect=lambda fn, *a, **kw: fn(*a, **kw)):
        res = recorder.forward(val="abc")

    assert res == {"status": "ok", "echo": "abc"}
    assert len(calls) == 1
    assert calls[0].name == "tiny_mutating_tool"
    assert calls[0].result == {"status": "ok", "echo": "abc"}
    assert calls[0].mutating is True


def test_writeragent_agent_lets_string_args_reach_tool():
    from typing import Any
    from plugin.chatbot.smol_agent import SmolToolAdapter, WriterAgentToolCallingAgent
    from plugin.framework.tool import ToolBase, ToolContext

    seen: dict[str, Any] = {}

    class _ProbeTool(ToolBase):
        name = "probe"
        description = "Probe tool."
        parameters = {
            "type": "object",
            "properties": {"page": {"type": "integer"}, "flag": {"type": "boolean"}},
            "required": ["page", "flag"],
        }

        def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
            seen.update(kwargs)
            return {"status": "ok"}

    ctx = MagicMock(spec=ToolContext)
    ctx.doc_type = "writer"
    ctx.caller = None
    ctx.read_only_target = False

    agent = WriterAgentToolCallingAgent(
        tools=[SmolToolAdapter(_ProbeTool(), ctx, safe=False, inputs_style="specialized")],
        model=MagicMock(),
    )
    with patch("plugin.framework.queue_executor.main.call", side_effect=lambda fn, *a, **kw: fn(*a, **kw)):
        res = agent.execute_tool_call("probe", {"page": "1", "flag": "true", "bogus": 1})

    assert seen == {"page": 1, "flag": True}
    assert type(seen["page"]) is int
    assert seen["flag"] is True
    assert res["ignored_parameters"] == ["bogus"]


def test_writeragent_agent_returns_validation_error_payload():
    from typing import Any
    from plugin.chatbot.smol_agent import SmolToolAdapter, WriterAgentToolCallingAgent
    from plugin.framework.tool import ToolBase, ToolContext

    class _ProbeTool(ToolBase):
        name = "probe"
        description = "Probe tool."
        parameters = {
            "type": "object",
            "properties": {"page": {"type": "integer"}},
            "required": ["page"],
        }

        def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
            return {"status": "ok"}

    ctx = MagicMock(spec=ToolContext)
    ctx.doc_type = "writer"
    ctx.caller = None
    ctx.read_only_target = False

    agent = WriterAgentToolCallingAgent(
        tools=[SmolToolAdapter(_ProbeTool(), ctx, safe=False, inputs_style="specialized")],
        model=MagicMock(),
    )
    res = agent.execute_tool_call("probe", {"page": "x"})
    assert isinstance(res, dict)
    assert res["code"] == "VALIDATION_ERROR"


def test_writeragent_agent_still_validates_plain_smol_tools():
    from plugin.chatbot.smol_agent import WriterAgentToolCallingAgent
    from plugin.contrib.smolagents.tools import Tool as SmolTool
    from plugin.contrib.smolagents.utils import AgentToolCallError

    class _PlainTool(SmolTool):
        name = "plain"
        description = "Plain smol tool."
        inputs = {"text": {"type": "string", "description": "text"}}
        output_type = "string"

        def forward(self, text: str) -> str:
            return text

    agent = WriterAgentToolCallingAgent(tools=[_PlainTool()], model=MagicMock())
    with pytest.raises(AgentToolCallError):
        agent.execute_tool_call("plain", {"text": 123})


def test_inner_call_recorder_records_editing_tool_that_raises():
    from plugin.chatbot.smol_agent import InnerCallRecorder
    from plugin.doc.specialized_catalog import InnerToolCall, record_edits
    from plugin.framework.tool import ToolBase, ToolContext

    events: list[str] = []

    class _RaisingEditTool(ToolBase):
        name = "edit_and_raise"
        description = "Mutates then raises."
        parameters = {"type": "object", "properties": {}}

        def detects_mutation(self) -> bool:
            return True

        def execute(self, ctx: ToolContext, **kwargs):
            del ctx, kwargs
            events.append("mutated")
            raise RuntimeError("boom after edit")

    ctx = MagicMock(spec=ToolContext)
    ctx.doc_type = "writer"
    ctx.caller = None
    ctx.read_only_target = False
    calls: list[InnerToolCall] = []
    recorder = InnerCallRecorder(_RaisingEditTool(), ctx, calls)
    with patch("plugin.framework.queue_executor.main.call", side_effect=lambda fn, *a, **kw: fn(*a, **kw)):
        result = recorder.forward()

    assert events == ["mutated"]
    assert len(calls) == 1
    assert calls[0].mutating is True
    assert result["status"] == "error"
    assert result["code"] == "TOOL_EXECUTION_ERROR"

    payload: dict[str, object] = {}
    record_edits(payload, calls)
    assert payload["edits"] == {}
    assert "note" not in payload


def _make_edit_doc_tool():
    from plugin.framework.tool import ToolBase, ToolContext

    class _EditTool(ToolBase):
        name = "edit_doc"
        description = "Mutates document."
        parameters = {"type": "object", "properties": {}}

        def detects_mutation(self) -> bool:
            return True

        def execute(self, ctx: ToolContext, **kwargs):
            del ctx, kwargs
            return {"status": "ok", "n": 1}

    return _EditTool()


def _make_unstarted_test_recorders():
    from plugin.chatbot.smol_agent import InnerCallRecorder
    from plugin.doc.specialized_catalog import InnerToolCall
    from plugin.framework.tool import ToolContext
    from plugin.writer.specialized_base import SpecializedWorkflowFinished

    ctx = MagicMock(spec=ToolContext)
    ctx.doc_type = "writer"
    ctx.caller = None
    ctx.read_only_target = False
    calls: list[InnerToolCall] = []
    edit_rec = InnerCallRecorder(_make_edit_doc_tool(), ctx, calls)
    finish_rec = InnerCallRecorder(SpecializedWorkflowFinished(), ctx, calls)
    return edit_rec, finish_rec, calls


def test_inner_call_recorder_unstarted_call_fails_delegation():
    from plugin.doc.specialized_catalog import NO_EDIT_TOOL_NOTE, normalize_inner_result

    edit_rec, finish_rec, calls = _make_unstarted_test_recorders()

    def _call(fn, *a, **kw):
        if getattr(getattr(fn, "__self__", None), "name", None) == "edit_doc":
            raise TimeoutError("Main thread busy")
        return fn(*a, **kw)

    with patch("plugin.framework.queue_executor.main.call", side_effect=_call):
        with pytest.raises(TimeoutError):
            edit_rec.forward()
        ans = finish_rec.forward(answer="done")

    out = normalize_inner_result(ans, ["structural"], calls)
    assert out["status"] == "error"
    assert out["code"] == "SPECIALIZED_TOOL_ERROR"
    assert len(out["tool_errors"]) == 1
    assert out["tool_errors"][0]["tool"] == "edit_doc"
    assert out["edits"] == {}
    assert out["note"] == NO_EDIT_TOOL_NOTE


def test_inner_call_recorder_unstarted_call_recovered_by_retry():
    from plugin.doc.specialized_catalog import normalize_inner_result

    edit_rec, finish_rec, calls = _make_unstarted_test_recorders()
    attempts = 0

    def _call(fn, *a, **kw):
        nonlocal attempts
        if getattr(getattr(fn, "__self__", None), "name", None) == "edit_doc":
            attempts += 1
            if attempts == 1:
                raise TimeoutError("Main thread busy")
        return fn(*a, **kw)

    with patch("plugin.framework.queue_executor.main.call", side_effect=_call):
        with pytest.raises(TimeoutError):
            edit_rec.forward()
        edit_rec.forward()
        ans = finish_rec.forward(answer="done")

    out = normalize_inner_result(ans, ["structural"], calls)
    assert out["status"] == "ok"
    assert len(out["recovered_tool_errors"]) == 1
    assert out["edits"] == {"edit_doc": 1}


def test_inner_call_recorder_cancelled_call_keeps_no_edit_note_on_stop():
    from plugin.doc.specialized_catalog import NO_EDIT_TOOL_NOTE, normalize_inner_result
    from plugin.framework.send_cancellation import SendCancelled

    edit_rec, _finish_rec, calls = _make_unstarted_test_recorders()

    with patch("plugin.framework.queue_executor.main.call", side_effect=SendCancelled()):
        with pytest.raises(SendCancelled):
            edit_rec.forward()

    stopped = {"status": "error", "code": "USER_STOPPED", "message": "Stopped by user."}
    out = normalize_inner_result(stopped, ["structural"], calls)
    assert out["status"] == "error"
    assert out["code"] == "USER_STOPPED"
    assert out["edits"] == {}
    assert out["note"] == NO_EDIT_TOOL_NOTE


def test_build_toolcalling_agent_prints_nothing_when_tool_with_password_fails(capsys):
    from plugin.contrib.smolagents.models import (
        ChatMessage,
        ChatMessageToolCall,
        ChatMessageToolCallFunction,
        MessageRole,
    )
    from plugin.contrib.smolagents.tools import Tool as SmolTool

    class _SecretTool(SmolTool):
        name = "sec"
        description = "Secret tool."
        inputs = {"password": {"type": "string", "description": "password"}}
        output_type = "string"

        def forward(self, password: str) -> str:
            raise ValueError("boom")

    class _ReplyTool(SmolTool):
        name = "reply_to_user"
        description = "Reply to user."
        inputs = {"answer": {"type": "string", "description": "answer"}}
        output_type = "string"

        def forward(self, answer: str) -> str:
            return answer

    mock_model = MagicMock()
    mock_model.generate.side_effect = [
        ChatMessage(
            role=MessageRole.ASSISTANT,
            content="",
            tool_calls=[
                ChatMessageToolCall(
                    function=ChatMessageToolCallFunction(name="sec", arguments={"password": "hunter2"}),
                    id="call_1",
                    type="function",
                )
            ],
        ),
        ChatMessage(
            role=MessageRole.ASSISTANT,
            content="",
            tool_calls=[
                ChatMessageToolCall(
                    function=ChatMessageToolCallFunction(name="reply_to_user", arguments={"answer": "done"}),
                    id="call_2",
                    type="function",
                )
            ],
        ),
    ]

    with (
        patch("plugin.chatbot.smol_agent.create_smol_model", return_value=mock_model),
        patch("plugin.chatbot.smol_agent.get_config", return_value=5),
    ):
        agent = build_toolcalling_agent(
            MagicMock(),
            [_SecretTool(), _ReplyTool()],
            instructions="i",
            final_answer_tool_name="reply_to_user",
            examples_block="e",
        )
        result = agent.run("t")

    assert result == "done"
    assert mock_model.generate.call_count == 2
    assert capsys.readouterr().out == ""


def _assistant_tool_calls_msg(*calls: tuple[str, dict, str]):
    from plugin.contrib.smolagents.models import (
        ChatMessage,
        ChatMessageToolCall,
        ChatMessageToolCallFunction,
        MessageRole,
    )

    return ChatMessage(
        role=MessageRole.ASSISTANT,
        content="",
        tool_calls=[
            ChatMessageToolCall(
                function=ChatMessageToolCallFunction(name=name, arguments=args),
                id=call_id,
                type="function",
            )
            for name, args, call_id in calls
        ],
    )


def _make_partial_turn_agent(mock_model):
    from plugin.chatbot.smol_agent import InnerCallRecorder
    from plugin.doc.specialized_catalog import InnerToolCall
    from plugin.framework.tool import ToolBase, ToolContext
    from plugin.writer.specialized_base import SpecializedWorkflowFinished

    class _OtherTool(ToolBase):
        name = "other_tool"
        description = "Another tool."
        parameters = {"type": "object", "properties": {}}

        def detects_mutation(self) -> bool:
            return False

        def execute(self, ctx: ToolContext, **kwargs):
            del ctx, kwargs
            return {"status": "ok"}

    ctx = MagicMock(spec=ToolContext)
    ctx.doc_type = "writer"
    ctx.caller = None
    ctx.read_only_target = False
    calls: list[InnerToolCall] = []
    tools = [
        InnerCallRecorder(_make_edit_doc_tool(), ctx, calls),
        InnerCallRecorder(_OtherTool(), ctx, calls),
        InnerCallRecorder(SpecializedWorkflowFinished(), ctx, calls),
    ]
    with (
        patch("plugin.chatbot.smol_agent.create_smol_model", return_value=mock_model),
        patch("plugin.chatbot.smol_agent.get_config", return_value=5),
    ):
        agent = build_toolcalling_agent(
            ctx,
            tools,
            instructions="i",
            final_answer_tool_name="workflow_finished",
            examples_block="e",
        )
    return agent, calls


def _fail_edit_doc_on_call(fail_on: int):
    attempts = 0

    def _call(fn, *a, **kw):
        nonlocal attempts
        if getattr(getattr(fn, "__self__", None), "name", None) == "edit_doc":
            attempts += 1
            if attempts == fail_on:
                raise TimeoutError("Main thread busy")
        return fn(*a, **kw)

    return _call


def test_partial_turn_skipped_other_tool_grades_error_with_edits():
    from plugin.doc.specialized_catalog import normalize_inner_result

    mock_model = MagicMock()
    mock_model.generate.side_effect = [
        _assistant_tool_calls_msg(
            ("edit_doc", {}, "c1"),
            ("edit_doc", {}, "c2"),
            ("other_tool", {}, "c3"),
        ),
        _assistant_tool_calls_msg(("workflow_finished", {"answer": "done"}, "c4")),
    ]
    agent, calls = _make_partial_turn_agent(mock_model)

    with patch("plugin.framework.queue_executor.main.call", side_effect=_fail_edit_doc_on_call(2)):
        ans = agent.run("t")

    out = normalize_inner_result(ans, ["structural"], calls)
    assert out["status"] == "error"
    assert out["code"] == "SPECIALIZED_TOOL_ERROR"
    assert out["edits"] == {"edit_doc": 1}
    assert [e["tool"] for e in out["tool_errors"]] == ["edit_doc", "other_tool"]


def test_partial_turn_skipped_call_recovered_by_retry():
    from plugin.doc.specialized_catalog import normalize_inner_result

    mock_model = MagicMock()
    mock_model.generate.side_effect = [
        _assistant_tool_calls_msg(
            ("edit_doc", {}, "c1"),
            ("edit_doc", {}, "c2"),
            ("other_tool", {}, "c3"),
        ),
        _assistant_tool_calls_msg(
            ("edit_doc", {}, "c4"),
            ("other_tool", {}, "c5"),
            ("workflow_finished", {"answer": "done"}, "c6"),
        ),
    ]
    agent, calls = _make_partial_turn_agent(mock_model)

    with patch("plugin.framework.queue_executor.main.call", side_effect=_fail_edit_doc_on_call(2)):
        ans = agent.run("t")

    out = normalize_inner_result(ans, ["structural"], calls)
    assert out["status"] == "ok"
    assert len(out["recovered_tool_errors"]) == 2
    assert out["edits"] == {"edit_doc": 2}


def test_partial_turn_unknown_tool_preserves_earlier_and_grades_skipped():
    from plugin.doc.specialized_catalog import normalize_inner_result

    seen_messages = []
    mock_model = MagicMock()

    def _generate(messages, **kwargs):
        del kwargs
        seen_messages.append(flatten_messages(messages))
        if len(seen_messages) == 1:
            return _assistant_tool_calls_msg(
                ("edit_doc", {}, "c1"),
                ("no_such_tool", {}, "c2"),
                ("other_tool", {}, "c3"),
            )
        return _assistant_tool_calls_msg(("workflow_finished", {"answer": "done"}, "c4"))

    mock_model.generate.side_effect = _generate
    agent, calls = _make_partial_turn_agent(mock_model)

    with patch("plugin.framework.queue_executor.main.call", side_effect=lambda fn, *a, **kw: fn(*a, **kw)):
        ans = agent.run("t")

    assert '"n": 1' in seen_messages[1]
    out = normalize_inner_result(ans, ["structural"], calls)
    assert out["status"] == "error"
    assert out["edits"] == {"edit_doc": 1}
    assert [e["tool"] for e in out["tool_errors"]] == ["other_tool"]


def test_partial_turn_skipped_workflow_finished_not_recorded():
    from plugin.doc.specialized_catalog import normalize_inner_result

    mock_model = MagicMock()
    mock_model.generate.side_effect = [
        _assistant_tool_calls_msg(
            ("edit_doc", {}, "c1"),
            ("no_such_tool", {}, "c2"),
            ("workflow_finished", {"answer": "done"}, "c3"),
        ),
        ChatMessage(role=MessageRole.ASSISTANT, content="stopped without finish"),
    ]
    agent, calls = _make_partial_turn_agent(mock_model)

    with patch("plugin.framework.queue_executor.main.call", side_effect=lambda fn, *a, **kw: fn(*a, **kw)):
        ans = agent.run("t", max_steps=2)

    assert [c.name for c in calls] == ["edit_doc"]
    out = normalize_inner_result(ans, ["structural"], calls)
    assert out["status"] == "incomplete"
    assert out["code"] == "SPECIALIZED_WORKFLOW_INCOMPLETE"
    assert out["edits"] == {"edit_doc": 1}


def test_partial_turn_double_finish_preserves_earlier_and_grades_skipped():
    from plugin.doc.specialized_catalog import normalize_inner_result

    seen_messages = []
    mock_model = MagicMock()

    def _generate(messages, **kwargs):
        del kwargs
        seen_messages.append(flatten_messages(messages))
        if len(seen_messages) == 1:
            return _assistant_tool_calls_msg(
                ("edit_doc", {}, "c1"),
                ("workflow_finished", {"answer": "a"}, "c2"),
                ("workflow_finished", {"answer": "b"}, "c3"),
                ("other_tool", {}, "c4"),
            )
        return _assistant_tool_calls_msg(("workflow_finished", {"answer": "done"}, "c5"))

    mock_model.generate.side_effect = _generate
    agent, calls = _make_partial_turn_agent(mock_model)

    with patch("plugin.framework.queue_executor.main.call", side_effect=lambda fn, *a, **kw: fn(*a, **kw)):
        ans = agent.run("t")

    assert '"n": 1' in seen_messages[1]
    out = normalize_inner_result(ans, ["structural"], calls)
    assert out["status"] == "error"
    assert out["edits"] == {"edit_doc": 1}
    assert [e["tool"] for e in out["tool_errors"]] == ["other_tool"]


@pytest.mark.parametrize(
    "turn_calls",
    [
        [
            ("edit_doc", {}, "c1"),
            ("workflow_finished", {"answer": "done"}, "c2"),
            ("workflow_finished", {"answer": "done2"}, "c3"),
        ],
        [
            ("edit_doc", {}, "c1"),
            ("workflow_finished", {"answer": "done"}, "c2"),
            ("no_such_tool", {}, "c3"),
        ],
    ],
)
def test_partial_turn_rejected_finish_then_max_steps_is_incomplete(turn_calls):
    from plugin.doc.specialized_catalog import normalize_inner_result

    mock_model = MagicMock()
    mock_model.generate.side_effect = [
        _assistant_tool_calls_msg(*turn_calls),
        ChatMessage(role=MessageRole.ASSISTANT, content="ran out of steps"),
    ]
    agent, calls = _make_partial_turn_agent(mock_model)

    with patch("plugin.framework.queue_executor.main.call", side_effect=lambda fn, *a, **kw: fn(*a, **kw)):
        ans = agent.run("t", max_steps=1)

    assert "workflow_finished" in [c.name for c in calls]
    out = normalize_inner_result(ans, ["structural"], calls)
    assert out["status"] == "incomplete"
    assert out["code"] == "SPECIALIZED_WORKFLOW_INCOMPLETE"
    assert out["message"] == "ran out of steps"
    assert out["edits"] == {"edit_doc": 1}
    assert "result" not in out

