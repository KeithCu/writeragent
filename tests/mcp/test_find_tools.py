# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for the find_tools discovery meta-tool + its tools/list gating."""
from unittest.mock import MagicMock, patch

from plugin.doc.find_tools_tool import FindTools, get_domain_guidance
from plugin.framework.prompts import get_specialized_domain_catalog
from plugin.framework.tool import ToolBase, ToolRegistry
from plugin.mcp.mcp_protocol import MCPProtocolHandler
from plugin.writer.specialized_base import ToolWriterSpecialBase


def _domain_ids(catalog):
    return {e["domain"] for e in catalog}


# --------------------------------------------------------------------------- #
# tool metadata + domain catalog
# --------------------------------------------------------------------------- #

def test_find_tools_properties():
    tool = FindTools()
    assert tool.name == "find_tools"
    assert tool.tier == "mcp"
    assert tool.is_mutation is False
    assert tool.requires_document is False


def test_get_domain_guidance():
    assert "data range" in get_domain_guidance("charts", agent_label="Calc").lower()
    assert "headers" in get_domain_guidance("charts", agent_label="Writer").lower()
    assert "insert_after_text" in get_domain_guidance("footnotes")
    assert get_domain_guidance("totally_unknown_domain") == ""


def test_specialized_domain_catalog_writer_has_footnotes():
    catalog = get_specialized_domain_catalog(agent_label="Writer", ctx=MagicMock())
    assert "footnotes" in _domain_ids(catalog)
    footnotes = next(e for e in catalog if e["domain"] == "footnotes")
    assert footnotes["description"]


def test_specialized_domain_catalog_merged_when_no_app():
    writer = get_specialized_domain_catalog(agent_label="Writer", ctx=MagicMock())
    merged = get_specialized_domain_catalog(agent_label=None, ctx=MagicMock())
    assert len(merged) >= len(writer)
    assert "footnotes" in _domain_ids(merged)


def test_specialized_domain_catalog_excludes_sidebar_only():
    catalog = get_specialized_domain_catalog(agent_label="Writer", ctx=MagicMock())
    assert "brainstorming" not in _domain_ids(catalog)


def test_specialized_domain_catalog_excludes_vision_without_venv():
    with patch("plugin.vision.vision_availability.vision_venv_configured", return_value=False):
        catalog = get_specialized_domain_catalog(agent_label="Writer", ctx=MagicMock())
    assert "vision" not in _domain_ids(catalog)


# --------------------------------------------------------------------------- #
# execute() with a mocked registry
# --------------------------------------------------------------------------- #

def _ctx(registry, doc_type="writer", doc=None):
    ctx = MagicMock()
    ctx.services.get.side_effect = lambda name: registry if name == "tools" else None
    ctx.doc = MagicMock() if doc is None and doc_type else doc
    ctx.doc_type = doc_type if ctx.doc else None
    ctx.ctx = MagicMock()
    return ctx


def _schema(name, desc="a tool"):
    return {"name": name, "description": desc, "inputSchema": {"type": "object", "properties": {}}}


def test_execute_domain_returns_domain_schemas_without_finish_tool():
    registry = MagicMock()
    registry.get_schemas.return_value = [
        _schema("footnotes_insert"), _schema("footnotes_list"),
        _schema("specialized_workflow_finished", "finish"),
    ]
    registry.get_tools.return_value = [
        MagicMock(specialized_domain="footnotes"), MagicMock(specialized_domain=None),
    ]
    ctx = _ctx(registry)

    result = FindTools().execute(ctx, domain="footnotes")

    names = {t["name"] for t in result["tools"]}
    assert {"footnotes_insert", "footnotes_list"} <= names
    assert "specialized_workflow_finished" not in names
    for t in result["tools"]:
        assert "inputSchema" in t
    registry.get_schemas.assert_called_once_with("mcp", doc=ctx.doc, active_domain="footnotes")
    assert "footnotes" in _domain_ids(result["available_domains"])
    assert "insert_after_text" in result["domain_guidance"]["footnotes"]


def test_execute_no_args_returns_catalog_only():
    registry = MagicMock()
    ctx = _ctx(registry)

    result = FindTools().execute(ctx)

    assert result["tools"] == []
    assert "footnotes" in _domain_ids(result["available_domains"])
    registry.get_schemas.assert_not_called()


