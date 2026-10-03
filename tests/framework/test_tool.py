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

import logging
import pytest
import threading
import time
import types
from unittest.mock import MagicMock, patch

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

    class DomainListTool(ToolBase):
        name = "image_list"
        def execute(self, ctx, **kwargs): pass

    class DomainGetInfoTool(ToolBase):
        name = "style_get_info"
        def execute(self, ctx, **kwargs): pass

    class DomainMutateTool(ToolBase):
        name = "image_insert"
        def execute(self, ctx, **kwargs): pass

    assert DomainListTool().detects_mutation() is False
    assert DomainGetInfoTool().detects_mutation() is False
    assert DomainMutateTool().detects_mutation() is True

    from plugin.writer.specialized_base import SpecializedWorkflowFinished

    assert SpecializedWorkflowFinished().detects_mutation() is False

def test_requires_document_lock_default_matches_detects_mutation():
    tool1 = ValidTool()
    assert tool1.requires_document_lock() is tool1.detects_mutation()

    tool2 = ReadTool()
    assert tool2.requires_document_lock() is tool2.detects_mutation()

    tool3 = ExplictMutateTool()
    assert tool3.requires_document_lock({"any": "args"}) is tool3.detects_mutation()

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


def test_validate_empty_properties_rejects_unknown_kwargs():
    """Gemini/Groq can invent kwargs; empty properties must still reject them."""
    tool = AllDocTool()
    ok, err = tool.validate(hallucinated="yes")
    assert ok is False
    assert err is not None
    assert "Unknown parameter: hallucinated" in err


def test_execute_strips_unknown_kwargs_when_properties_empty():
    """Registry strip must treat properties {} as authoritative (same as validate)."""

    class CaptureTool(ToolBase):
        name = "capture_no_arg"
        description = "capture kwargs"
        parameters = {"type": "object", "properties": {}}
        uno_services = None

        def execute(self, ctx, **kwargs):
            return {"status": "ok", "kwargs": dict(kwargs)}

    reg = _make_registry(CaptureTool())
    ctx = _make_ctx("writer")
    result = reg.execute("capture_no_arg", ctx, hallucinated="yes")
    assert result["status"] == "ok"
    assert result["kwargs"] == {}

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

