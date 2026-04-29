"""Tests for plugin.framework.tool_registry."""

import pytest

from plugin.framework.tool_base import ToolBase
from plugin.framework.tool_context import ToolContext

class MockDoc:
    def __init__(self, doc_type="writer"):
        self.doc_type = doc_type

    def supportsService(self, svc):
        if self.doc_type == "writer" and svc == "com.sun.star.text.TextDocument": return True
        if self.doc_type == "calc" and svc == "com.sun.star.sheet.SpreadsheetDocument": return True
        if self.doc_type == "draw" and svc == "com.sun.star.drawing.DrawingDocument": return True
        if self.doc_type == "impress" and svc == "com.sun.star.presentation.PresentationDocument": return True
        return False

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
    uno_services = ["com.sun.star.text.TextDocument"]

    def execute(self, ctx, **kwargs):
        return {"status": "ok", "text": kwargs["text"]}


class AllDocTool(ToolBase):
    name = "universal_tool"
    description = "Works everywhere"
    parameters = {"type": "object", "properties": {}}
    uno_services = None

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
        doc=MockDoc(doc_type), ctx=None, doc_type=doc_type,
        services=ServiceRegistry(), caller="test"
    )


class TestRegister:
    def test_auto_discover(self):
        # Create a fake module to test auto_discover
        from plugin.framework.tool_base import ToolBaseDummy
        import types

        mock_module = types.ModuleType("mock_module")

        class GoodTool(ToolBase):
            name = "good_tool"
            description = "A good tool"
            def execute(self, ctx, **kwargs): pass
        GoodTool.__module__ = "mock_module"

        class AnotherTool(ToolBase):
            name = "another_tool"
            description = "Another tool"
            def execute(self, ctx, **kwargs): pass
        AnotherTool.__module__ = "mock_module"

        class AbstractTool(ToolBase):
            pass # No name defined
        AbstractTool.__module__ = "mock_module"

        class DummyTool(ToolBaseDummy):
            name = "dummy_tool"
            def execute(self, ctx, **kwargs): pass
        DummyTool.__module__ = "mock_module"

        class ImportedTool(ToolBase):
            name = "imported_tool"
            def execute(self, ctx, **kwargs): pass
        ImportedTool.__module__ = "other_module" # Simulate an imported class

        # Add classes to module
        mock_module.GoodTool = GoodTool
        mock_module.AnotherTool = AnotherTool
        mock_module.AbstractTool = AbstractTool
        mock_module.DummyTool = DummyTool
        mock_module.ImportedTool = ImportedTool
        mock_module.NotATool = object()

        reg = _make_registry()
        reg.auto_discover(mock_module)

        # Should only register GoodTool and AnotherTool
        assert reg.get("good_tool") is not None
        assert reg.get("another_tool") is not None
        assert reg.get("dummy_tool") is None
        assert reg.get("imported_tool") is None
        assert len(reg) == 2

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
        names = [t.name for t in reg.get_tools(doc=MockDoc("writer"))]
        assert "fake_tool" in names
        assert "universal_tool" in names

    def test_tools_for_calc_excludes_writer_only(self):
        reg = _make_registry(FakeTool(), AllDocTool())
        names = [t.name for t in reg.get_tools(doc=MockDoc("calc"))]
        assert "fake_tool" not in names
        assert "universal_tool" in names

    def test_tools_for_none_returns_universal_only(self):
        """When doc_type is None (unknown), only universal tools are returned."""
        reg = _make_registry(FakeTool(), AllDocTool())
        names = [t.name for t in reg.get_tools(doc=None)]
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

    def test_execution_wrong_type_returns_error(self):
        class TypeCheckingTool(ToolBase):
            name = "type_checker"
            description = "Checks type"
            parameters = {"type": "object", "properties": {"text": {"type": "string"}}}
            def execute(self, ctx, **kwargs):
                text = kwargs.get("text")
                if not isinstance(text, str):
                    raise TypeError("text must be a string")
                return {"status": "ok", "text": text}

        reg = _make_registry(TypeCheckingTool())
        ctx = _make_ctx("writer")

        # Pass a list instead of string
        result = reg.execute("type_checker", ctx, text=["not", "a", "string"])
        assert result["status"] == "error"
        assert "text must be a string" in result.get("message", "")


