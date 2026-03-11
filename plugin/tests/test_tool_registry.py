"""Tests for plugin.framework.tool_registry."""

import pytest

from plugin.framework.tool_base import ToolBase
from plugin.framework.tool_context import ToolContext
from plugin.framework.tool_registry import ToolRegistry
from plugin.framework.service_registry import ServiceRegistry


class FakeTool(ToolBase):
    name = "fake_tool"
    description = "A fake tool"
    parameters = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }
    doc_types = ["writer"]

    def execute(self, ctx, **kwargs):
        return {"status": "ok", "text": kwargs["text"]}


class AllDocTool(ToolBase):
    name = "universal_tool"
    description = "Works everywhere"
    parameters = {"type": "object", "properties": {}}
    doc_types = None

    def execute(self, ctx, **kwargs):
        return {"status": "ok"}


class FailingTool(ToolBase):
    name = "fail_tool"
    description = "Always fails"
    parameters = {"type": "object", "properties": {}}

    def execute(self, ctx, **kwargs):
        raise RuntimeError("intentional failure")


def _make_registry(*tools):
    services = ServiceRegistry()
    reg = ToolRegistry(services)
    for t in tools:
        reg.register(t)
    return reg


def _make_ctx(doc_type="writer"):
    return ToolContext(
        doc=None, ctx=None, doc_type=doc_type,
        services=ServiceRegistry(), caller="test"
    )


class TestRegister:
    def test_register_and_get(self):
        reg = _make_registry(FakeTool())
        assert reg.get("fake_tool") is not None
        assert reg.get("missing") is None

    def test_tool_names(self):
        reg = _make_registry(FakeTool(), AllDocTool())
        assert set(reg.tool_names) == {"fake_tool", "universal_tool"}

    def test_len(self):
        reg = _make_registry(FakeTool(), AllDocTool())
        assert len(reg) == 2


class TestDocTypeFiltering:
    def test_tools_for_writer(self):
        reg = _make_registry(FakeTool(), AllDocTool())
        names = [t.name for t in reg.tools_for_doc_type("writer")]
        assert "fake_tool" in names
        assert "universal_tool" in names

    def test_tools_for_calc_excludes_writer_only(self):
        reg = _make_registry(FakeTool(), AllDocTool())
        names = [t.name for t in reg.tools_for_doc_type("calc")]
        assert "fake_tool" not in names
        assert "universal_tool" in names

    def test_tools_for_none_returns_universal_only(self):
        """When doc_type is None (unknown), only universal tools are returned."""
        reg = _make_registry(FakeTool(), AllDocTool())
        names = [t.name for t in reg.tools_for_doc_type(None)]
        assert names == ["universal_tool"]


class TestExecute:
    def test_successful_execution(self):
        reg = _make_registry(FakeTool())
        ctx = _make_ctx("writer")
        result = reg.execute("fake_tool", ctx, text="hello")
        assert result == {"status": "ok", "text": "hello"}

    def test_unknown_tool_raises(self):
        reg = _make_registry()
        ctx = _make_ctx()
        with pytest.raises(KeyError, match="Unknown tool"):
            reg.execute("nope", ctx)

    def test_incompatible_doc_type_raises(self):
        reg = _make_registry(FakeTool())
        ctx = _make_ctx("calc")
        with pytest.raises(ValueError, match="does not support"):
            reg.execute("fake_tool", ctx, text="x")

    def test_validation_failure_returns_error(self):
        reg = _make_registry(FakeTool())
        ctx = _make_ctx("writer")
        result = reg.execute("fake_tool", ctx)  # missing 'text'
        assert result["status"] == "error"
        assert "Missing required" in result.get("error", result.get("message", ""))

    def test_execution_failure_returns_error(self):
        reg = _make_registry(FailingTool())
        ctx = _make_ctx("writer")
        result = reg.execute("fail_tool", ctx)
        assert result["status"] == "error"
        assert "intentional failure" in result.get("error", result.get("message", ""))


class TestSchemas:
    def test_openai_schemas(self):
        reg = _make_registry(FakeTool())
        schemas = reg.get_openai_schemas("writer")
        assert len(schemas) == 1
        s = schemas[0]
        assert s["type"] == "function"
        assert s["function"]["name"] == "fake_tool"

    def test_mcp_schemas(self):
        reg = _make_registry(FakeTool())
        schemas = reg.get_mcp_schemas("writer")
        assert len(schemas) == 1
        s = schemas[0]
        assert s["name"] == "fake_tool"
        assert "inputSchema" in s


class TestExecuteEventsAndInvalidation:
    """Tests that execute() emits events and invalidates document cache (from test_registry)."""

    def test_execute_emits_events_and_invalidates_cache(self):
        class MockEventBus:
            def __init__(self):
                self.events = []

            def emit(self, event, **kwargs):
                self.events.append((event, kwargs))

        class MockDocumentService:
            def __init__(self):
                self.invalidated = []

            def invalidate_cache(self, doc):
                self.invalidated.append(doc)

        class ToolWithParams(ToolBase):
            name = "tool_with_params"
            description = "Tool with params"
            parameters = {"type": "object", "properties": {"arg1": {"type": "string"}}}
            doc_types = ["writer"]

            def execute(self, ctx, **kwargs):
                return {"status": "success"}

        services = ServiceRegistry()
        events = MockEventBus()
        doc_svc = MockDocumentService()
        services.register_instance("events", events)
        services.register_instance("document", doc_svc)

        reg = ToolRegistry(services)
        reg.register(ToolWithParams())

        ctx = ToolContext(doc=object(), ctx=None, doc_type="writer", services=services, caller="test")
        result = reg.execute("tool_with_params", ctx, arg1="val1", extra="ignored")

        assert result == {"status": "success"}
        assert len(events.events) == 2
        assert events.events[0][0] == "tool:executing"
        assert events.events[1][0] == "tool:completed"
        assert len(doc_svc.invalidated) == 1
        assert doc_svc.invalidated[0] is ctx.doc


def test_tool_registry_discover(tmpdir):
    """Discovery from a directory (from test_registry)."""
    import sys

    services = ServiceRegistry()
    registry = ToolRegistry(services)

    pkg_dir = tmpdir.mkdir("fake_tools")
    pkg_dir.join("__init__.py").write("")
    pkg_dir.join("my_tool.py").write("""
from plugin.framework.tool_base import ToolBase
class MyFakeTool(ToolBase):
    name = "my_fake"
    def execute(self, ctx, **kwargs): pass
""")

    sys.path.insert(0, str(tmpdir))
    try:
        registry.discover(str(pkg_dir), "fake_tools")
        assert "my_fake" in registry.tool_names
    finally:
        sys.path.pop(0)