class RangeIndexTool(ToolBase):
    name = "range_index_tool"
    description = "Indexes range[0] the way Calc filter tools do"
    parameters = {
        "type": "object",
        "properties": {"range": {"type": "array", "items": {"type": "string"}}},
        "required": ["range"],
    }
    uno_services = None

    def execute(self, ctx, **kwargs):
        return {"status": "ok", "range": kwargs["range"], "first": kwargs["range"][0]}

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

    def test_register_warns_same_class_name_different_module(self, caplog):
        def _exec(self, ctx, **kwargs):
            return {"status": "ok"}

        DrawCls = type("UpsertShape", (ToolBase,), {"name": "shape_upsert", "description": "draw", "execute": _exec})
        DrawCls.__module__ = "plugin.draw.shapes"
        WriterCls = type("UpsertShape", (ToolBase,), {"name": "shape_upsert", "description": "writer", "execute": _exec})
        WriterCls.__module__ = "plugin.writer.specialized.shapes"
        reg = ToolRegistry(ServiceRegistry())
        wa_logger = logging.getLogger("writeragent")
        old_propagate = wa_logger.propagate
        wa_logger.propagate = True
        try:
            with caplog.at_level("WARNING", logger="writeragent.tools"):
                reg.register(DrawCls())
                reg.register(WriterCls())
            assert any("plugin.draw.shapes" in r.message and "plugin.writer.specialized.shapes" in r.message for r in caplog.records)
            assert reg.get("shape_upsert").description == "writer"
        finally:
            wa_logger.propagate = old_propagate

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
    def test_string_range_is_wrapped_before_execute(self):
        reg = _make_registry(RangeIndexTool())
        ctx = _make_ctx()
        result = reg.execute("range_index_tool", ctx, range="A1:D20")
        assert result["status"] == "ok"
        assert result["range"] == ["A1:D20"]
        assert result["first"] == "A1:D20"

        listed = reg.execute("range_index_tool", ctx, range=["B2:C3"])
        assert listed["range"] == ["B2:C3"]

    def test_successful_execution(self):
        reg = _make_registry(FakeTool())
        ctx = _make_ctx("writer")
        result = reg.execute("fake_tool", ctx, text="hello")
        assert result == {"status": "ok", "text": "hello"}

    def test_unknown_tool_returns_error(self):
        # Unknown names (including hallucinated ones and the 'unknown' sentinel
        # from null tool-name calls) must return a structured error so the model
        # gets actionable feedback rather than an opaque traceback.
        reg = _make_registry()
        ctx = _make_ctx()
        result = reg.execute("nope", ctx)
        assert result["status"] == "error"
        assert result.get("code") == "UNKNOWN_TOOL"
        assert "nope" in result.get("message", "")
        assert result.get("details", {}).get("tool_name") == "nope"


    def test_incompatible_doc_type_returns_unsupported_document(self):
        reg = _make_registry(FakeTool())
        ctx = _make_ctx("calc")
        result = reg.execute("fake_tool", ctx, text="x")
        assert result["status"] == "error"
        assert result.get("code") == "UNSUPPORTED_DOCUMENT"
        assert "fake_tool" in result.get("message", "")
        assert result.get("details", {}).get("tool_name") == "fake_tool"

    def test_string_range_stays_string_when_schema_is_not_array(self):
        class StringRangeTool(ToolBase):
            name = "string_range_tool"
            description = "range is a string, not an array"
            parameters = {
                "type": "object",
                "properties": {"range": {"type": "string"}},
                "required": ["range"],
            }
            uno_services = None

            def execute(self, ctx, **kwargs):
                return {"status": "ok", "range": kwargs["range"]}

        reg = _make_registry(StringRangeTool())
        ctx = _make_ctx()
        result = reg.execute("string_range_tool", ctx, range="A1:D20")
        assert result["status"] == "ok"
        assert result["range"] == "A1:D20"

    def test_unknown_schema_type_name_is_rejected(self):
        class WeirdType(ToolBase):
            name = "weird_type"
            description = "type str is not JSON Schema"
            parameters = {"type": "object", "properties": {"arg1": {"type": "str"}}, "required": ["arg1"]}
            uno_services = None

            def execute(self, ctx, **kwargs):
                return {"status": "ok"}

        reg = _make_registry(WeirdType())
        result = reg.execute("weird_type", _make_ctx(), arg1="x")
        assert result["status"] == "error"
        assert result["code"] == "VALIDATION_ERROR"

    def test_string_range_wraps_when_schema_type_lists_array(self):
        class ListedRangeTool(ToolBase):
            name = "listed_range_tool"
            description = "range type is a list that includes array"
            parameters = {
                "type": "object",
                "properties": {"range": {"type": ["string", "array"], "items": {"type": "string"}}},
                "required": ["range"],
            }
            uno_services = None

            def execute(self, ctx, **kwargs):
                return {"status": "ok", "range": kwargs["range"], "first": kwargs["range"][0]}

        reg = _make_registry(ListedRangeTool())
        ctx = _make_ctx()
        result = reg.execute("listed_range_tool", ctx, range="A1:D20")
        assert result["range"] == ["A1:D20"]
        assert result["first"] == "A1:D20"

    def test_validation_failure_returns_error(self):
        reg = _make_registry(FakeTool())
        ctx = _make_ctx("writer")
        result = reg.execute("fake_tool", ctx)  # missing 'text'
        assert result["status"] == "error"
        assert result.get("code") == "VALIDATION_ERROR"
        assert "Missing required" in result.get("error", result.get("message", ""))
        assert result.get("details", {}).get("tool_name") == "fake_tool"

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
        assert result.get("code") == "VALIDATION_ERROR"
        assert "Invalid type for text" in result.get("message", "")

    def test_optional_json_null_is_omitted(self):
        class OptionalTool(ToolBase):
            name = "optional_tool"
            description = "x"
            parameters = {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                "required": ["text"],
            }

            def execute(self, ctx, **kwargs):
                return {"status": "ok", "keys": sorted(kwargs)}

        reg = _make_registry(OptionalTool())
        ctx = _make_ctx("writer")
        result = reg.execute("optional_tool", ctx, text="hi", limit=None)
        assert result == {"status": "ok", "keys": ["text"]}

    def test_required_json_null_still_fails_validation(self):
        reg = _make_registry(FakeTool())
        ctx = _make_ctx("writer")
        result = reg.execute("fake_tool", ctx, text=None)
        assert result["status"] == "error"
        assert result.get("code") == "VALIDATION_ERROR"

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

