# WriterAgent - AI Writing Assistant for LibreOffice
# SPDX-License-Identifier: GPL-3.0-or-later

"""Unit tests for DelegateToSpecializedBase (domain validation, mutation lock, registry exposure) and dummy-tool fixtures."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from plugin.framework.tool import ToolContext
from plugin.writer.specialized_base import DelegateToSpecializedWriter


@pytest.fixture
def writer_gateway() -> DelegateToSpecializedWriter:
    return DelegateToSpecializedWriter()


@pytest.fixture
def mock_ctx() -> ToolContext:
    ctx = MagicMock(spec=ToolContext)
    ctx.ctx = MagicMock()
    ctx.services = MagicMock()
    ctx.doc = MagicMock()
    return ctx


def test_requires_document_lock_non_str_domain(writer_gateway: DelegateToSpecializedWriter) -> None:
    """Non-string domain values must safely default to requiring document lock without TypeError."""
    # Dict domain (unhashable)
    assert writer_gateway.requires_document_lock({"domain": {"nested": "value"}}) is True

    # List domain (unhashable)
    assert writer_gateway.requires_document_lock({"domain": ["tables", "shapes"]}) is True

    # Int domain
    assert writer_gateway.requires_document_lock({"domain": 42}) is True

    # None domain
    assert writer_gateway.requires_document_lock({"domain": None}) is True

    # JSON string containing a dict domain
    assert writer_gateway.requires_document_lock('{"domain": {"nested": "value"}}') is True

    # Empty / None arguments
    assert writer_gateway.requires_document_lock(None) is True
    assert writer_gateway.requires_document_lock({}) is True


def test_requires_document_lock_string_domains(writer_gateway: DelegateToSpecializedWriter) -> None:
    """Read-only string domains skip lock; mutating string domains and vision require lock."""
    # Read-only domains do not require document lock
    assert writer_gateway.requires_document_lock({"domain": "document_research"}) is False
    assert writer_gateway.requires_document_lock({"domain": "web_research"}) is False

    # Vision requires lock even though listed in READ_ONLY_DOMAINS
    assert writer_gateway.requires_document_lock({"domain": "vision"}) is True

    # Mutating domains require lock
    assert writer_gateway.requires_document_lock({"domain": "tables"}) is True
    assert writer_gateway.requires_document_lock({"domain": "shapes"}) is True


def test_execute_non_str_domain_returns_error(
    writer_gateway: DelegateToSpecializedWriter, mock_ctx: ToolContext
) -> None:
    """Executing with a non-string domain must return the invalid-domain error instead of raising TypeError."""
    result = writer_gateway.execute(mock_ctx, domain={"foo": "bar"}, task="analyze")
    assert result["status"] == "error"
    assert "No specialized tools found for domain" in result["message"]
    assert "foo" in result["message"]

    list_result = writer_gateway.execute(mock_ctx, domain=["invalid"], task="analyze")
    assert list_result["status"] == "error"
    assert "No specialized tools found for domain" in list_result["message"]


def test_delegate_status_workflow_finished_ok(
    writer_gateway: DelegateToSpecializedWriter,
) -> None:
    """Discussion #2029: Finish dict with no inner errors stays status: 'ok'."""
    from unittest.mock import patch

    ctx = _structural_registry(writer_gateway)
    with patch("plugin.framework.queue_executor.main.call", side_effect=lambda fn, *a, **kw: fn(*a, **kw)):
        with patch("plugin.doc.specialized_base.run_inner_tool_agent") as mock_run_inner:
            mock_run_inner.return_value = {
                "status": "ok",
                "finished": True,
                "answer": "Navigated to page 3",
                "message": "Specialized task complete.",
            }
            result = writer_gateway.execute(ctx, domain="structural", task="Go to page 3")

    assert result["status"] == "ok"
    assert result["answer"] == "Navigated to page 3"
    from plugin.framework.prompts import DELEGATE_ANCHOR_RULE
    assert DELEGATE_ANCHOR_RULE in mock_run_inner.call_args.kwargs["instructions"]


