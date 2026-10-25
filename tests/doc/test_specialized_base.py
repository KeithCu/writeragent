# WriterAgent - AI Writing Assistant for LibreOffice
# SPDX-License-Identifier: GPL-3.0-or-later

"""Unit tests for DelegateToSpecializedBase (domain validation and mutation lock)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from plugin.framework.tool import ToolContext
from plugin.mcp.document_mutation_gate import _tool_needs_document_mutation_gate
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

    # Vision requires lock even though listed in _READ_ONLY_DOMAINS
    assert writer_gateway.requires_document_lock({"domain": "vision"}) is True

    # Mutating domains require lock
    assert writer_gateway.requires_document_lock({"domain": "tables"}) is True
    assert writer_gateway.requires_document_lock({"domain": "shapes"}) is True


def test_mcp_mutation_gate_non_str_domain(writer_gateway: DelegateToSpecializedWriter) -> None:
    """_tool_needs_document_mutation_gate must not crash on unhashable dict domain arguments."""
    needs_gate = _tool_needs_document_mutation_gate(writer_gateway, {"domain": {"type": "object"}})
    assert needs_gate is True


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
    from plugin.framework.tool import ToolRegistry
    from plugin.writer.specialized_base import SpecializedWorkflowFinished, ToolWriterStructuralBase

    class _DummyStructuralTool(ToolWriterStructuralBase):
        name = "dummy_structural_tool"
        description = "A dummy structural tool."
        parameters = {"type": "object", "properties": {}}

        def execute(self, ctx, **kw):
            return {"status": "ok"}

    reg = ToolRegistry(services={})
    reg.register(_DummyStructuralTool())
    reg.register(SpecializedWorkflowFinished())
    reg.register(writer_gateway)

    ctx = MagicMock(spec=ToolContext)
    ctx.ctx = MagicMock()
    ctx.doc = MagicMock()
    ctx.doc.supportsService.return_value = True
    ctx.services = {"tools": reg}
    ctx.stop_checker = lambda: False

    with patch("plugin.framework.queue_executor.execute_on_main_thread", side_effect=lambda fn, *a, **kw: fn(*a, **kw)):
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


def test_delegate_status_inner_tool_error(
    writer_gateway: DelegateToSpecializedWriter,
) -> None:
    """Discussion #2029: Inner tool error becomes status: 'error', code SPECIALIZED_TOOL_ERROR."""
    from unittest.mock import patch
    from plugin.framework.tool import ToolRegistry
    from plugin.writer.specialized_base import SpecializedWorkflowFinished, ToolWriterStructuralBase

    class _FailingStructuralTool(ToolWriterStructuralBase):
        name = "failing_structural_tool"
        description = "A failing structural tool."
        parameters = {"type": "object", "properties": {}}

        def execute(self, ctx, **kw):
            return {"status": "error", "message": "Failed to read page objects"}

    reg = ToolRegistry(services={})
    reg.register(_FailingStructuralTool())
    reg.register(SpecializedWorkflowFinished())
    reg.register(writer_gateway)

    ctx = MagicMock(spec=ToolContext)
    ctx.ctx = MagicMock()
    ctx.doc = MagicMock()
    ctx.doc.supportsService.return_value = True
    ctx.services = {"tools": reg}
    ctx.stop_checker = lambda: False

    def fake_run_inner(ctx, tools, **kwargs):
        tool_map = {t.name: t for t in tools}
        tool_map["failing_structural_tool"]()
        return tool_map["specialized_workflow_finished"](answer="Final attempted answer")

    with patch("plugin.framework.queue_executor.execute_on_main_thread", side_effect=lambda fn, *a, **kw: fn(*a, **kw)):
        with patch("plugin.doc.specialized_base.run_inner_tool_agent", side_effect=fake_run_inner):
            result = writer_gateway.execute(ctx, domain="structural", task="Inspect page")

    assert result["status"] == "error"
    assert result["code"] == "SPECIALIZED_TOOL_ERROR"
    assert "Failed to read page objects" in result["message"]
    assert "Final attempted answer" in result["message"]


def test_delegate_status_incomplete_no_finish_no_tool_error(
    writer_gateway: DelegateToSpecializedWriter,
) -> None:
    """Discussion #2029: No finish tool call and no tool error becomes status: 'incomplete'."""
    from unittest.mock import patch
    from plugin.framework.tool import ToolRegistry
    from plugin.writer.specialized_base import SpecializedWorkflowFinished, ToolWriterStructuralBase

    class _DummyStructuralTool(ToolWriterStructuralBase):
        name = "dummy_structural_tool"
        description = "A dummy structural tool."
        parameters = {"type": "object", "properties": {}}

        def execute(self, ctx, **kw):
            return {"status": "ok"}

    reg = ToolRegistry(services={})
    reg.register(_DummyStructuralTool())
    reg.register(SpecializedWorkflowFinished())
    reg.register(writer_gateway)

    ctx = MagicMock(spec=ToolContext)
    ctx.ctx = MagicMock()
    ctx.doc = MagicMock()
    ctx.doc.supportsService.return_value = True
    ctx.services = {"tools": reg}
    ctx.stop_checker = lambda: False

    with patch("plugin.framework.queue_executor.execute_on_main_thread", side_effect=lambda fn, *a, **kw: fn(*a, **kw)):
        with patch("plugin.doc.specialized_base.run_inner_tool_agent", return_value="I searched but could not find the heading."):
            result = writer_gateway.execute(ctx, domain="structural", task="Find heading")

    assert result["status"] == "incomplete"
    assert result["code"] == "SPECIALIZED_WORKFLOW_INCOMPLETE"
    assert result["message"] == "I searched but could not find the heading."