class TestManageChartsSpecializedTier:
    """manage_charts is specialized on all apps; use delegate domain=charts."""

    _SKINNY_CHART_TOOLS = frozenset({"list_charts", "get_chart_info", "upsert_chart", "delete_chart"})

    @pytest.fixture
    def chart_registry(self):
        from plugin.calc.charts import ManageCharts

        return _make_registry(ManageCharts())

    def test_manage_charts_hidden_from_main_chat_calc(self, chart_registry):
        doc = TestingFactory.create_doc(doc_type="calc", content=[])
        names = {t.name for t in chart_registry.get_tools(doc=doc)}
        assert "manage_charts" not in names

    def test_manage_charts_hidden_from_main_chat_writer(self, chart_registry):
        doc = TestingFactory.create_doc(doc_type="writer", content=[])
        names = {t.name for t in chart_registry.get_tools(doc=doc)}
        assert "manage_charts" not in names

    def test_manage_charts_in_charts_domain_calc(self, chart_registry):
        doc = TestingFactory.create_doc(doc_type="calc", content=[])
        names = {t.name for t in chart_registry.get_tools(doc=doc, active_domain="charts")}
        assert "manage_charts" in names

    def test_manage_charts_openai_schema_excluded_by_default(self, chart_registry):
        doc = TestingFactory.create_doc(doc_type="calc", content=[])
        tool_names = {s["function"]["name"] for s in chart_registry.get_schemas("openai", doc=doc)}
        assert "manage_charts" not in tool_names

    def test_skinny_chart_tools_not_discovered(self):
        """list/info/upsert/delete are ToolBaseDummy-only; only manage_charts registers."""
        import plugin.calc.charts as calc_charts
        import plugin.draw.charts as draw_charts
        import plugin.writer.specialized.charts as writer_charts

        reg = _make_registry()
        reg.auto_discover(calc_charts)
        reg.auto_discover(writer_charts)
        reg.auto_discover(draw_charts)

        assert reg.get("manage_charts") is not None
        for name in self._SKINNY_CHART_TOOLS:
            assert reg.get(name) is None, f"{name} should not be registered"

        doc = TestingFactory.create_doc(doc_type="calc", content=[])
        domain_names = {t.name for t in reg.get_tools(doc=doc, active_domain="charts")}
        assert domain_names == {"manage_charts"}

    def test_writer_charts_domain_uses_writer_core_tools_not_sheet_readers(self):
        """Shared manage_charts name must not inherit Calc's required_core_tools via MRO."""
        from plugin.calc.shapes import UpsertShape as CalcUpsertShape
        from plugin.writer.specialized.charts import ManageCharts as WriterManageCharts
        from plugin.writer.specialized.shapes import UpsertShape as WriterUpsertShape

        assert "get_document_content" in (WriterUpsertShape.required_core_tools or ())
        assert "get_sheet_summary" in (CalcUpsertShape.required_core_tools or ())
        assert WriterManageCharts.required_core_tools is not None
        assert "read_cell_range" not in WriterManageCharts.required_core_tools

        def _exec(self, ctx, **kwargs):
            return {"status": "ok"}

        def core(name: str) -> ToolBase:
            cls = type(
                "Core_" + name,
                (ToolBase,),
                {"name": name, "description": name, "tier": "core", "requires_document": False, "execute": _exec},
            )
            return cls()

        reg = _make_registry(
            core("get_document_content"),
            core("get_document_tree"),
            core("get_sheet_summary"),
            core("read_cell_range"),
            WriterManageCharts(),
        )
        names = {t.name for t in reg.get_tools(active_domain="charts", filter_doc_type=False)}
        assert "manage_charts" in names
        assert "get_document_content" in names
        assert "get_document_tree" in names
        assert "read_cell_range" not in names
        assert "get_sheet_summary" not in names

    def test_shared_shape_upsert_unions_required_core_tools_on_replace(self):
        """Last-wins registration must keep Calc and Writer core tool sets."""
        from plugin.calc.shapes import UpsertShape as CalcUpsertShape
        from plugin.writer.specialized.shapes import UpsertShape as WriterUpsertShape

        reg = _make_registry()
        reg.register(CalcUpsertShape())
        reg.register(WriterUpsertShape())
        tool = reg.get("shape_upsert")
        assert tool is not None
        cores = reg._required_core_union.get("shape_upsert") or frozenset()
        assert "get_document_content" in cores
        assert "get_document_tree" in cores
        assert "get_sheet_summary" in cores
        assert "read_cell_range" in cores

    def test_manage_charts_still_dispatches_to_dummy_backends(self):
        from plugin.calc.charts import ManageCharts

        ctx = TestingFactory.create_context(doc_type="calc")
        tool = ManageCharts()
        with patch("plugin.calc.charts.ListCharts") as list_cls:
            list_cls.return_value.execute.return_value = {"status": "ok", "charts": [], "count": 0}
            res = tool.execute(ctx, action="list")
        assert res["status"] == "ok"
        assert res["count"] == 0
        list_cls.assert_called_once_with()
        list_cls.return_value.execute.assert_called_once()