def test_execute_no_registry_errors():
    ctx = MagicMock()
    ctx.services.get.side_effect = lambda name: None
    result = FindTools().execute(ctx)
    assert result.get("status") == "error"


# --------------------------------------------------------------------------- #
# tools/list gating: find_tools only in direct_discovery
# --------------------------------------------------------------------------- #

def _handler(mode, schemas):
    services = MagicMock()
    services.tools.get_schemas.return_value = list(schemas)
    services.config.get.side_effect = (
        lambda key, default=None: mode if key == "mcp.tool_exposure_mode" else default
    )
    services.get.side_effect = lambda name: getattr(services, name, None)

    def _inline(fn, *a, **k):
        k.pop("timeout", None)
        return fn(*a, **k)

    services.main_thread.execute.side_effect = _inline
    services.document.get_active_document.return_value = MagicMock()
    return MCPProtocolHandler(services)


def _list_names(mode):
    schemas = [
        {"name": "find_tools", "description": "discovery", "inputSchema": {}},
        {"name": "insert_footnote", "description": "footnote", "inputSchema": {}},
        {"name": "apply_document_content", "description": "core", "inputSchema": {}},
    ]
    handler = _handler(mode, schemas)
    return {t["name"] for t in handler._mcp_tools_list({})["tools"]}


def test_find_tools_listed_only_in_direct_discovery():
    assert "find_tools" not in _list_names("delegate")
    assert "find_tools" not in _list_names("direct_flat")
    assert "find_tools" in _list_names("direct_discovery")


def test_gating_keeps_other_tools():
    assert "apply_document_content" in _list_names("delegate")
    assert "insert_footnote" in _list_names("direct_discovery")


def test_tools_list_filters_on_main_thread():
    # Regression: the doc-type filtering (get_schemas -> supports_doc -> doc.supportsService)
    # touches UNO, so it must run INSIDE the main-thread executor, not on the MCP request
    # thread -- otherwise the UNO thread guard fails tools/list with a 500. Affects every
    # mode (all pass the live doc to get_schemas), so delegate is enough to exercise it.
    on_main = {"in": False}

    def _exec(fn, *a, **k):
        k.pop("timeout", None)
        on_main["in"] = True
        try:
            return fn(*a, **k)
        finally:
            on_main["in"] = False

    def _get_schemas(*a, **k):
        assert on_main["in"], "get_schemas (UNO doc-type filtering) ran off the main thread"
        return [{"name": "insert_footnote", "description": "f", "inputSchema": {}}]

    services = MagicMock()
    services.config.get.side_effect = (
        lambda key, default=None: "delegate" if key == "mcp.tool_exposure_mode" else default
    )
    services.get.side_effect = lambda name: getattr(services, name, None)
    services.main_thread.execute.side_effect = _exec
    services.tools.get_schemas.side_effect = _get_schemas
    services.document.get_active_document.return_value = MagicMock()  # a document is open

    result = MCPProtocolHandler(services)._mcp_tools_list({})
    assert any(t["name"] == "insert_footnote" for t in result["tools"])
    services.tools.get_schemas.assert_called()  # the thread assertion only fires if get_schemas ran


def test_tools_list_direct_flat_filters_sidebar_on_main_thread():
    # direct_flat runs a second sidebar-only filter; with cached uno_services_supported
    # it no longer probes doc.supportsService, but still must run on the main thread
    # because it shares the _mcp_tools_list UNO resolution block.
    from unittest.mock import patch

    on_main = {"in": False}

    def _exec(fn, *a, **k):
        k.pop("timeout", None)
        on_main["in"] = True
        try:
            return fn(*a, **k)
        finally:
            on_main["in"] = False

    def _get_schemas(*a, **k):
        assert on_main["in"], "get_schemas (UNO doc-type filtering) ran off the main thread"
        return [{"name": "create_chart", "description": "c", "inputSchema": {}}]

    def _sidebar(registry, doc, *, doc_type=None, uno_services_supported=None):
        assert on_main["in"], "sidebar_only_tool_names (UNO get_tools filtering) ran off the main thread"
        return frozenset()

    services = MagicMock()
    services.config.get.side_effect = (
        lambda key, default=None: "direct_flat" if key == "mcp.tool_exposure_mode" else default
    )
    services.get.side_effect = lambda name: getattr(services, name, None)
    services.main_thread.execute.side_effect = _exec
    services.tools.get_schemas.side_effect = _get_schemas
    services.document.get_active_document.return_value = MagicMock()  # a document is open

    with patch("plugin.doc.find_tools_tool.sidebar_only_tool_names", _sidebar):
        result = MCPProtocolHandler(services)._mcp_tools_list({})
    assert any(t["name"] == "create_chart" for t in result["tools"])
    services.tools.get_schemas.assert_called()