def test_delegate_status_user_stopped_left_unchanged(
    writer_gateway: DelegateToSpecializedWriter,
) -> None:
    """Discussion #2029: User-stopped or crash dict already status: 'error' is left unchanged."""
    from unittest.mock import patch
    from plugin.framework.tool import ToolRegistry
    from plugin.writer.specialized_base import SpecializedWorkflowFinished, ToolWriterStructuralBase

    class _DummyStructuralTool(ToolWriterStructuralBase):
        name = "dummy_structural_tool"
        description = "A dummy structural tool."
        parameters = {"type": "object", "properties": {}}

        def execute(self, ctx, **kw):
            return {"status": "ok"}

    reg = ToolRegistry(services={})
    reg.register(_DummyStructuralTool())
    reg.register(SpecializedWorkflowFinished())
    reg.register(writer_gateway)

    ctx = MagicMock(spec=ToolContext)
    ctx.ctx = MagicMock()
    ctx.doc = MagicMock()
    ctx.doc.supportsService.return_value = True
    ctx.services = {"tools": reg}
    ctx.stop_checker = lambda: False

    with patch("plugin.framework.queue_executor.execute_on_main_thread", side_effect=lambda fn, *a, **kw: fn(*a, **kw)):
        with patch("plugin.doc.specialized_base.run_inner_tool_agent") as mock_run_inner:
            mock_run_inner.return_value = {
                "status": "error",
                "error": "Task stopped by user.",
                "code": "USER_STOPPED",
            }
            result = writer_gateway.execute(ctx, domain="structural", task="Find heading")

    assert result["status"] == "error"
    assert result["code"] == "USER_STOPPED"
    assert result["error"] == "Task stopped by user."


def test_gateway_enum_is_sorted_and_unique(writer_gateway: DelegateToSpecializedWriter) -> None:
    assert writer_gateway.parameters is not None
    enum = writer_gateway.parameters["properties"]["domain"]["enum"]
    assert enum == sorted(enum)
    assert len(enum) == len(set(enum))


def test_delegate_completed_message_uses_static_msgid(
    writer_gateway: DelegateToSpecializedWriter,
) -> None:
    from unittest.mock import patch
    from plugin.framework.tool import ToolRegistry
    from plugin.writer.specialized_base import SpecializedWorkflowFinished, ToolWriterStructuralBase

    class _DummyStructuralTool(ToolWriterStructuralBase):
        name = "dummy_structural_tool"
        description = "A dummy structural tool."
        parameters = {"type": "object", "properties": {}}

        def execute(self, ctx, **kw):
            return {"status": "ok"}

    reg = ToolRegistry(services={})
    reg.register(_DummyStructuralTool())
    reg.register(SpecializedWorkflowFinished())
    reg.register(writer_gateway)

    ctx = MagicMock(spec=ToolContext)
    ctx.ctx = MagicMock()
    ctx.doc = MagicMock()
    ctx.doc.supportsService.return_value = True
    ctx.services = {"tools": reg}
    ctx.stop_checker = lambda: False

    def fake_run_inner(ctx, tools, **kwargs):
        tool_map = {t.name: t for t in tools}
        tool_map["specialized_workflow_finished"](answer="Done")
        return "All done."

    with patch("plugin.framework.queue_executor.execute_on_main_thread", side_effect=lambda fn, *a, **kw: fn(*a, **kw)):
        with patch("plugin.doc.specialized_base.run_inner_tool_agent", side_effect=fake_run_inner):
            result = writer_gateway.execute(ctx, domain="structural", task="Inspect page")

    assert result["status"] == "ok"
    assert result["message"] == "Specialized task (structural) completed."


def test_draw_gateway_enum_narrowed_by_uno_services_not_doc_type_label() -> None:
    """Impress and Draw both carry doc_type "draw"; only PresentationDocument keeps slide domains."""
    import plugin.draw.headers_footers  # noqa: F401
    import plugin.draw.notes  # noqa: F401
    from plugin.doc.doc_type import DRAW_DOCUMENT_SERVICE, IMPRESS_DOCUMENT_SERVICE
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