class TestLibrarianToolVisibility:
    def test_librarian_tools_are_hidden_by_default_in_main_chat_schema(self):
        from plugin.chatbot.librarian import LibrarianOnboardingTool

        class VisibleTool(ToolBase):
            name = "visible_tool"
            description = "Visible tool"
            parameters = {"type": "object", "properties": {}}
            uno_services = None
            def execute(self, ctx, **kwargs): return {"status": "ok"}

        reg = _make_registry(
            VisibleTool(),
            LibrarianOnboardingTool(),
        )

        schemas = reg.get_schemas("openai", doc=TestingFactory.create_doc(doc_type="writer"))
        tool_names = {s["function"]["name"] for s in schemas}

        assert "visible_tool" in tool_names
        assert "librarian_onboarding" not in tool_names
        assert "reply_to_user" not in tool_names

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

    @patch("plugin.scripting.venv_diagnostics._probe_vision_packages")
    def test_get_schemas_openai_does_not_subprocess_probe_vision(self, mock_probe):
        """Regression: Send path must not import-probe the venv on get_schemas."""
        reg = _make_registry(FakeTool())
        ctx = MagicMock()
        with patch("plugin.vision.vision_availability._resolve_vision_python_exe", return_value="/venv/bin/python"):
            reg.get_schemas("openai", doc=TestingFactory.create_doc(doc_type="writer"), ctx=ctx)
        mock_probe.assert_not_called()