def test_initialize_instructions_mentions_find_tools_in_direct_discovery():
    handler = _handler("direct_discovery", [])
    result = handler._mcp_initialize({})
    assert "find_tools" in result["instructions"].lower()


def test_initialize_instructions_mentions_delegate_in_default_mode():
    handler = _handler("delegate", [])
    result = handler._mcp_initialize({})
    assert "delegate_to_specialized" in result["instructions"]


# --------------------------------------------------------------------------- #
# real-registry tests
# --------------------------------------------------------------------------- #

class _FtBase(ToolWriterSpecialBase):
    specialized_domain = "footnotes"
    uno_services = None
    is_mutation = False
    description = "footnotes domain tool"
    parameters = {"type": "object", "properties": {}, "required": []}

    def execute(self, ctx, **kwargs):
        return {"status": "ok"}


class _FtInsert(_FtBase):
    name = "footnotes_insert"
    description = "insert a footnote at an anchor"


class _FtList(_FtBase):
    name = "footnotes_list"
    description = "list the footnotes in the document"


class _FinishTool(ToolBase):
    name = "specialized_workflow_finished"
    description = "finish the specialized workflow"
    tier = "specialized_control"
    is_mutation = False
    parameters = {"type": "object", "properties": {}, "required": []}

    def execute(self, ctx, **kwargs):
        return {}


class _GatewayTool(ToolBase):
    name = "delegate_to_specialized_writer_toolset"
    description = "delegate to a specialized writer toolset"
    tier = "core"
    is_mutation = False
    parameters = {"type": "object", "properties": {}, "required": []}

    def execute(self, ctx, **kwargs):
        return {}


class _CoreTool(ToolBase):
    name = "apply_document_content"
    description = "core document edit"
    tier = "core"
    is_mutation = False
    parameters = {"type": "object", "properties": {}, "required": []}

    def execute(self, ctx, **kwargs):
        return {}


class _BrainstormTool(_FtBase):
    name = "brainstorm_research_web"
    specialized_domain = "brainstorming"
    description = "brainstorm research on the web"


class _AppSpecificTool(ToolBase):
    name = "create_chart"
    description = "create a chart from a data range"
    tier = "specialized"
    is_mutation = False
    uno_services = ["com.sun.star.text.TextDocument"]
    parameters = {"type": "object", "properties": {}, "required": []}

    def execute(self, ctx, **kwargs):
        return {}


class _AppSpecificCoreTool(ToolBase):
    name = "calc_only_core"
    description = "a calc-only core tool"
    tier = "core"
    is_mutation = False
    uno_services = ["com.sun.star.sheet.SpreadsheetDocument"]
    parameters = {"type": "object", "properties": {}, "required": []}

    def execute(self, ctx, **kwargs):
        return {}


def _real_registry():
    reg = ToolRegistry(MagicMock())
    for cls in (FindTools, _FtInsert, _FtList, _FinishTool, _GatewayTool, _CoreTool, _BrainstormTool):
        reg.register(cls())
    return reg


def _ctx_real(registry):
    ctx = MagicMock()
    ctx.services.get.side_effect = lambda name: registry if name == "tools" else None
    ctx.doc = None
    ctx.doc_type = None
    ctx.ctx = MagicMock()
    return ctx


def _handler_real(mode, registry):
    services = MagicMock()
    services.tools = registry
    services.config.get.side_effect = (
        lambda key, default=None: mode if key == "mcp.tool_exposure_mode" else default
    )
    services.get.side_effect = lambda name: getattr(services, name, None)

    def _inline(fn, *a, **k):
        k.pop("timeout", None)
        return fn(*a, **k)

    services.main_thread.execute.side_effect = _inline
    services.document.get_active_document.return_value = None
    return MCPProtocolHandler(services)


def test_execute_domain_real_registry():
    result = FindTools().execute(_ctx_real(_real_registry()), domain="footnotes")
    names = {t["name"] for t in result["tools"]}
    assert {"footnotes_insert", "footnotes_list"} <= names
    assert "specialized_workflow_finished" not in names
    assert "delegate_to_specialized_writer_toolset" not in names
    assert "find_tools" not in names