def test_delegate_status_inner_tool_error(
    writer_gateway: DelegateToSpecializedWriter,
) -> None:
    """Discussion #2029: Inner tool error becomes status: 'error', code SPECIALIZED_TOOL_ERROR."""
    from unittest.mock import patch
    from plugin.writer.specialized_base import ToolWriterStructuralBase

    class _FailingStructuralTool(ToolWriterStructuralBase):
        name = "failing_structural_tool"
        description = "A failing structural tool."
        parameters = {"type": "object", "properties": {}}

        def execute(self, ctx, **kw):
            return {"status": "error", "message": "Failed to read page objects"}

    ctx = _structural_registry(writer_gateway, tool=_FailingStructuralTool())

    def fake_run_inner(ctx, tools, **kwargs):
        tool_map = {t.name: t for t in tools}
        tool_map["failing_structural_tool"]()
        return tool_map["specialized_workflow_finished"](answer="Final attempted answer")

    with patch("plugin.framework.queue_executor.main.call", side_effect=lambda fn, *a, **kw: fn(*a, **kw)):
        with patch("plugin.doc.specialized_base.run_inner_tool_agent", side_effect=fake_run_inner):
            result = writer_gateway.execute(ctx, domain="structural", task="Inspect page")

    assert result["status"] == "error"
    assert result["code"] == "SPECIALIZED_TOOL_ERROR"
    # Discussion #2268: the finish answer is delivered once, in answer only.
    assert result["message"] == "Failed to read page objects"
    assert result["error"] == "Failed to read page objects"
    assert result["answer"] == "Final attempted answer"


def test_delegate_status_incomplete_no_finish_no_tool_error(
    writer_gateway: DelegateToSpecializedWriter,
) -> None:
    """Discussion #2029: No finish tool call and no tool error becomes status: 'incomplete'."""
    from unittest.mock import patch

    ctx = _structural_registry(writer_gateway)
    with patch("plugin.framework.queue_executor.main.call", side_effect=lambda fn, *a, **kw: fn(*a, **kw)):
        with patch("plugin.doc.specialized_base.run_inner_tool_agent", return_value="I searched but could not find the heading."):
            result = writer_gateway.execute(ctx, domain="structural", task="Find heading")

    assert result["status"] == "incomplete"
    assert result["code"] == "SPECIALIZED_WORKFLOW_INCOMPLETE"
    assert result["message"] == "I searched but could not find the heading."


def _structural_registry(writer_gateway: DelegateToSpecializedWriter, tool: Any = None) -> MagicMock:
    from plugin.framework.tool import ToolRegistry
    from plugin.writer.specialized_base import SpecializedWorkflowFinished, ToolWriterStructuralBase

    class _DummyStructuralTool(ToolWriterStructuralBase):
        name = "dummy_structural_tool"
        description = "A dummy structural tool."
        parameters = {"type": "object", "properties": {}}

        def execute(self, ctx, **kw):
            return {"status": "ok"}

    reg = ToolRegistry(services={})
    reg.register(tool if tool is not None else _DummyStructuralTool())
    reg.register(SpecializedWorkflowFinished())
    reg.register(writer_gateway)

    ctx = MagicMock(spec=ToolContext)
    ctx.ctx = MagicMock()
    ctx.doc = MagicMock()
    ctx.doc.supportsService.return_value = True
    ctx.services = {"tools": reg}
    ctx.stop_checker = lambda: False
    return ctx


def test_delegate_length_truncated_reply_is_incomplete_not_finished(
    writer_gateway: DelegateToSpecializedWriter,
) -> None:
    """Discussion #2262: a plain-text reply cut off by finish_reason=length must not become a
    synthetic specialized_workflow_finished call; the delegate reports incomplete with the partial text."""
    from unittest.mock import patch

    from plugin.chatbot.smol_agent import WriterAgentSmolModel

    partial = "I'll now build the final JSON delivery with both original returns"
    client = MagicMock()
    client.config = {"model": "test/local"}
    client.request_with_tools.return_value = {
        "content": partial,
        "tool_calls": None,
        "finish_reason": "length",
        "images": None,
        "usage": {"prompt_tokens": 10, "completion_tokens": 16384},
    }
    ctx = _structural_registry(writer_gateway)

    with patch("plugin.framework.queue_executor.main.call", side_effect=lambda fn, *a, **kw: fn(*a, **kw)):
        with patch("plugin.chatbot.smol_agent.create_smol_model", return_value=WriterAgentSmolModel(client, max_tokens=256)), patch("plugin.chatbot.smol_agent.get_config", return_value=5):
            result = writer_gateway.execute(ctx, domain="structural", task="Build the delivery")

    assert client.request_with_tools.call_count == 1
    assert result["status"] == "incomplete"
    assert result["code"] == "SPECIALIZED_WORKFLOW_INCOMPLETE"
    assert result["message"] == partial
    assert "finished" not in result
    assert "result" not in result
    assert json.dumps(result).count(partial) == 1