class TestExecuteKwargsAndFailure:
    def test_execute_strips_extra_kwargs(self):
        class ToolWithParams(ToolBase):
            name = "tool_with_params"
            description = "Tool with params"
            parameters = {"type": "object", "properties": {"arg1": {"type": "string"}}}
            uno_services = ["com.sun.star.text.TextDocument"]
            def execute(self, ctx, **kwargs): return {"status": "success", "got": dict(kwargs)}

        services = ServiceRegistry()
        reg = ToolRegistry(services)
        reg.register(ToolWithParams())

        ctx = ToolContext(doc=TestingFactory.create_doc(doc_type="writer"), ctx=None, doc_type="writer", services=services, caller="test")
        result = reg.execute("tool_with_params", ctx, arg1="val1", extra="ignored")

        assert result["status"] == "success"
        assert result["got"] == {"arg1": "val1"}

    def test_execute_coerces_write_formula_range_array_and_number(self):
        class WriteFormulaRange(ToolBase):
            name = "write_formula_range"
            description = "write"
            parameters = {
                "type": "object",
                "properties": {
                    "range": {"type": "array", "items": {"type": "string"}},
                    "values": {"type": "string"},
                },
                "required": ["range"],
            }
            uno_services = ["com.sun.star.text.TextDocument"]

            def execute(self, ctx, **kwargs):
                return {"status": "ok", "values": kwargs.get("values")}

        services = ServiceRegistry()
        reg = ToolRegistry(services)
        reg.register(WriteFormulaRange())
        ctx = ToolContext(doc=TestingFactory.create_doc(doc_type="writer"), ctx=None, doc_type="writer", services=services, caller="mcp")

        listed = reg.execute("write_formula_range", ctx, range=["A1:B1"], values=["a", "b"])
        assert listed["status"] == "ok"
        assert listed["values"] == '["a", "b"]'

        number = reg.execute("write_formula_range", ctx, range=["A1"], values=3)
        assert number["values"] == "3"

        empty = reg.execute("write_formula_range", ctx, range=["A1"], values=[])
        assert empty["values"] == ""

        # A plain string parameter must still reject a list. The coerce is
        # only the MCP widening for write_formula_range values.
        class StringArg(ToolBase):
            name = "string_arg"
            description = "string only"
            parameters = {"type": "object", "properties": {"arg1": {"type": "string"}}}
            uno_services = ["com.sun.star.text.TextDocument"]

            def execute(self, ctx, **kwargs):
                return {"status": "ok"}

        reg.register(StringArg())
        rejected = reg.execute("string_arg", ctx, arg1=["nope"])
        assert rejected["status"] == "error"
        assert rejected["code"] == "VALIDATION_ERROR"

    def test_execute_failure_returns_error_dict(self):
        class FailingToolWithParams(ToolBase):
            name = "failing_tool_with_params"
            description = "Tool with params that fails"
            parameters = {"type": "object", "properties": {"arg1": {"type": "string"}}}
            uno_services = ["com.sun.star.text.TextDocument"]
            def execute(self, ctx, **kwargs): raise RuntimeError("something went wrong")

        services = ServiceRegistry()
        reg = ToolRegistry(services)
        reg.register(FailingToolWithParams())

        ctx = ToolContext(doc=TestingFactory.create_doc(doc_type="writer"), ctx=None, doc_type="writer", services=services, caller="test")
        result = reg.execute("failing_tool_with_params", ctx, arg1="val1")

        assert result["status"] == "error"
        assert "something went wrong" in result["message"]

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
            doc = None
            doc_type = None
            caller = None
        result = registry.execute("test_fail", DummyContext())
        assert result["status"] == "error"
        assert "Test error" in result["details"]["original_error"]
        assert result["code"] == "TOOL_EXECUTION_ERROR"

    def test_execute_safe_document_disposed(self):
        from plugin.framework.errors import DocumentDisposedError

        class DeadTool(ToolBase):
            name = "test_dead"
            description = "x"
            parameters = {"type": "object", "properties": {}}

            def is_async(self):
                return True

            def execute(self, ctx, **kwargs):
                raise DocumentDisposedError("gone")

        class DummyContext:
            doc = None
            doc_type = None
            caller = None

        result = DeadTool().execute_safe(DummyContext())
        assert result["status"] == "error"
        assert result["code"] == "DOCUMENT_DISPOSED"

    def test_execute_safe_keeps_writeragent_exception_code(self):
        from plugin.framework.errors import ToolPermissionError

        class DeniedTool(ToolBase):
            name = "test_denied"
            description = "x"
            parameters = {"type": "object", "properties": {}}

            def is_async(self):
                return True

            def execute(self, ctx, **kwargs):
                raise ToolPermissionError("nope", details={"reason": "user"})

        class DummyContext:
            doc = None
            doc_type = None
            caller = None

        result = DeniedTool().execute_safe(DummyContext())
        assert result["status"] == "error"
        assert result["code"] == "PERMISSION_DENIED"
        assert result["message"] == "nope"
        assert result["details"]["reason"] == "user"

    def test_execute_safe_bare_runtime_exception_live_doc(self):
        """Image insert's empty RuntimeException must not become DOCUMENT_DISPOSED."""

        class BareRuntimeException(Exception):
            pass

        class LiveDoc:
            def getImplementationName(self):
                return "SwXTextDocument"

        class InsertTool(ToolBase):
            name = "image_generate"
            description = "x"
            parameters = {"type": "object", "properties": {}}

            def is_async(self):
                return True

            def execute(self, ctx, **kwargs):
                raise BareRuntimeException("")

        class LiveContext:
            doc = LiveDoc()
            doc_type = "writer"
            caller = None

        result = InsertTool().execute_safe(LiveContext())
        assert result["status"] == "error"
        assert result["code"] == "TOOL_EXECUTION_ERROR"
        assert "Document was closed" not in result["message"]
        assert "BareRuntimeException" in result["message"]

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
            doc = None
            doc_type = None
            caller = None
        result = registry.execute("test_slow", DummyContext())
        assert result["status"] == "error"
        assert result["code"] == "TOOL_TIMEOUT"
        assert result.get("details", {}).get("tool_name") == "test_slow"