def test_mode_sizing_real_registry():
    reg = _real_registry()

    def names(mode):
        return {t["name"] for t in _handler_real(mode, reg)._mcp_tools_list({})["tools"]}

    spec = {"footnotes_insert", "footnotes_list"}
    delegate = names("delegate")
    assert not (spec & delegate) and "find_tools" not in delegate
    assert "apply_document_content" in delegate
    flat = names("direct_flat")
    assert spec <= flat and "find_tools" not in flat
    discovery = names("direct_discovery")
    assert "find_tools" in discovery and not (spec & discovery)


def test_execute_tolerates_malformed_schemas_in_domain_listing():
    registry = MagicMock()
    registry.get_schemas.return_value = [
        {"name": 123, "description": ["x"], "inputSchema": {}},
        "not_a_dict",
        _schema("footnotes_insert", "add a note"),
    ]
    registry.get_tools.return_value = []
    result = FindTools().execute(_ctx(registry), domain="footnotes")
    assert result["status"] == "ok"
    assert "footnotes_insert" in {t["name"] for t in result["tools"]}


def test_execute_tolerates_non_string_domain():
    registry = MagicMock()
    registry.get_schemas.return_value = [_schema("a")]
    registry.get_tools.return_value = []
    result = FindTools().execute(_ctx(registry), domain=123)
    assert result["status"] == "ok"
    assert result["tools"] == []


def test_execute_normalizes_domain_case_and_whitespace():
    registry = MagicMock()
    registry.get_schemas.return_value = [_schema("footnotes_insert")]
    registry.get_tools.return_value = []
    ctx = _ctx(registry)
    result = FindTools().execute(ctx, domain="  Footnotes ")
    assert result["domain"] == "footnotes"
    registry.get_schemas.assert_called_once_with("mcp", doc=ctx.doc, active_domain="footnotes")


def test_find_tools_call_blocked_outside_direct_discovery():
    handler = _handler_real("delegate", _real_registry())
    res = handler._mcp_tools_call({"name": "find_tools", "arguments": {}})
    assert res["isError"] is True
    assert "direct_discovery" in res["content"][0]["text"]


def test_execute_tool_on_main_runs_document_optional_without_doc():
    handler = _handler_real("direct_discovery", _real_registry())
    res = handler._execute_tool_on_main("find_tools", {})
    assert res.get("status") == "ok"
    assert res.get("tools") == []


def test_execute_tool_on_main_still_requires_doc_for_normal_tools():
    handler = _handler_real("direct_discovery", _real_registry())
    res = handler._execute_tool_on_main("apply_document_content", {})
    assert res.get("code") == "NO_DOCUMENT_OPEN"


def test_domain_listing_not_truncated_by_default():
    registry = MagicMock()
    registry.get_schemas.return_value = [_schema(f"footnotes_{i}") for i in range(12)]
    registry.get_tools.return_value = [MagicMock(specialized_domain="footnotes")]
    result = FindTools().execute(_ctx(registry), domain="footnotes")
    assert len(result["tools"]) == 12


def test_find_tools_excludes_sidebar_only_domains_from_catalog():
    result = FindTools().execute(_ctx_real(_real_registry()))
    assert "brainstorming" not in _domain_ids(result["available_domains"])


def test_direct_flat_excludes_sidebar_only_domains():
    names = {t["name"] for t in _handler_real("direct_flat", _real_registry())._mcp_tools_list({})["tools"]}
    assert "footnotes_insert" in names
    assert "brainstorm_research_web" not in names


def test_no_document_domain_listing_uses_broad_catalog():
    registry = MagicMock()
    registry.get_schemas.return_value = [_schema("create_chart")]
    registry.get_tools.return_value = []
    ctx = _ctx_real(registry)
    result = FindTools().execute(ctx, domain="charts")
    registry.get_schemas.assert_called_once_with(
        "mcp", doc=None, active_domain="charts", filter_doc_type=False)
    assert "create_chart" in {t["name"] for t in result["tools"]}
    assert "note" in result


def test_explicit_sidebar_domain_returns_no_tools():
    result = FindTools().execute(_ctx_real(_real_registry()), domain="brainstorming")
    assert result["tools"] == []
    assert result["domain"] == "brainstorming"