def test_delegate_plain_text_answer_appears_once(
    writer_gateway: DelegateToSpecializedWriter,
) -> None:
    """Discussion #2262: a plain-text return (no finish call) is carried once, on message."""
    from unittest.mock import patch

    text = "PLAIN-" + "y" * 5000
    ctx = _structural_registry(writer_gateway)

    with patch("plugin.framework.queue_executor.main.call", side_effect=lambda fn, *a, **kw: fn(*a, **kw)):
        with patch("plugin.doc.specialized_base.run_inner_tool_agent", return_value=text):
            result = writer_gateway.execute(ctx, domain="structural", task="Summarize headings")

    assert result["status"] == "incomplete"
    assert result["message"] == text
    assert "result" not in result
    assert json.dumps(result).count("PLAIN-") == 1


def test_delegate_finished_payload_does_not_duplicate_answer(
    writer_gateway: DelegateToSpecializedWriter,
) -> None:
    """Discussion #2262: the finish answer is sent once, not copied again into ``result``."""
    import json
    from unittest.mock import patch

    answer = "ENTRY-" + "x" * 5000
    ctx = _structural_registry(writer_gateway)

    def fake_run_inner(ctx, tools, **kwargs):
        tool_map = {t.name: t for t in tools}
        tool_map["dummy_structural_tool"]()
        return tool_map["specialized_workflow_finished"](answer=answer)

    with patch("plugin.framework.queue_executor.main.call", side_effect=lambda fn, *a, **kw: fn(*a, **kw)):
        with patch("plugin.doc.specialized_base.run_inner_tool_agent", side_effect=fake_run_inner):
            result = writer_gateway.execute(ctx, domain="structural", task="Deliver entries")

    assert result["status"] == "ok"
    assert result["finished"] is True
    assert result["answer"] == answer
    assert result["domains"] == ["structural"]
    assert "result" not in result
    assert json.dumps(result).count("ENTRY-") == 1


def test_delegate_status_user_stopped_left_unchanged(
    writer_gateway: DelegateToSpecializedWriter,
) -> None:
    """Discussion #2029: User-stopped or crash dict already status: 'error' is left unchanged."""
    from unittest.mock import patch

    ctx = _structural_registry(writer_gateway)
    stopped = {"status": "error", "error": "Task stopped by user.", "code": "USER_STOPPED"}
    with patch("plugin.framework.queue_executor.main.call", side_effect=lambda fn, *a, **kw: fn(*a, **kw)):
        with patch("plugin.doc.specialized_base.run_inner_tool_agent", return_value=stopped):
            result = writer_gateway.execute(ctx, domain="structural", task="Find heading")

    assert result["status"] == "error"
    assert result["code"] == "USER_STOPPED"


def test_gateway_enum_is_sorted_and_unique(writer_gateway: DelegateToSpecializedWriter) -> None:
    assert writer_gateway.parameters is not None
    enum = writer_gateway.parameters["properties"]["domain"]["enum"]
    assert enum == sorted(enum)
    assert len(enum) == len(set(enum))