class TestToolRegistryMainThreadMarshal:
    """Sync tools invoked via ToolRegistry.execute run on the logical main thread."""

    def test_sync_tool_marshaled_from_background(self, uno_thread_safety) -> None:
        threads_seen: list = []

        class DummySync(ToolBase):
            name = "dummy_sync"
            description = "x"
            parameters = {"type": "object", "properties": {}}
            uno_services = None
            doc_types = None

            def execute(self, ctx, **kwargs):
                threads_seen.append(threading.current_thread())
                return {"status": "ok"}

        reg = ToolRegistry(MagicMock())
        reg.register(DummySync())
        ctx = ToolContext(MagicMock(), MagicMock(), "writer", {}, "test")

        worker = threading.Thread(target=lambda: reg.execute("dummy_sync", ctx), name="registry-bg")
        worker.start()
        worker.join(timeout=3.0)

        assert len(threads_seen) == 1
        assert threads_seen[0] is uno_thread_safety.pump.main_thread

    def test_async_tool_stays_on_caller_thread(self, uno_thread_safety) -> None:
        threads_seen: list = []
        caller_threads: list = []

        class DummyAsync(ToolBase):
            name = "dummy_async"
            description = "x"
            parameters = {"type": "object", "properties": {}}
            uno_services = None
            doc_types = None

            def is_async(self) -> bool:
                return True

            def execute(self, ctx, **kwargs):
                threads_seen.append(threading.current_thread())
                return {"status": "ok"}

        reg = ToolRegistry(MagicMock())
        reg.register(DummyAsync())
        ctx = ToolContext(MagicMock(), MagicMock(), "writer", {}, "test")

        def bg():
            caller_threads.append(threading.current_thread())
            reg.execute("dummy_async", ctx)

        worker = threading.Thread(target=bg, name="registry-async-bg")
        worker.start()
        worker.join(timeout=3.0)

        assert len(threads_seen) == 1
        assert threads_seen[0] is caller_threads[0]
        assert threads_seen[0] is not uno_thread_safety.pump.main_thread


def test_tool_supports_document_uses_cached_services():
    from plugin.framework.tool import ToolBase, tool_supports_document

    class WriterOnly(ToolBase):
        name = "writer_only"
        description = "x"
        parameters = {"type": "object", "properties": {}}
        uno_services = ["com.sun.star.text.TextDocument"]

        def execute(self, ctx, **kwargs):
            return {"status": "ok"}

    tool = WriterOnly()
    services = frozenset({"com.sun.star.text.TextDocument"})
    assert tool_supports_document(tool, doc_type="writer", uno_services_supported=services) is True
    assert tool_supports_document(tool, doc_type="calc", uno_services_supported=frozenset({"com.sun.star.sheet.SpreadsheetDocument"})) is False


def test_tool_supports_document_unrestricted_and_doc_types():
    from plugin.framework.tool import ToolBase, tool_supports_document

    class Open(ToolBase):
        name = "open"
        description = "x"
        parameters = {"type": "object", "properties": {}}

        def execute(self, ctx, **kwargs):
            return {"status": "ok"}

    class WriterLabel(ToolBase):
        name = "writer_label"
        description = "x"
        parameters = {"type": "object", "properties": {}}
        doc_types = ["writer"]

        def execute(self, ctx, **kwargs):
            return {"status": "ok"}

    class WriterServicesAndLabel(ToolBase):
        name = "both"
        description = "x"
        parameters = {"type": "object", "properties": {}}
        uno_services = ["com.sun.star.text.TextDocument"]
        doc_types = ["writer"]

        def execute(self, ctx, **kwargs):
            return {"status": "ok"}

    open_tool = Open()
    assert tool_supports_document(open_tool, doc_type="calc", uno_services_supported=None) is True
    label_only = WriterLabel()
    assert tool_supports_document(label_only, doc_type="writer", uno_services_supported=None) is True
    assert tool_supports_document(label_only, doc_type="calc", uno_services_supported=None) is False
    both = WriterServicesAndLabel()
    calc_svcs = frozenset({"com.sun.star.sheet.SpreadsheetDocument"})
    assert tool_supports_document(both, doc_type="writer", uno_services_supported=calc_svcs) is True


