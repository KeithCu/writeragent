# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""Tests for consolidated plugin.framework.tool."""

import pytest
import time
import types
from plugin.framework.tool import ToolBase, ToolContext, ToolRegistry, ToolBaseDummy
from plugin.framework.service import ServiceRegistry
from plugin.tests.testing_utils import TestingFactory

# ── Tool Context Tests ───────────────────────────────────────────────

def test_tool_context_init():
    doc = object()
    ctx = object()
    doc_type = "writer"
    services = object()
    caller = "test"

    def status_cb(): pass
    def thinking_cb(): pass
    def stop_cb(): return False

    tc = ToolContext(
        doc=doc,
        ctx=ctx,
        doc_type=doc_type,
        services=services,
        caller=caller,
        status_callback=status_cb,
        append_thinking_callback=thinking_cb,
        stop_checker=stop_cb
    )

    assert tc.doc is doc
    assert tc.ctx is ctx
    assert tc.doc_type == doc_type
    assert tc.services is services
    assert tc.caller == caller
    assert tc.status_callback is status_cb
    assert tc.append_thinking_callback is thinking_cb
    assert tc.stop_checker is stop_cb

def test_tool_context_defaults():
    tc = ToolContext(doc=None, ctx=None, doc_type="calc", services=None)
    assert tc.caller == ""
    assert tc.status_callback is None
    assert tc.append_thinking_callback is None
    assert tc.stop_checker is None

# ── Tool Base Tests ──────────────────────────────────────────────────

class ValidTool(ToolBase):
    name = "edit_doc"
    description = "edit doc"
    parameters = {
        "properties": {
            "text": {"type": "string"}
        },
        "required": ["text"]
    }
    def execute(self, ctx, **kwargs):
        return {"status": "ok"}

class ReadTool(ToolBase):
    name = "get_info"
    def execute(self, ctx, **kwargs): pass

class ExplictMutateTool(ToolBase):
    name = "get_but_mutates"
    is_mutation = True
    def execute(self, ctx, **kwargs): pass

def test_detects_mutation():
    tool1 = ValidTool()
    assert tool1.detects_mutation() is True  # does not start with get_

    tool2 = ReadTool()
    assert tool2.detects_mutation() is False # starts with get_

    tool3 = ExplictMutateTool()
    assert tool3.detects_mutation() is True  # is_mutation is explicit

    class UnnamedTool(ToolBase):
        name = None
        def execute(self, ctx, **kwargs): pass
    assert UnnamedTool().detects_mutation() is True

def test_validate():
    tool = ValidTool()

    # Valid
    ok, err = tool.validate(text="hello")
    assert ok is True
    assert err is None

    # Missing required
    ok, err = tool.validate()
    assert ok is False
    assert "Missing required parameter: text" in err

    # Unknown param
    ok, err = tool.validate(text="hello", extra="bad")
    assert ok is False
    assert "Unknown parameter: extra" in err

def test_get_collection():
    tool = ValidTool()

    # Missing getter
    doc_bad = object()
    res = tool.get_collection(doc_bad, "getMyItems")
    assert isinstance(res, dict)
    assert res["status"] == "error"

    # Valid getter
    doc_good = TestingFactory.create_doc(doc_type="writer", content=[], items={"a": 1})
    coll = tool.get_collection(doc_good, "getMyItems")
    assert not isinstance(coll, dict)
    assert coll.hasByName("a")

def test_get_item():
    tool = ValidTool()
    doc = TestingFactory.create_doc(doc_type="writer", items={"item1": "val1", "item2": "val2"})

    # Missing getter entirely
    res = tool.get_item(object(), "getMyItems", "item1")
    assert isinstance(res, dict)
    assert res["status"] == "error"

    # Item not found
    res = tool.get_item(doc, "getMyItems", "missing")
    assert isinstance(res, dict)
    assert res["status"] == "error"
    assert "missing" in res["message"]
    assert "available" in res["details"]
    assert "item1" in res["details"]["available"]

    # Item found
    res = tool.get_item(doc, "getMyItems", "item1")
    assert res == "val1"

# ── Tool Registry Tests ─────────────────────────────────────────────

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
    return TestingFactory.create_context(doc_type=doc_type)

class TestRegister:
    def test_auto_discover(self):
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

        class AbstractTool(ToolBase): pass
        AbstractTool.__module__ = "mock_module"

        class DummyTool(ToolBaseDummy):
            name = "dummy_tool"
            def execute(self, ctx, **kwargs): pass
        DummyTool.__module__ = "mock_module"

        class ImportedTool(ToolBase):
            name = "imported_tool"
            def execute(self, ctx, **kwargs): pass
        ImportedTool.__module__ = "other_module"

        mock_module.GoodTool = GoodTool
        mock_module.AnotherTool = AnotherTool
        mock_module.AbstractTool = AbstractTool
        mock_module.DummyTool = DummyTool
        mock_module.ImportedTool = ImportedTool
        mock_module.NotATool = object()

        reg = _make_registry()
        reg.auto_discover(mock_module)

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
        names = [t.name for t in reg.get_tools(doc=TestingFactory.create_doc(doc_type="writer"))]
        assert "fake_tool" in names
        assert "universal_tool" in names

    def test_tools_for_calc_excludes_writer_only(self):
        reg = _make_registry(FakeTool(), AllDocTool())
        names = [t.name for t in reg.get_tools(doc=TestingFactory.create_doc(doc_type="calc"))]
        assert "fake_tool" not in names
        assert "universal_tool" in names

    def test_tools_for_none_returns_universal_only(self):
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
            def execute(self, ctx, **kwargs): return {"status": "ok"}

        reg = _make_registry(FakeTool(), SpecTool())
        names = [t.name for t in reg.get_tools(doc=TestingFactory.create_doc(doc_type="writer"))]
        assert "fake_tool" in names
        assert "spec_tool" not in names

    def test_exclude_tiers_empty_includes_specialized(self):
        class SpecTool(ToolBase):
            name = "spec_tool"
            description = "x"
            parameters = {"type": "object", "properties": {}}
            tier = "specialized"
            def execute(self, ctx, **kwargs): return {"status": "ok"}

        reg = _make_registry(FakeTool(), SpecTool())
        names = [t.name for t in reg.get_tools(doc=TestingFactory.create_doc(doc_type="writer"), exclude_tiers=())]
        assert "fake_tool" in names
        assert "spec_tool" in names