def test_delegate_completed_message_uses_static_msgid(
    writer_gateway: DelegateToSpecializedWriter,
) -> None:
    from unittest.mock import patch

    ctx = _structural_registry(writer_gateway)

    def fake_run_inner(ctx, tools, **kwargs):
        tool_map = {t.name: t for t in tools}
        tool_map["dummy_structural_tool"]()
        tool_map["specialized_workflow_finished"](answer="Done")
        return "All done."

    with patch("plugin.framework.queue_executor.main.call", side_effect=lambda fn, *a, **kw: fn(*a, **kw)):
        with patch("plugin.doc.specialized_base.run_inner_tool_agent", side_effect=fake_run_inner):
            result = writer_gateway.execute(ctx, domain="structural", task="Inspect page")

    assert result["status"] == "ok"
    assert result["message"] == "Specialized task (structural) completed."


def test_delegate_finish_only_stays_ok(
    writer_gateway: DelegateToSpecializedWriter,
) -> None:
    from unittest.mock import patch
    from plugin.chatbot.tool_loop_actions import format_delegate_result_chat_line
    from plugin.doc.specialized_catalog import NO_EDIT_TOOL_NOTE
    from plugin.writer.specialized_base import ToolWriterDocumentResearchBase

    answer = "All done without tools."

    def fake_run_inner(ctx, tools, **kwargs):
        tool_map = {t.name: t for t in tools}
        return tool_map["specialized_workflow_finished"](answer=answer)

    # finish-only on an editing domain -> status "ok", finished true, answer kept, note == NO_EDIT_TOOL_NOTE
    ctx_edit = _structural_registry(writer_gateway)

    with patch("plugin.framework.queue_executor.main.call", side_effect=lambda fn, *a, **kw: fn(*a, **kw)):
        with patch("plugin.doc.specialized_base.run_inner_tool_agent", side_effect=fake_run_inner):
            result_edit = writer_gateway.execute(ctx_edit, domain="structural", task="Check headings")

    assert result_edit["status"] == "ok"
    assert result_edit["finished"] is True
    assert result_edit["answer"] == answer
    assert result_edit.get("note") == NO_EDIT_TOOL_NOTE
    chat_line = format_delegate_result_chat_line({"domain": "structural"}, result_edit)
    assert "done" in chat_line

    # finish-only on a read-only domain (document_research) -> status "ok", no note
    class _DummyDocResearchTool(ToolWriterDocumentResearchBase):
        name = "dummy_doc_research_tool"
        description = "A dummy doc research tool."
        parameters = {"type": "object", "properties": {}}

        def execute(self, ctx, **kw):
            return {"status": "ok"}

    ctx_ro = _structural_registry(writer_gateway, tool=_DummyDocResearchTool())

    with patch("plugin.framework.queue_executor.main.call", side_effect=lambda fn, *a, **kw: fn(*a, **kw)):
        # document_research queries embeddings.folder_search_mode, which is unregistered in standalone runs.
        with patch("plugin.framework.constants.configured_folder_search_mode", return_value="none"):
            with patch("plugin.doc.specialized_base.run_inner_tool_agent", side_effect=fake_run_inner):
                result_ro = writer_gateway.execute(ctx_ro, domain="document_research", task="Search docs")

    assert result_ro["status"] == "ok"
    assert result_ro["finished"] is True
    assert result_ro["answer"] == answer
    assert "note" not in result_ro


def test_delegate_retried_tool_error_is_recovered(
    writer_gateway: DelegateToSpecializedWriter,
) -> None:
    from unittest.mock import patch
    from plugin.writer.specialized_base import ToolWriterStructuralBase

    class _RetryingStructuralTool(ToolWriterStructuralBase):
        name = "retrying_structural_tool"
        description = "A retrying structural tool."
        parameters = {"type": "object", "properties": {}}

        def __init__(self) -> None:
            super().__init__()
            self._attempt = 0

        def execute(self, ctx, **kw):
            self._attempt += 1
            if self._attempt == 1:
                return {"status": "error", "message": "First attempt failed"}
            return {"status": "ok"}

    ctx = _structural_registry(writer_gateway, tool=_RetryingStructuralTool())

    def fake_run_inner(ctx, tools, **kwargs):
        tool_map = {t.name: t for t in tools}
        tool_map["retrying_structural_tool"]()
        tool_map["retrying_structural_tool"]()
        return tool_map["specialized_workflow_finished"](answer="Succeeded after retry")

    with patch("plugin.framework.queue_executor.main.call", side_effect=lambda fn, *a, **kw: fn(*a, **kw)):
        with patch("plugin.doc.specialized_base.run_inner_tool_agent", side_effect=fake_run_inner):
            result = writer_gateway.execute(ctx, domain="structural", task="Retry inspect")

    assert result["status"] == "ok"
    assert result["finished"] is True
    assert result["recovered_tool_errors"] == ["First attempt failed"]