class TestExcludeSpecializedTiers:
    def test_default_excludes_specialized_tier(self):
        class SpecTool(ToolBase):
            name = "spec_tool"
            description = "x"
            parameters = {"type": "object", "properties": {}}
            tier = "specialized"

            def execute(self, ctx, **kwargs):
                return {"status": "ok"}

        reg = _make_registry(FakeTool(), SpecTool())
        names = [t.name for t in reg.get_tools(doc=MockDoc("writer"))]
        assert "fake_tool" in names
        assert "spec_tool" not in names

    def test_exclude_tiers_empty_includes_specialized(self):
        class SpecTool(ToolBase):
            name = "spec_tool"
            description = "x"
            parameters = {"type": "object", "properties": {}}
            tier = "specialized"

            def execute(self, ctx, **kwargs):
                return {"status": "ok"}

        reg = _make_registry(FakeTool(), SpecTool())
        names = [t.name for t in reg.get_tools(doc=MockDoc("writer"), exclude_tiers=())]
        assert "fake_tool" in names
        assert "spec_tool" in names

    def test_create_shape_specialized_hidden_for_all(self):
        class CreateShapeStub(ToolBase):
            name = "create_shape"
            description = "stub"
            parameters = {"type": "object", "properties": {}}
            tier = "specialized"
            uno_services = [
                "com.sun.star.text.TextDocument",
                "com.sun.star.drawing.DrawingDocument",
            ]

            def execute(self, ctx, **kwargs):
                return {"status": "ok"}

        reg = _make_registry(FakeTool(), CreateShapeStub())
        w = [t.name for t in reg.get_tools(doc=MockDoc("writer"))]
        d = [t.name for t in reg.get_tools(doc=MockDoc("draw"))]
        assert "create_shape" not in w
        assert "create_shape" not in d


class TestLibrarianToolVisibility:
    def test_librarian_tools_are_hidden_by_default_in_main_chat_schema(self):
        # Import real librarian tools to ensure their tier affects the registry output.
        from plugin.modules.chatbot.librarian import (
            LibrarianOnboardingTool,
            SwitchToDocumentModeTool,
        )

        class VisibleTool(ToolBase):
            name = "visible_tool"
            description = "Visible tool"
            parameters = {"type": "object", "properties": {}}

            # Universal tool (no uno_services / doc_types), should be included.
            uno_services = None

            def execute(self, ctx, **kwargs):
                return {"status": "ok"}

        reg = _make_registry(
            VisibleTool(),
            LibrarianOnboardingTool(),
            SwitchToDocumentModeTool(),
        )

        schemas = reg.get_schemas("openai", doc=MockDoc("writer"))
        tool_names = {s["function"]["name"] for s in schemas}

        assert "visible_tool" in tool_names
        assert "librarian_onboarding" not in tool_names
        assert "switch_to_document_mode" not in tool_names


class TestSchemas:
    def test_openai_schemas(self):
        reg = _make_registry(FakeTool())
        schemas = reg.get_schemas("openai", doc=MockDoc("writer"))
        assert len(schemas) == 1
        s = schemas[0]
        assert s["type"] == "function"
        assert s["function"]["name"] == "fake_tool"

    def test_mcp_schemas(self):
        reg = _make_registry(FakeTool())
        schemas = reg.get_schemas("mcp", doc=MockDoc("writer"))
        assert len(schemas) == 1
        s = schemas[0]
        assert s["name"] == "fake_tool"
        assert "inputSchema" in s