def test_get_domain_guidance_is_app_neutral_when_app_unknown():
    g = get_domain_guidance("charts", agent_label=None)
    assert "data_range" in g and "headers" in g


def test_no_document_chart_guidance_is_app_neutral():
    registry = MagicMock()
    registry.get_schemas.return_value = [_schema("create_chart")]
    registry.get_tools.return_value = []
    ctx = MagicMock()
    ctx.services.get.side_effect = lambda name: registry if name == "tools" else None
    ctx.doc = None
    ctx.doc_type = None
    ctx.ctx = MagicMock()
    result = FindTools().execute(ctx, domain="charts")
    g = result["domain_guidance"]["charts"]
    assert "data_range" in g and "headers" in g


def test_get_domain_guidance_python_is_app_neutral_when_app_unknown():
    g = get_domain_guidance("python", agent_label=None)
    assert "data_range" in g
    assert "document tools" in g.lower()


def test_run_venv_python_advertises_superset_when_doc_unknown():
    from plugin.calc.python.venv import RunVenvPythonScript
    params = RunVenvPythonScript().get_parameters(None)
    assert "data_range" in params["properties"]


def test_run_venv_python_description_neutral_when_doc_unknown():
    from plugin.calc.python.venv import RunVenvPythonScript
    desc = RunVenvPythonScript().get_description(None)
    assert "data_range" in desc
    assert "document tools" in desc.lower()


def test_execute_drops_schemas_with_unusable_names():
    registry = MagicMock()
    registry.get_schemas.return_value = [
        {"name": ["bad"], "description": "x", "inputSchema": {}},
        {"name": "", "description": "y", "inputSchema": {}},
        _schema("footnotes_insert", "insert a footnote"),
    ]
    registry.get_tools.return_value = []
    names = [t.get("name") for t in FindTools().execute(_ctx(registry), domain="footnotes")["tools"]]
    assert ["bad"] not in names and "" not in names
    assert "footnotes_insert" in names


def test_direct_flat_lists_app_specific_tools_without_document():
    reg = ToolRegistry(MagicMock())
    for cls in (FindTools, _FtInsert, _AppSpecificTool):
        reg.register(cls())
    names = {t["name"] for t in _handler_real("direct_flat", reg)._mcp_tools_list({})["tools"]}
    assert "create_chart" in names
    assert "footnotes_insert" in names


def test_delegate_no_doc_keeps_doc_type_filtering():
    reg = ToolRegistry(MagicMock())
    for cls in (_CoreTool, _AppSpecificCoreTool):
        reg.register(cls())
    names = {t["name"] for t in _handler_real("delegate", reg)._mcp_tools_list({})["tools"]}
    assert "apply_document_content" in names
    assert "calc_only_core" not in names


def test_direct_flat_unresolved_document_url_does_not_broaden():
    reg = ToolRegistry(MagicMock())
    for cls in (FindTools, _FtInsert, _AppSpecificTool):
        reg.register(cls())
    handler = _handler_real("direct_flat", reg)
    handler.services.document.resolve_document_by_url.return_value = (None, None)
    names = {t["name"] for t in handler._mcp_tools_list({}, document_url="file:///missing.odt")["tools"]}
    assert "create_chart" not in names


def test_real_active_document_treats_start_center_as_none():
    from plugin.mcp.mcp_protocol import _real_active_document

    doc_svc = MagicMock()
    phantom = MagicMock()
    phantom.supportsService.return_value = False
    doc_svc.get_active_document.return_value = phantom
    assert _real_active_document(doc_svc) is None

    real = MagicMock()
    real.supportsService.return_value = True
    doc_svc.get_active_document.return_value = real
    assert _real_active_document(doc_svc) is real

    doc_svc.get_active_document.return_value = None
    assert _real_active_document(doc_svc) is None


def test_direct_flat_treats_start_center_as_no_document():
    reg = ToolRegistry(MagicMock())
    for cls in (FindTools, _FtInsert, _AppSpecificTool):
        reg.register(cls())
    handler = _handler_real("direct_flat", reg)
    phantom = MagicMock()
    phantom.supportsService.return_value = False
    handler.services.document.get_active_document.return_value = phantom
    names = {t["name"] for t in handler._mcp_tools_list({})["tools"]}
    assert "create_chart" in names