def test_draw_gateway_enum_narrowed_by_uno_services_not_doc_type_label() -> None:
    """Impress and Draw both carry doc_type "draw"; only PresentationDocument keeps slide domains."""
    import plugin.draw.headers_footers  # noqa: F401
    import plugin.draw.notes  # noqa: F401
    import plugin.draw.comments  # noqa: F401
    from plugin.framework.doc_type import DRAW_DOCUMENT_SERVICE, IMPRESS_DOCUMENT_SERVICE
    from plugin.doc.specialized_base import narrow_delegate_domain_enums
    from plugin.draw.specialized import DelegateToSpecializedDraw
    from plugin.framework.tool_schema import to_mcp_schema, to_openai_schema

    gateway = DelegateToSpecializedDraw()
    impress_only = {"headers_footers", "speaker_notes"}
    assert gateway.parameters is not None
    assert impress_only <= set(gateway.parameters["properties"]["domain"]["enum"])

    draw = frozenset({DRAW_DOCUMENT_SERVICE})
    impress = frozenset({DRAW_DOCUMENT_SERVICE, IMPRESS_DOCUMENT_SERVICE})
    assert not impress_only & set(gateway.domains_for_services(draw))
    assert "shapes" in gateway.domains_for_services(draw)
    assert "page_comments" in gateway.domains_for_services(draw)
    assert "page_comments" in gateway.domains_for_services(impress)
    assert "page_comments" not in impress_only
    assert impress_only <= set(gateway.domains_for_services(impress))

    oa = to_openai_schema(gateway, doc_type="draw")
    [draw_oa] = narrow_delegate_domain_enums([oa], [gateway], draw)
    assert not impress_only & set(draw_oa["function"]["parameters"]["properties"]["domain"]["enum"])
    [impress_oa] = narrow_delegate_domain_enums([oa], [gateway], impress)
    assert impress_only <= set(impress_oa["function"]["parameters"]["properties"]["domain"]["enum"])
    mcp = to_mcp_schema(gateway, doc_type="draw")
    [draw_mcp] = narrow_delegate_domain_enums([mcp], [gateway], draw)
    assert not impress_only & set(draw_mcp["inputSchema"]["properties"]["domain"]["enum"])
    # Unknown services leave the schema alone; the shared instance enum is never mutated.
    assert narrow_delegate_domain_enums([oa], [gateway], None) == [oa]
    assert impress_only <= set(gateway.parameters["properties"]["domain"]["enum"])


def test_delegate_footnotes_hint_reaches_inner_instructions(
    writer_gateway: DelegateToSpecializedWriter,
) -> None:
    from unittest.mock import patch
    from plugin.framework.tool import ToolRegistry
    from plugin.writer.specialized.footnotes import FootnotesList
    from plugin.writer.specialized_base import SpecializedWorkflowFinished

    class _DummyFootnotesList(FootnotesList):
        def execute(self, ctx, **kwargs):
            return {"status": "ok", "footnotes": [], "endnotes": []}

    reg = ToolRegistry(services={})
    reg.register(_DummyFootnotesList())
    reg.register(SpecializedWorkflowFinished())
    reg.register(writer_gateway)

    ctx = MagicMock(spec=ToolContext)
    ctx.ctx = MagicMock()
    ctx.doc = MagicMock()
    ctx.doc.supportsService.return_value = True
    ctx.services = {"tools": reg}
    ctx.stop_checker = lambda: False

    mock_run_inner = MagicMock(return_value={"status": "ok", "finished": True, "answer": "done"})

    with patch("plugin.framework.queue_executor.main.call", side_effect=lambda fn, *a, **kw: fn(*a, **kw)):
        with patch("plugin.doc.specialized_base.run_inner_tool_agent", mock_run_inner):
            writer_gateway.execute(ctx, domain="footnotes", task="List footnotes")

    assert mock_run_inner.called
    instructions = mock_run_inner.call_args.kwargs["instructions"]
    assert "insert_after" in instructions