def test_execute_async_delegate_skips_uno_doc_probe(monkeypatch):
    from plugin.framework.thread_guard import guard_uno
    from plugin.writer.specialized_base import DelegateToSpecializedWriter

    class _NoExecuteDelegate(DelegateToSpecializedWriter):
        def execute(self, ctx, **kwargs):
            return {"status": "ok", "message": "stub"}

    reg = ToolRegistry(MagicMock())
    reg.register(_NoExecuteDelegate())

    mock_doc = MagicMock()
    guarded_doc = guard_uno(mock_doc)
    ctx = ToolContext(
        guarded_doc,
        MagicMock(),
        "writer",
        {},
        "test",
        uno_services_supported=frozenset({"com.sun.star.text.TextDocument"}),
    )
    errors: list[Exception] = []

    def bg():
        try:
            reg.execute("delegate_to_specialized_writer_toolset", ctx, domain="document_research", task="find notes")
        except Exception as exc:
            errors.append(exc)

    worker = threading.Thread(target=bg, name="delegate-compat-bg")
    worker.start()
    worker.join(timeout=3.0)
    assert not errors
    mock_doc.supportsService.assert_not_called()


def test_get_tools_off_main_thread_without_doc_probe():
    from plugin.framework.tool import ToolBase

    doc = MagicMock()
    doc.supportsService.return_value = True

    class WriterOnly(ToolBase):
        name = "writer_only2"
        description = "x"
        parameters = {"type": "object", "properties": {}}
        uno_services = ["com.sun.star.text.TextDocument"]

        def execute(self, ctx, **kwargs):
            return {"status": "ok"}

    reg = ToolRegistry(MagicMock())
    reg.register(WriterOnly())
    tools = reg.get_tools(
        doc_type="writer",
        uno_services_supported=frozenset({"com.sun.star.text.TextDocument"}),
    )
    assert any(t.name == "writer_only2" for t in tools)
    doc.supportsService.assert_not_called()


def test_execute_with_timeout_parameterizes_result_queue() -> None:
    """Local result_queue must be Queue[tuple[str, Any]] for reportMissingTypeArgument."""
    import inspect

    from plugin.framework.tool import ToolRegistry

    src = inspect.getsource(ToolRegistry._execute_with_timeout)
    assert "result_queue: queue.Queue[tuple[str, Any]]" in src


def test_execute_bypass_thread_guard_allows_background_thread() -> None:
    """Regression: DSPy eval (tools_lo) may call tools from the LO worker thread."""
    calls: list[str] = []

    class DummyTool:
        name = "dummy_sync"
        description = "x"
        parameters = {"type": "object", "properties": {}}
        uno_services = None
        doc_types = None

        def get_parameters(self, doc_type=None):
            return self.parameters

        def get_description(self, doc_type=None):
            return self.description

        def validate(self, *, doc_type=None, **kwargs):
            return True, None

        def execute(self, ctx, **kwargs):
            calls.append("execute")
            return {"status": "ok"}

        def execute_safe(self, ctx, **kwargs):
            calls.append("execute_safe")
            return {"status": "ok"}

    reg = ToolRegistry(MagicMock())
    reg.register(DummyTool())  # type: ignore[arg-type]
    ctx = ToolContext(MagicMock(), MagicMock(), "writer", {}, "test")

    out: dict | None = None

    def bg():
        nonlocal out
        out = reg.execute("dummy_sync", ctx, bypass_thread_guard=True)

    t = threading.Thread(target=bg)
    t.start()
    t.join()

    assert out == {"status": "ok"}
    assert calls == ["execute"]