class TestLibrarianToolVisibility:
    def test_librarian_tools_are_hidden_by_default_in_main_chat_schema(self):
        from plugin.modules.chatbot.librarian import (
            LibrarianOnboardingTool,
            SwitchToDocumentModeTool,
        )

        class VisibleTool(ToolBase):
            name = "visible_tool"
            description = "Visible tool"
            parameters = {"type": "object", "properties": {}}
            uno_services = None
            def execute(self, ctx, **kwargs): return {"status": "ok"}

        reg = _make_registry(
            VisibleTool(),
            LibrarianOnboardingTool(),
            SwitchToDocumentModeTool(),
        )

        schemas = reg.get_schemas("openai", doc=TestingFactory.create_doc(doc_type="writer"))
        tool_names = {s["function"]["name"] for s in schemas}

        assert "visible_tool" in tool_names
        assert "librarian_onboarding" not in tool_names
        assert "switch_to_document_mode" not in tool_names

class TestSchemas:
    def test_openai_schemas(self):
        reg = _make_registry(FakeTool())
        schemas = reg.get_schemas("openai", doc=TestingFactory.create_doc(doc_type="writer"))
        assert len(schemas) == 1
        s = schemas[0]
        assert s["type"] == "function"
        assert s["function"]["name"] == "fake_tool"

    def test_mcp_schemas(self):
        reg = _make_registry(FakeTool())
        schemas = reg.get_schemas("mcp", doc=TestingFactory.create_doc(doc_type="writer"))
        assert len(schemas) == 1
        s = schemas[0]
        assert s["name"] == "fake_tool"
        assert "inputSchema" in s

class TestExecuteEventsAndInvalidation:
    def test_execute_emits_events(self):
        class MockEventBus:
            def __init__(self): self.events = []
            def emit(self, event, **kwargs): self.events.append((event, kwargs))

        class ToolWithParams(ToolBase):
            name = "tool_with_params"
            description = "Tool with params"
            parameters = {"type": "object", "properties": {"arg1": {"type": "string"}}}
            uno_services = ["com.sun.star.text.TextDocument"]
            def execute(self, ctx, **kwargs): return {"status": "success"}

        services = ServiceRegistry()
        events = MockEventBus()
        services.register("events", events)
        reg = ToolRegistry(services)
        reg.register(ToolWithParams())

        ctx = ToolContext(doc=TestingFactory.create_doc(doc_type="writer"), ctx=None, doc_type="writer", services=services, caller="test")
        result = reg.execute("tool_with_params", ctx, arg1="val1", extra="ignored")

        assert result == {"status": "success"}
        assert len(events.events) == 2
        assert events.events[0][0] == "tool:executing"
        assert events.events[1][0] == "tool:completed"

    def test_execute_failure_emits_events(self):
        class MockEventBus:
            def __init__(self): self.events = []
            def emit(self, event, **kwargs): self.events.append((event, kwargs))

        class FailingToolWithParams(ToolBase):
            name = "failing_tool_with_params"
            description = "Tool with params that fails"
            parameters = {"type": "object", "properties": {"arg1": {"type": "string"}}}
            uno_services = ["com.sun.star.text.TextDocument"]
            def execute(self, ctx, **kwargs): raise RuntimeError("something went wrong")

        services = ServiceRegistry()
        events = MockEventBus()
        services.register("events", events)
        reg = ToolRegistry(services)
        reg.register(FailingToolWithParams())

        ctx = ToolContext(doc=TestingFactory.create_doc(doc_type="writer"), ctx=None, doc_type="writer", services=services, caller="test")
        result = reg.execute("failing_tool_with_params", ctx, arg1="val1")

        assert result["status"] == "error"
        assert "something went wrong" in result["message"]
        assert len(events.events) == 2
        assert events.events[0][0] == "tool:executing"
        assert events.events[1][0] == "tool:failed"

class TestToolIsolation:
    def test_tool_execution_error(self):
        class FailingTool(ToolBase):
            name = "test_fail"
            description = "x"
            parameters = {"type": "object", "properties": {}}
            def execute(self, ctx, **kwargs): raise ValueError("Test error")

        registry = ToolRegistry(services={})
        registry.register(FailingTool())

        class DummyContext:
            doc = None; doc_type = None; caller = None
        result = registry.execute("test_fail", DummyContext())
        assert result["status"] == "error"
        assert "Test error" in result["details"]["original_error"]
        assert result["code"] == "TOOL_EXECUTION_ERROR"

    def test_tool_timeout(self):
        class SlowTool(ToolBase):
            name = "test_slow"
            description = "x"
            timeout = 0.1
            parameters = {"type": "object", "properties": {}}
            def is_async(self): return True
            def execute(self, ctx, **kwargs):
                time.sleep(2)
                return {"status": "ok"}

        registry = ToolRegistry(services={})
        registry.register(SlowTool())
        class DummyContext:
            doc = None; doc_type = None; caller = None
        result = registry.execute("test_slow", DummyContext())
        assert result["status"] == "error"
        assert result["code"] == "TOOL_TIMEOUT"