# Dummy specialized tools and fixtures, imported by test_document_research_specialized / test_python_domain_specialized / test_images.
from plugin.calc.base import ToolCalcSpecialBase
from plugin.calc.specialized import DelegateToSpecializedCalc
from plugin.contrib.smolagents.memory import FinalAnswerStep
from plugin.draw.base import ToolDrawSpecialBase
from plugin.draw.specialized import DelegateToSpecializedDraw
from plugin.framework.config import get_config as _real_get_config
from plugin.framework.tool import ToolBase, ToolRegistry, _is_specialized_domain_tool
from plugin.writer.specialized_base import (
    SpecializedWorkflowFinished,
    ToolWriterSpecialBase,
)


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


# Module-scope inheritance from ToolDrawSpecialBase leaks draw_test_domain into
# __subclasses__() for the whole pytest session and breaks exposure-parity checks.
# DummyDrawSpecialTool inherits ToolBase instead; the dummy_draw_tool fixture
# patches __subclasses__ only for Draw delegation tests and restores on teardown.
class DummyDrawSpecialTool(ToolBase):
    name = "dummy_draw_special_tool"
    description = "A dummy Draw specialized tool."
    parameters = {"type": "object", "properties": {}, "required": []}
    specialized_domain = "draw_test_domain"
    tier = "specialized"
    is_app_specialized_tool = True

    uno_services = ["com.sun.star.drawing.DrawingDocument"]

    def execute(self, ctx, **kwargs):
        return {"status": "ok", "message": "ok"}


ToolDrawSpecialBase.register(DummyDrawSpecialTool)


@pytest.fixture
def dummy_draw_tool(monkeypatch):
    """Temporarily include DummyDrawSpecialTool in ToolDrawSpecialBase subclasses and restore on teardown."""
    orig_subclasses = ToolDrawSpecialBase.__subclasses__
    monkeypatch.setattr(
        ToolDrawSpecialBase,
        "__subclasses__",
        classmethod(lambda cls: [*orig_subclasses(), DummyDrawSpecialTool]),
    )
    yield DummyDrawSpecialTool


@pytest.fixture
def registry():
    r = ToolRegistry(services={})
    r.register(DummyTableTool())
    r.register(DummyShapeTool())
    r.register(SpecializedWorkflowFinished())
    r.register(DelegateToSpecializedWriter())
    return r


@pytest.fixture
def registry_ctx(registry):
    ctx = MagicMock()
    ctx.services = {"tools": registry}
    ctx.ctx = MagicMock()
    return ctx


def _mock_get_config_for_sub_agent(key):
    if key == "chat_max_tokens":
        return 2048
    if key in ("chat_max_tool_rounds", "chatbot.max_tool_rounds"):
        return 25
    return _real_get_config(key)


@patch(
    "plugin.chatbot.smol_agent.get_config",
    side_effect=_mock_get_config_for_sub_agent,
)
@patch("plugin.chatbot.smol_agent.get_api_config", create=True)
@patch("plugin.chatbot.smol_agent.ToolCallingAgent")
@patch("plugin.chatbot.smol_agent.WriterAgentSmolModel")
@patch("plugin.chatbot.smol_agent.LlmClient")
def test_specialized_delegation_sub_agent_mode(
    mock_llm,
    mock_smol_model,
    mock_agent_class,
    mock_get_api_config,
    _mock_get_config,
    registry,
    registry_ctx,
):
    registry_ctx.ctx = MagicMock()

    active_domain = "initial_value"

    def mock_set_active_domain(domain):
        nonlocal active_domain
        active_domain = domain

    registry_ctx.set_active_domain_callback = mock_set_active_domain

    mock_agent_instance = MagicMock()

    dummy_final_step = FinalAnswerStep(output="Mocked agent summary")
    mock_agent_instance.run.return_value = [dummy_final_step]

    mock_agent_class.return_value = mock_agent_instance

    gateway_tool = registry.get("delegate_to_specialized_writer_toolset")

    registry_ctx.stop_checker = lambda: False

    result = gateway_tool.execute_safe(
        registry_ctx,
        domain="tables",
        task="Create a 3x3 table",
    )

    # Verify callback was NOT called (it remains initial_value)
    assert active_domain == "initial_value"

    assert result["status"] == "incomplete"
    assert result["code"] == "SPECIALIZED_WORKFLOW_INCOMPLETE"
    assert result["message"] == "Mocked agent summary"
    assert "result" not in result

    call_args = mock_agent_class.call_args
    assert call_args is not None
    smol_tools = call_args.kwargs.get("tools", [])

    # Sub-agent gets domain tools plus specialized_workflow_finished (active_domain registry rules).
    smol_tool_names = [t.name for t in smol_tools]
    assert "dummy_table_tool" in smol_tool_names
    assert "specialized_workflow_finished" in smol_tool_names