class TestExecuteEventsAndInvalidation:
    """Tests that execute() emits events."""

    def test_execute_emits_events(self):
        class MockEventBus:
            def __init__(self):
                self.events = []

            def emit(self, event, **kwargs):
                self.events.append((event, kwargs))

        class ToolWithParams(ToolBase):
            name = "tool_with_params"
            description = "Tool with params"
            parameters = {"type": "object", "properties": {"arg1": {"type": "string"}}}
            uno_services = ["com.sun.star.text.TextDocument"]

            def execute(self, ctx, **kwargs):
                return {"status": "success"}

        services = ServiceRegistry()
        events = MockEventBus()
        services.register("events", events)

        reg = ToolRegistry(services)
        reg.register(ToolWithParams())

        ctx = ToolContext(doc=MockDoc("writer"), ctx=None, doc_type="writer", services=services, caller="test")
        result = reg.execute("tool_with_params", ctx, arg1="val1", extra="ignored")

        assert result == {"status": "success"}
        assert len(events.events) == 2
        assert events.events[0][0] == "tool:executing"
        assert events.events[1][0] == "tool:completed"

    def test_execute_failure_emits_events(self):
        class MockEventBus:
            def __init__(self):
                self.events = []

            def emit(self, event, **kwargs):
                self.events.append((event, kwargs))

        class FailingToolWithParams(ToolBase):
            name = "failing_tool_with_params"
            description = "Tool with params that fails"
            parameters = {"type": "object", "properties": {"arg1": {"type": "string"}}}
            uno_services = ["com.sun.star.text.TextDocument"]

            def execute(self, ctx, **kwargs):
                raise RuntimeError("something went wrong")

        services = ServiceRegistry()
        events = MockEventBus()
        services.register("events", events)

        reg = ToolRegistry(services)
        reg.register(FailingToolWithParams())

        ctx = ToolContext(doc=MockDoc("writer"), ctx=None, doc_type="writer", services=services, caller="test")
        result = reg.execute("failing_tool_with_params", ctx, arg1="val1")

        assert result["status"] == "error"
        assert "something went wrong" in result["message"]

        assert len(events.events) == 2
        assert events.events[0][0] == "tool:executing"
        assert events.events[1][0] == "tool:failed"
        assert "something went wrong" in events.events[1][1]["error"]

class TestToolIsolation:
    def test_tool_execution_error(self):
        from plugin.framework.tool_registry import ToolRegistry
        from plugin.framework.tool_base import ToolBase

        class FailingTool(ToolBase):
            name = "test_fail"
            description = "Test tool that raises ValueError"
            parameters = {"type": "object", "properties": {}}

            def execute(self, ctx, **kwargs):
                raise ValueError("Test error")

        registry = ToolRegistry(services={})
        registry.register(FailingTool())

        class DummyContext:
            doc_type = None
            caller = None

        result = registry.execute("test_fail", DummyContext())

        assert result["status"] == "error"
        assert "Test error" in result["details"]["original_error"]
        assert result["code"] == "TOOL_EXECUTION_ERROR"

    def test_tool_timeout(self):
        from plugin.framework.tool_registry import ToolRegistry
        from plugin.framework.tool_base import ToolBase
        import time

        class SlowTool(ToolBase):
            name = "test_slow"
            description = "Test tool that sleeps past timeout"
            timeout = 0.1
            parameters = {"type": "object", "properties": {}}

            def is_async(self):
                # allow it to run in the test thread pool
                return True

            def execute(self, ctx, **kwargs):
                time.sleep(2)
                return {"status": "ok"}

        registry = ToolRegistry(services={})
        registry.register(SlowTool())

        class DummyContext:
            doc_type = None
            caller = None

        result = registry.execute("test_slow", DummyContext())

        assert result["status"] == "error"
        assert result["code"] == "TOOL_TIMEOUT"
        assert "Tool timed out" in result["message"]

