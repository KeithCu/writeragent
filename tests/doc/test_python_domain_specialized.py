# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
"""Tests for the two-level python specialized agent (delegate_tool_domains)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from plugin.calc.cells import SortRange
from plugin.calc.sheets import ListSheets
from plugin.contrib.smolagents.memory import FinalAnswerStep
from plugin.doc.python_domain_specialized import (
    DelegateToolDomains,
    agent_label_for_context,
    allowed_specialized_domains,
    gather_domain_tools,
    normalize_domain_list,
    run_inner_domain_tool_agent,
    validate_requested_domains,
)
from plugin.doc.specialized_base import DelegateToSpecializedBase
from plugin.framework.tool import ToolContext, ToolRegistry
from plugin.writer.specialized.footnotes import FootnotesInsert
from plugin.writer.specialized_base import (
    DelegateToSpecializedWriter,
    SpecializedWorkflowFinished,
    ToolWriterFootnoteBase,
    ToolWriterShapeBase,
)
from tests.chatbot.test_tool_loop import _mock_get_config_int_for_sub_agent


class _ShapeProbe(ToolWriterShapeBase):
    name = "shape_summary"
    description = "Summarize shapes on the page, including position and type."
    parameters = {
        "type": "object",
        "properties": {
            "page": {"type": "integer", "description": "0-based page index to summarize."},
        },
        "required": ["page"],
    }

    def execute(self, ctx, **kwargs):
        return {"status": "ok"}


class _NestedDelegateProbe(ToolWriterFootnoteBase):
    """Same name as the outer hop, registered on footnotes, so gather must drop it."""

    name = "delegate_tool_domains"
    description = "Must not be given to the inner domain agent."
    parameters = {"type": "object", "properties": {}}

    def execute(self, ctx, **kwargs):
        return {"status": "ok"}


def _ctx(registry: ToolRegistry, doc_type: str, service: str, **extra):
    doc = MagicMock()
    doc.supportsService = lambda svc, service=service: svc == service
    stop_checker = extra.pop("stop_checker", lambda: False)
    return ToolContext(
        doc,
        MagicMock(),
        doc_type,
        {"tools": registry},
        caller="chat",
        stop_checker=stop_checker,
        **extra,
    )


def test_python_hint_and_examples_require_delegate_before_finish():
    """Computing a layout is not placing shapes; finish only after delegate_tool_domains."""
    from plugin.chatbot.smol_examples import PYTHON_SPECIALIZED_EXAMPLES, get_examples_block
    from plugin.framework.prompts import (
        DRAW_CORE_DIRECTIVES,
        WRITER_CORE_DIRECTIVES,
        python_specialized_sub_agent_hint,
    )

    for label in ("Writer", "Calc", "Draw"):
        hint = python_specialized_sub_agent_hint(label)
        assert "does not place" in hint
        assert "matplotlib" in hint
        assert "MUST call delegate_tool_domains" in hint
        assert "BEFORE specialized_workflow_finished" in hint
        assert 'delegate_to_specialized_*(domain="shapes")' in hint
        assert "page-scale absolute positions in HMM" in hint
        assert "top-left" in hint
        assert "origin-centered" in hint
        assert "document canvas line" in hint
    writer = python_specialized_sub_agent_hint("Writer")
    assert "footnotes" in writer
    assert "shapes" in writer

    block = get_examples_block("writer:python")
    assert block == PYTHON_SPECIALIZED_EXAMPLES
    parts = block.split('Task: "')
    assert "delegate_tool_domains" not in parts[1]
    assert '"domains": ["footnotes"]' in parts[2]
    ring = parts[3]
    assert "ring of 8 blue circles" in ring
    assert '"domains": ["shapes"]' in ring
    assert "10500" in ring
    assert "14000" in ring
    assert "7000" in ring
    assert "1600" in ring
    assert "HMM" in ring
    assert "top-left" in ring
    assert "not (0,0)" in ring
    assert ring.index("run_venv_python_script") < ring.index("delegate_tool_domains")
    assert ring.index("delegate_tool_domains") < ring.index("specialized_workflow_finished")
    assert "via the shapes domain" in ring
    assert 'rather than domain="shapes"' in WRITER_CORE_DIRECTIVES
    assert 'rather than domain="shapes"' in DRAW_CORE_DIRECTIVES
    assert "draw a rectangle" in WRITER_CORE_DIRECTIVES


def test_domain_loop_hints_include_footnotes_and_charts_lines():
    """Cheap static suffixes match the single-domain specialized loop."""
    from plugin.doc.python_domain_specialized import _domain_loop_hints

    parent = MagicMock()
    parent.doc = None
    calc = _domain_loop_hints(parent, ["charts"], "Calc")
    assert "data_range='A1:B10'" in calc
    writer = _domain_loop_hints(parent, ["charts", "footnotes"], "Writer")
    assert "headers" in writer
    assert "rows" in writer
    assert "insert_after" in writer
    assert "Document canvas" not in writer


def test_normalize_domain_list_dedupes_and_parses_json_array():
    names, err = normalize_domain_list([" footnotes ", "shapes", "footnotes"])
    assert err is None
    assert names == ["footnotes", "shapes"]
    names, err = normalize_domain_list('["sheets", "ranges"]')
    assert err is None
    assert names == ["sheets", "ranges"]
    names, err = normalize_domain_list("footnotes")
    assert names is None
    assert err and "list" in err
    names, err = normalize_domain_list([])
    assert names is None


def test_validate_domains_by_app():
    writer = allowed_specialized_domains("Writer")
    calc = allowed_specialized_domains("Calc")
    draw = allowed_specialized_domains("Draw")
    assert "python" not in writer
    assert "python" not in calc
    assert "footnotes" in writer
    assert "sheets" not in writer
    assert "sheets" in calc
    assert "ranges" in calc
    assert "footnotes" not in calc
    assert "speaker_notes" in draw
    assert "ppt-master" not in draw

    message, code = validate_requested_domains(["footnotes", "shapes"], "Writer")
    assert message is None and code is None
    message, code = validate_requested_domains(["sheets"], "Writer")
    assert code == "UNKNOWN_SPECIALIZED_DOMAIN"
    assert message and "sheets" in message
    message, code = validate_requested_domains(["python"], "Writer")
    assert code == "PYTHON_DOMAIN_NOT_NESTED"
    message, code = validate_requested_domains(["python", "not_a_domain"], "Calc")
    assert code == "UNKNOWN_SPECIALIZED_DOMAIN"
    assert message and "python" in message and "not_a_domain" in message
    message, code = validate_requested_domains(["speaker_notes"], "Draw")
    assert message is None
    message, code = validate_requested_domains(["ppt-master"], "Draw")
    assert code == "UNKNOWN_SPECIALIZED_DOMAIN"


def test_agent_label_from_doc_type():
    registry = ToolRegistry(services={})
    assert agent_label_for_context(_ctx(registry, "writer", "com.sun.star.text.TextDocument")) == "Writer"
    assert agent_label_for_context(_ctx(registry, "calc", "com.sun.star.sheet.SpreadsheetDocument")) == "Calc"
    assert agent_label_for_context(_ctx(registry, "impress", "com.sun.star.presentation.PresentationDocument")) == "Draw"


def test_delegate_tool_domains_rejects_unknown_and_python_and_empty():
    registry = ToolRegistry(services={})
    tool = DelegateToolDomains()
    ctx = _ctx(registry, "writer", "com.sun.star.text.TextDocument")

    missing = tool.execute(ctx, domains=[], task="add a note")
    assert missing["status"] == "error"
    assert missing.get("code") == "DOMAINS_REQUIRED"

    no_task = tool.execute(ctx, domains=["footnotes"], task="  ")
    assert no_task["status"] == "error"
    assert no_task.get("code") == "TASK_REQUIRED"

    unknown = tool.execute(ctx, domains=["sheets"], task="add a sheet")
    assert unknown["status"] == "error"
    assert unknown.get("code") == "UNKNOWN_SPECIALIZED_DOMAIN"
    assert "sheets" in unknown["message"]

    nested = tool.execute(ctx, domains=["python"], task="compute a prime")
    assert nested["status"] == "error"
    assert nested.get("code") == "PYTHON_DOMAIN_NOT_NESTED"

    assert not isinstance(tool, DelegateToSpecializedBase)


def test_delegate_tool_domains_visible_on_python_domain_for_each_app():
    registry = ToolRegistry(services={})
    registry.register(DelegateToolDomains())
    registry.register(SpecializedWorkflowFinished())
    cases = (
        ("writer", "com.sun.star.text.TextDocument"),
        ("calc", "com.sun.star.sheet.SpreadsheetDocument"),
        ("draw", "com.sun.star.drawing.DrawingDocument"),
    )
    for doc_type, service in cases:
        doc = MagicMock()
        doc.supportsService = lambda svc, service=service: svc == service
        names = {
            tool.name
            for tool in registry.get_tools(doc=doc, doc_type=doc_type, active_domain="python", exclude_tiers=())
        }
        assert "delegate_tool_domains" in names
        assert "specialized_workflow_finished" in names
        default_names = {tool.name for tool in registry.get_tools(doc=doc, doc_type=doc_type)}
        assert "delegate_tool_domains" not in default_names


@patch("plugin.doc.python_domain_specialized.build_toolcalling_agent")
@patch("plugin.doc.python_domain_specialized.SmolAgentExecutor")
def test_run_inner_domain_tool_agent_unions_full_schemas(mock_executor_cls, mock_build):
    mock_build.return_value = MagicMock()
    mock_executor_cls.return_value.execute_safe.return_value = "Inserted the footnote."

    registry = ToolRegistry(services={})
    registry.register(FootnotesInsert())
    registry.register(_ShapeProbe())
    registry.register(_NestedDelegateProbe())
    registry.register(SpecializedWorkflowFinished())
    parent = _ctx(registry, "writer", "com.sun.star.text.TextDocument")
    parent.append_thinking_callback = MagicMock()
    parent.status_callback = MagicMock()

    result = run_inner_domain_tool_agent(parent, ["footnotes", "shapes"], "Add a footnote and summarize shapes")
    assert result["status"] == "ok"
    assert result["domains"] == ["footnotes", "shapes"]
    assert result["result"] == "Inserted the footnote."

    tools_arg = mock_build.call_args[0][1]
    by_name = {tool.name: tool for tool in tools_arg}
    assert "footnotes_insert" in by_name
    assert "shape_summary" in by_name
    assert "delegate_tool_domains" not in by_name
    assert tools_arg[-1].name == "specialized_workflow_finished"
    note = by_name["footnotes_insert"].inputs["note"]
    assert note["enum"] == ["footnote", "endnote"]
    assert "footnote" in note["description"].lower()
    assert note["nullable"] is False
    assert "Inserts a new footnote" in by_name["footnotes_insert"].description
    assert "0-based page index" in by_name["shape_summary"].inputs["page"]["description"]

    inner_ctx = mock_build.call_args[0][0]
    assert inner_ctx.doc is parent.doc
    assert inner_ctx.stop_checker is parent.stop_checker
    assert inner_ctx.status_callback is parent.status_callback
    assert inner_ctx.read_only_target is False
    assert inner_ctx.set_active_domain_callback is None
    instructions = mock_build.call_args.kwargs["instructions"]
    assert "footnotes" in instructions
    assert "shapes" in instructions
    assert mock_build.call_args.kwargs["final_answer_tool_name"] == "specialized_workflow_finished"
    examples_key_block = mock_build.call_args.kwargs["examples_block"]
    assert "run_venv_python_script" not in examples_key_block
    assert "specialized_workflow_finished" in examples_key_block
    assert "insert_after" in instructions


@patch("plugin.doc.python_domain_specialized.build_toolcalling_agent")
@patch("plugin.doc.python_domain_specialized.SmolAgentExecutor")
@patch("plugin.doc.specialized_shapes_context.format_shapes_canvas_context")
def test_shapes_domain_includes_canvas_context_from_main_thread(mock_canvas, mock_executor_cls, mock_build):
    """Inner shapes instructions get the same canvas line as the shapes specialized loop."""
    mock_canvas.return_value = " Document canvas (Writer): paper 210.0 x 297.0 mm"
    mock_build.return_value = MagicMock()
    mock_executor_cls.return_value.execute_safe.return_value = "placed"

    registry = ToolRegistry(services={})
    registry.register(_ShapeProbe())
    registry.register(FootnotesInsert())
    registry.register(SpecializedWorkflowFinished())
    parent = _ctx(registry, "writer", "com.sun.star.text.TextDocument")

    def _call(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with patch("plugin.framework.thread_guard.on_main_thread", return_value=False), patch(
        "plugin.framework.queue_executor.execute_on_main_thread", side_effect=_call
    ) as mock_main:
        result = run_inner_domain_tool_agent(parent, ["shapes"], "Place a ring of circles")

    assert result["status"] == "ok"
    mock_canvas.assert_called_once_with(parent.doc)
    assert mock_main.called
    instructions = mock_build.call_args.kwargs["instructions"]
    assert "Document canvas (Writer)" in instructions
    assert "210.0 x 297.0 mm" in instructions

    mock_canvas.reset_mock()
    run_inner_domain_tool_agent(parent, ["footnotes"], "Add a note")
    mock_canvas.assert_not_called()
    footnotes_instructions = mock_build.call_args.kwargs["instructions"]
    assert "Document canvas" not in footnotes_instructions


@patch("plugin.doc.python_domain_specialized.build_toolcalling_agent")
@patch("plugin.doc.python_domain_specialized.SmolAgentExecutor")
def test_run_inner_domain_tool_agent_calc_union(mock_executor_cls, mock_build):
    mock_build.return_value = MagicMock()
    mock_executor_cls.return_value.execute_safe.return_value = "sorted"

    registry = ToolRegistry(services={})
    registry.register(SortRange())
    registry.register(ListSheets())
    registry.register(FootnotesInsert())
    registry.register(SpecializedWorkflowFinished())
    parent = _ctx(registry, "calc", "com.sun.star.sheet.SpreadsheetDocument")

    result = run_inner_domain_tool_agent(parent, ["sheets", "ranges"], "List sheets and sort A1:B10")
    assert result["status"] == "ok"
    tools_arg = mock_build.call_args[0][1]
    names = {tool.name for tool in tools_arg}
    assert "list_sheets" in names
    assert "sort_range" in names
    assert "footnotes_insert" not in names
    by_name = {tool.name: tool for tool in tools_arg}
    assert "labels" in by_name["sort_range"].inputs["has_header"]["description"]
    assert by_name["sort_range"].inputs["range"]["type"] == "array"


def test_gather_domain_tools_marshals_off_main_thread():
    registry = ToolRegistry(services={})
    registry.register(FootnotesInsert())
    registry.register(SpecializedWorkflowFinished())
    parent = _ctx(registry, "writer", "com.sun.star.text.TextDocument")

    def _call(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with patch("plugin.framework.thread_guard.on_main_thread", return_value=False), patch(
        "plugin.framework.queue_executor.execute_on_main_thread", side_effect=_call
    ) as mock_main:
        tools = gather_domain_tools(parent, ["footnotes"])
    assert mock_main.called
    assert {tool.name for tool in tools} >= {"footnotes_insert", "specialized_workflow_finished"}


@patch("plugin.doc.python_domain_specialized.run_inner_domain_tool_agent", return_value={"status": "ok", "domains": ["footnotes"], "result": "noted"})
def test_delegate_tool_domains_execute_returns_inner_result(mock_inner):
    registry = ToolRegistry(services={})
    ctx = _ctx(registry, "writer", "com.sun.star.text.TextDocument")
    result = DelegateToolDomains().execute(ctx, domains=[" footnotes ", "footnotes"], task=" Add the note ")
    assert result["status"] == "ok"
    assert result["result"] == "noted"
    mock_inner.assert_called_once()
    assert mock_inner.call_args[0][1] == ["footnotes"]
    assert mock_inner.call_args[0][2] == "Add the note"


@patch(
    "plugin.chatbot.smol_agent.get_config_int",
    side_effect=_mock_get_config_int_for_sub_agent,
)
@patch("plugin.chatbot.smol_agent.get_api_config", create=True)
@patch("plugin.chatbot.smol_agent.ToolCallingAgent")
@patch("plugin.chatbot.smol_agent.WriterAgentSmolModel")
@patch("plugin.chatbot.smol_agent.LlmClient")
@patch("plugin.doc.specialized_base.format_shapes_canvas_context", return_value=" Document canvas (Writer): paper 210.0 x 297.0 mm")
def test_python_outer_delegation_gets_delegate_tool_domains_and_hint(
    mock_canvas,
    mock_llm,
    mock_smol_model,
    mock_agent_class,
    mock_get_config,
    _mock_get_config_int,
):
    registry = ToolRegistry(services={})
    registry.register(DelegateToolDomains())
    registry.register(SpecializedWorkflowFinished())
    registry.register(DelegateToSpecializedWriter())

    mock_get_config.return_value = {}
    mock_agent_instance = MagicMock()
    mock_agent_instance.run.return_value = [FinalAnswerStep(output="done")]
    mock_agent_class.return_value = mock_agent_instance

    ctx = MagicMock()
    ctx.doc = MagicMock()
    ctx.doc.supportsService = lambda svc: svc == "com.sun.star.text.TextDocument"
    ctx.ctx = MagicMock()
    ctx.services = {"tools": registry}
    ctx.stop_checker = lambda: False
    ctx.doc_type = "writer"

    gateway = registry.get("delegate_to_specialized_writer_toolset")
    result = gateway.execute_safe(ctx, domain="python", task="Add a footnote after the revenue sentence")
    assert result["status"] == "ok"
    smol_tools = mock_agent_class.call_args.kwargs.get("tools", [])
    by_name = {tool.name: tool for tool in smol_tools}
    assert "delegate_tool_domains" in by_name
    assert "specialized_workflow_finished" in by_name
    domains_input = by_name["delegate_tool_domains"].inputs["domains"]
    assert domains_input["type"] == "array"
    assert domains_input["minItems"] == 1
    assert domains_input["items"]["type"] == "string"
    assert "task" in by_name["delegate_tool_domains"].inputs
    instructions = mock_agent_class.call_args.kwargs.get("instructions") or ""
    assert "delegate_tool_domains" in instructions
    assert "domains" in instructions
    assert "task" in instructions
    assert "BEFORE specialized_workflow_finished" in instructions
    assert "does not place" in instructions
    assert 'delegate_to_specialized_*(domain="shapes")' in instructions
    mock_canvas.assert_called_once_with(ctx.doc)
    assert "Document canvas (Writer)" in instructions
    assert "210.0 x 297.0 mm" in instructions
    examples = mock_agent_class.call_args.kwargs.get("system_prompt_examples") or ""
    assert "delegate_tool_domains" in examples
    assert '"domains"' in examples
    assert '"task"' in examples
    assert '"domains": ["shapes"]' in examples
    ring = examples.split('Task: "Use python to place a ring', 1)[1]
    assert ring.index("delegate_tool_domains") < ring.index("specialized_workflow_finished")
    assert "via the shapes domain" in ring
    assert "10500" in ring
    assert "HMM" in ring
    assert "top-left" in ring