@patch(
    "plugin.chatbot.smol_agent.get_config",
    side_effect=_mock_get_config_for_sub_agent,
)
@patch("plugin.chatbot.smol_agent.get_api_config", create=True)
@patch("plugin.chatbot.smol_agent.ToolCallingAgent")
@patch("plugin.chatbot.smol_agent.WriterAgentSmolModel")
@patch("plugin.chatbot.smol_agent.LlmClient")
def test_shapes_delegation_includes_canvas_in_instructions(
    mock_llm,
    mock_smol_model,
    mock_agent_class,
    mock_get_api_config,
    _mock_get_config,
    registry,
    registry_ctx,
):
    registry_ctx.ctx = MagicMock()

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
    registry_ctx.doc = doc

    mock_agent_instance = MagicMock()
    mock_agent_instance.run.return_value = [FinalAnswerStep(output="Shapes done")]
    mock_agent_class.return_value = mock_agent_instance

    gateway_tool = registry.get("delegate_to_specialized_writer_toolset")
    registry_ctx.stop_checker = lambda: False

    result = gateway_tool.execute_safe(
        registry_ctx,
        domain="shapes",
        task="Add a rectangle",
    )
    assert result["status"] == "incomplete"
    assert result["code"] == "SPECIALIZED_WORKFLOW_INCOMPLETE"
    instructions = mock_agent_class.call_args.kwargs["instructions"]
    assert "Document canvas (Writer)" in instructions
    assert "210.0" in instructions
    smol_tools = mock_agent_class.call_args.kwargs.get("tools", [])
    assert any(t.name == "dummy_shape_tool" for t in smol_tools)


def test_is_specialized_domain_tool_helper(dummy_draw_tool):
    t = DummyTableTool()
    assert _is_specialized_domain_tool(t, "tables") is True
    assert _is_specialized_domain_tool(t, "images") is False
    c = DummyCalcSpecialTool()
    assert _is_specialized_domain_tool(c, "images") is True
    d = dummy_draw_tool()
    assert _is_specialized_domain_tool(d, "draw_test_domain") is True


def test_cross_cutting_venv_python_matches_python_domain():
    from plugin.calc.python.venv import RunVenvPythonScript

    r = RunVenvPythonScript()
    assert _is_specialized_domain_tool(r, "python") is True
    assert _is_specialized_domain_tool(r, "tables") is False


def test_run_venv_python_script_in_schemas_for_writer_when_domain_python(registry):
    from plugin.calc.python.venv import RunVenvPythonScript

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


def test_active_domain_schemas_include_calc_and_draw(registry, dummy_draw_tool):
    """Calc/Draw specialized tools must appear when active_domain matches."""
    registry.register(DummyCalcSpecialTool())
    registry.register(dummy_draw_tool())

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
    "plugin.chatbot.smol_agent.get_config",
    side_effect=_mock_get_config_for_sub_agent,
)
@patch("plugin.chatbot.smol_agent.get_api_config", create=True)
@patch("plugin.chatbot.smol_agent.ToolCallingAgent")
@patch("plugin.chatbot.smol_agent.WriterAgentSmolModel")
@patch("plugin.chatbot.smol_agent.LlmClient")
def test_calc_specialized_delegation_sub_agent(
    mock_llm,
    mock_smol_model,
    mock_agent_class,
    mock_get_api_config,
    _mock_get_config,
    registry,
    registry_ctx,
):
    registry.register(DummyCalcSpecialTool())
    registry.register(DelegateToSpecializedCalc())

    mock_agent_instance = MagicMock()
    mock_agent_instance.run.return_value = [FinalAnswerStep(output="Calc done")]
    mock_agent_class.return_value = mock_agent_instance

    gateway_tool = registry.get("delegate_to_specialized_calc_toolset")
    registry_ctx.stop_checker = lambda: False

    result = gateway_tool.execute_safe(registry_ctx, domain="images", task="Insert image")

    assert result["status"] == "incomplete"
    assert result["code"] == "SPECIALIZED_WORKFLOW_INCOMPLETE"
    assert result["message"] == "Calc done"
    assert "result" not in result

    call_args = mock_agent_class.call_args
    smol_tools = call_args.kwargs.get("tools", [])
    smol_tool_names = [t.name for t in smol_tools]
    assert "dummy_calc_images_tool" in smol_tool_names


@patch(
    "plugin.chatbot.smol_agent.get_config",
    side_effect=_mock_get_config_for_sub_agent,
)
@patch("plugin.chatbot.smol_agent.get_api_config", create=True)
@patch("plugin.chatbot.smol_agent.ToolCallingAgent")
@patch("plugin.chatbot.smol_agent.WriterAgentSmolModel")
@patch("plugin.chatbot.smol_agent.LlmClient")
def test_draw_specialized_delegation_sub_agent(
    mock_llm,
    mock_smol_model,
    mock_agent_class,
    mock_get_api_config,
    _mock_get_config,
    registry,
    registry_ctx,
    dummy_draw_tool,
):
    registry.register(dummy_draw_tool())
    registry.register(DelegateToSpecializedDraw())

    mock_agent_instance = MagicMock()
    mock_agent_instance.run.return_value = [FinalAnswerStep(output="Draw done")]
    mock_agent_class.return_value = mock_agent_instance

    gateway_tool = registry.get("delegate_to_specialized_draw_toolset")
    registry_ctx.stop_checker = lambda: False

    result = gateway_tool.execute_safe(registry_ctx, domain="draw_test_domain", task="Create shape")

    assert result["status"] == "incomplete"
    assert result["code"] == "SPECIALIZED_WORKFLOW_INCOMPLETE"
    assert result["message"] == "Draw done"
    assert "result" not in result

    call_args = mock_agent_class.call_args
    smol_tools = call_args.kwargs.get("tools", [])
    smol_tool_names = [t.name for t in smol_tools]
    assert "dummy_draw_special_tool" in smol_tool_names


@patch("plugin.chatbot.smol_agent.build_toolcalling_agent")
@patch("plugin.chatbot.smol_agent.SmolAgentExecutor")
def test_writer_delegate_python_includes_cross_cutting_venv_tool(mock_executor_cls, mock_build, registry):
    """Writer delegate must pick up Calc-registered ``run_venv_python_script`` (specialized_cross_cutting)."""
    from plugin.calc.python.venv import RunVenvPythonScript
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

    assert result["status"] == "incomplete"
    assert result["code"] == "SPECIALIZED_WORKFLOW_INCOMPLETE"
    assert result["message"] == "done"
    assert "result" not in result
    from plugin.framework.prompts import python_specialized_sub_agent_hint

    instructions = mock_build.call_args.kwargs["instructions"]
    assert python_specialized_sub_agent_hint("Writer") in instructions
    tools_passed = mock_build.call_args[0][1]
    names = [t.name for t in tools_passed]
    assert "run_venv_python_script" in names
    assert "specialized_workflow_finished" in names


def test_delegate_requires_document_lock_read_only_domains():
    from plugin.writer.specialized_base import DelegateToSpecializedWriter

    gw = DelegateToSpecializedWriter()
    assert gw.requires_document_lock({"domain": "document_research"}) is False
    assert gw.requires_document_lock({"domain": "web_research"}) is False
    assert gw.requires_document_lock('{"domain": "web_research"}') is False
    assert gw.requires_document_lock({"domain": "footnotes"}) is True
    assert gw.requires_document_lock({}) is True
