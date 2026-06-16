# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for the find_tools discovery meta-tool + its tools/list gating.

Registry is mocked (the tool's own logic + the protocol gating are what we assert
here); the real registry / domain-narrowing path is covered by the live MCP run.
"""
from unittest.mock import MagicMock

from plugin.doc.find_tools_tool import FindTools, _rank, get_domain_guidance
from plugin.framework.tool import ToolBase, ToolRegistry
from plugin.mcp.mcp_protocol import MCPProtocolHandler
from plugin.writer.specialized_base import ToolWriterSpecialBase


# --------------------------------------------------------------------------- #
# tool metadata + pure ranker
# --------------------------------------------------------------------------- #

def test_find_tools_properties():
    tool = FindTools()
    assert tool.name == "find_tools"
    assert tool.tier == "mcp"           # hidden from the chat agent's tool list
    assert tool.is_mutation is False
    assert tool.requires_document is False   # discovery works with no document open


def test_rank_name_substring_outranks_description():
    schemas = [
        {"name": "unrelated_tool", "description": "mentions footnote here"},
        {"name": "footnotes_insert", "description": "add a note"},
    ]
    assert _rank(schemas, "footnote", 5)[0]["name"] == "footnotes_insert"


def test_rank_empty_query_returns_input_order_truncated():
    schemas = [{"name": "a", "description": ""}, {"name": "b", "description": ""}]
    assert _rank(schemas, "", 1) == schemas[:1]


def test_rank_no_overlap_is_dropped():
    schemas = [{"name": "footnotes_insert", "description": "add a note"}]
    assert _rank(schemas, "zzz_no_such_capability", 5) == []


def test_rank_multiword_query():
    schemas = [
        {"name": "create_chart", "description": "make a chart from a data range"},
        {"name": "footnotes_insert", "description": "insert a footnote at an anchor"},
        {"name": "noise", "description": "totally unrelated text"},
    ]
    ranked = _rank(schemas, "insert a footnote", 2)
    assert ranked[0]["name"] == "footnotes_insert"


def test_get_domain_guidance():
    assert "data range" in get_domain_guidance("charts", agent_label="Calc").lower()
    assert "headers" in get_domain_guidance("charts", agent_label="Writer").lower()
    assert "insert_after_text" in get_domain_guidance("footnotes")
    assert get_domain_guidance("totally_unknown_domain") == ""


# --------------------------------------------------------------------------- #
# execute() with a mocked registry
# --------------------------------------------------------------------------- #

def _ctx(registry, doc_type="writer"):
    ctx = MagicMock()
    ctx.services.get.side_effect = lambda name: registry if name == "tools" else None
    ctx.doc = MagicMock()
    ctx.doc_type = doc_type
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
    assert "specialized_workflow_finished" not in names         # finish tool stripped
    for t in result["tools"]:
        assert "inputSchema" in t
    registry.get_schemas.assert_called_once_with("mcp", doc=ctx.doc, active_domain="footnotes")
    assert "footnotes" in result["available_domains"]
    assert "insert_after_text" in result["domain_guidance"]["footnotes"]


def test_execute_query_ranks_top_n():
    registry = MagicMock()
    registry.get_schemas.return_value = [
        _schema("footnotes_insert", "insert a footnote at an anchor"),
        _schema("create_chart", "make a chart from a range"),
        _schema("unrelated", "something else entirely"),
    ]
    registry.get_tools.return_value = []
    ctx = _ctx(registry)

    result = FindTools().execute(ctx, query="insert a footnote", limit=2)

    names = [t["name"] for t in result["tools"]]
    assert len(names) <= 2
    assert names[0] == "footnotes_insert"
    # global branch hides mcp + control + core tiers (core is already in tools/list)
    registry.get_schemas.assert_called_once_with(
        "mcp", doc=ctx.doc, exclude_tiers=frozenset({"mcp", "specialized_control", "core"}))


def test_execute_unknown_query_returns_empty():
    registry = MagicMock()
    registry.get_schemas.return_value = [_schema("footnotes_insert", "add a note")]
    registry.get_tools.return_value = []
    result = FindTools().execute(_ctx(registry), query="zzz_no_such_capability")
    assert result["tools"] == []


def test_execute_no_args_lists_domains():
    registry = MagicMock()
    registry.get_schemas.return_value = [_schema("a"), _schema("b")]
    registry.get_tools.return_value = [
        MagicMock(specialized_domain="footnotes"), MagicMock(specialized_domain="charts"),
    ]
    result = FindTools().execute(_ctx(registry))
    assert sorted(result["available_domains"]) == ["charts", "footnotes"]
    assert result["tools"]                                       # global listing, no query


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
    # the name filter only drops find_tools, never the real tools
    assert "apply_document_content" in _list_names("delegate")
    assert "insert_footnote" in _list_names("direct_discovery")


# --------------------------------------------------------------------------- #
# real-registry tests: validate the FindTools <-> ToolRegistry contract and the
# per-mode tools/list sizing (the actual product promise) without mocking the
# registry internals. Universal (uno_services=None) fakes so no live doc is needed.
# --------------------------------------------------------------------------- #

class _FtBase(ToolWriterSpecialBase):
    # _is_specialized_domain_tool requires an instance of the real specialized base
    # (tool.py:435), not just a specialized_domain attribute. uno_services=None keeps
    # the fakes universal so the test needs no live Writer document.
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
    # Writer sidebar-only domain (WRITER_SIDEBAR_ONLY_DOMAINS): must NOT leak into the
    # direct MCP modes, since its flow needs a bespoke finish tool the direct modes lack.
    name = "brainstorm_research_web"
    specialized_domain = "brainstorming"
    description = "brainstorm research on the web"


class _AppSpecificTool(ToolBase):
    # An app-specific specialized tool (uno_services set) -> the registry filters it out
    # when there's no open document, unless discovery lists the full catalog.
    name = "create_chart"
    description = "create a chart from a data range"
    tier = "specialized"
    is_mutation = False
    uno_services = ["com.sun.star.text.TextDocument"]
    parameters = {"type": "object", "properties": {}, "required": []}

    def execute(self, ctx, **kwargs):
        return {}


class _AppSpecificCoreTool(ToolBase):
    # An app-specific *core* tool: must stay filtered out with no document in delegate /
    # direct_discovery (the no-doc broad catalog is direct_flat-only).
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
    ctx.doc = None          # universal fakes pass with no active document
    ctx.doc_type = "writer"
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
    assert "specialized_workflow_finished" not in names      # finish stripped
    assert "delegate_to_specialized_writer_toolset" not in names
    assert "find_tools" not in names                          # self not surfaced


def test_mode_sizing_real_registry():
    reg = _real_registry()

    def names(mode):
        return {t["name"] for t in _handler_real(mode, reg)._mcp_tools_list({})["tools"]}

    spec = {"footnotes_insert", "footnotes_list"}
    delegate = names("delegate")
    assert not (spec & delegate) and "find_tools" not in delegate
    assert "apply_document_content" in delegate
    flat = names("direct_flat")
    assert spec <= flat and "find_tools" not in flat          # specialized exposed, no find_tools
    discovery = names("direct_discovery")
    assert "find_tools" in discovery and not (spec & discovery)  # small list + find_tools


# --------------------------------------------------------------------------- #
# robustness regressions (the review's SHOULD-FIX hardenings)
# --------------------------------------------------------------------------- #

def test_execute_tolerates_malformed_schemas():
    registry = MagicMock()
    registry.get_schemas.return_value = [
        {"name": 123, "description": ["x"], "inputSchema": {}},   # non-string name/desc
        "not_a_dict",                                             # non-dict candidate
        _schema("footnotes_insert", "add a note"),
    ]
    registry.get_tools.return_value = []
    result = FindTools().execute(_ctx(registry), query="footnote")
    assert result["status"] == "ok"


def test_execute_tolerates_infinite_limit():
    registry = MagicMock()
    registry.get_schemas.return_value = [_schema("a")]
    registry.get_tools.return_value = []
    assert FindTools().execute(_ctx(registry), limit=float("inf"))["status"] == "ok"


def test_execute_tolerates_non_string_inputs():
    registry = MagicMock()
    registry.get_schemas.return_value = [_schema("a")]
    registry.get_tools.return_value = []
    assert FindTools().execute(_ctx(registry), query=["not", "str"], domain=123)["status"] == "ok"


def test_execute_excludes_gateway_from_global():
    registry = MagicMock()
    registry.get_schemas.return_value = [
        _schema("delegate_to_specialized_writer_toolset", "delegate"),
        _schema("footnotes_insert", "insert a footnote"),
    ]
    registry.get_tools.return_value = []
    names = {t["name"] for t in FindTools().execute(_ctx(registry), query="footnote")["tools"]}
    assert "delegate_to_specialized_writer_toolset" not in names


# --------------------------------------------------------------------------- #
# review follow-ups: domain normalization, query-path guidance, mode gating on
# tools/call, and document-optional execution
# --------------------------------------------------------------------------- #

def test_execute_normalizes_domain_case_and_whitespace():
    registry = MagicMock()
    registry.get_schemas.return_value = [_schema("footnotes_insert")]
    registry.get_tools.return_value = []
    ctx = _ctx(registry)
    result = FindTools().execute(ctx, domain="  Footnotes ")
    assert result["domain"] == "footnotes"
    registry.get_schemas.assert_called_once_with("mcp", doc=ctx.doc, active_domain="footnotes")


def test_execute_query_path_infers_domain_guidance():
    # A free-text query (no explicit domain) still surfaces the matched domain's hints,
    # as a {domain: text} dict, so callers don't lose the delegate's load-bearing guidance.
    result = FindTools().execute(_ctx_real(_real_registry()), query="insert a footnote")
    assert "footnotes_insert" in {t["name"] for t in result["tools"]}
    assert "insert_after_text" in result["domain_guidance"]["footnotes"]


def test_find_tools_call_blocked_outside_direct_discovery():
    handler = _handler_real("delegate", _real_registry())
    res = handler._mcp_tools_call({"name": "find_tools", "arguments": {}})
    assert res["isError"] is True
    assert "direct_discovery" in res["content"][0]["text"]


def test_execute_tool_on_main_runs_document_optional_without_doc():
    # find_tools (requires_document=False) must NOT short-circuit on NO_DOCUMENT_OPEN
    # even though get_active_document() returns None.
    handler = _handler_real("direct_discovery", _real_registry())
    res = handler._execute_tool_on_main("find_tools", {})
    assert res.get("status") == "ok"


def test_execute_tool_on_main_still_requires_doc_for_normal_tools():
    handler = _handler_real("direct_discovery", _real_registry())
    res = handler._execute_tool_on_main("apply_document_content", {})
    assert res.get("code") == "NO_DOCUMENT_OPEN"


def test_global_search_excludes_core_tools():
    # Global (no-domain) discovery surfaces hidden specialized tools, not the core
    # tools already advertised in tools/list (apply_document_content is core here).
    result = FindTools().execute(_ctx_real(_real_registry()), query="document")
    names = {t["name"] for t in result["tools"]}
    assert "apply_document_content" not in names


def test_domain_listing_not_truncated_at_default():
    # "Pass a domain to list every tool in one area" -> a domain with >8 tools must not
    # be silently capped at the small free-text default.
    registry = MagicMock()
    registry.get_schemas.return_value = [_schema(f"footnotes_{i}") for i in range(12)]
    registry.get_tools.return_value = [MagicMock(specialized_domain="footnotes")]
    result = FindTools().execute(_ctx(registry), domain="footnotes")
    assert len(result["tools"]) == 12


def test_rank_is_accent_insensitive():
    schemas = [{"name": "footnotes_insert", "description": "insere uma nota de rodape"}]
    # an accented query still matches the unaccented description token
    assert _rank(schemas, "rodapé", 5)[0]["name"] == "footnotes_insert"


def test_find_tools_excludes_sidebar_only_domains():
    # brainstorming is sidebar-only: not an available domain, and its tool is not
    # surfaced even when it would match the query.
    result = FindTools().execute(_ctx_real(_real_registry()), query="research")
    assert "brainstorming" not in result["available_domains"]
    assert "brainstorm_research_web" not in {t["name"] for t in result["tools"]}


def test_direct_flat_excludes_sidebar_only_domains():
    names = {t["name"] for t in _handler_real("direct_flat", _real_registry())._mcp_tools_list({})["tools"]}
    assert "footnotes_insert" in names              # normal specialized still exposed
    assert "brainstorm_research_web" not in names   # sidebar-only kept out


def test_no_document_discovery_lists_full_catalog():
    # With no document open, discovery must still surface app-specific specialized tools
    # (the registry would otherwise filter them out by uno_services) and flag the state.
    reg = ToolRegistry(MagicMock())
    reg.register(FindTools())
    reg.register(_AppSpecificTool())
    result = FindTools().execute(_ctx_real(reg), query="chart")   # _ctx_real has doc=None
    assert "create_chart" in {t["name"] for t in result["tools"]}
    assert "note" in result


def test_explicit_sidebar_domain_returns_no_tools():
    # An explicit sidebar-only domain must surface nothing -- not even its required-core.
    result = FindTools().execute(_ctx_real(_real_registry()), domain="brainstorming")
    assert result["tools"] == []
    assert result["domain"] == "brainstorming"


def test_get_domain_guidance_is_app_neutral_when_app_unknown():
    # No app context -> chart guidance must cover BOTH Calc (data_range) and Writer/Draw
    # (headers/rows), not silently assume Writer.
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
    result = FindTools().execute(ctx, domain="charts")
    g = result["domain_guidance"]["charts"]
    assert "data_range" in g and "headers" in g


def test_get_domain_guidance_python_is_app_neutral_when_app_unknown():
    # python guidance with no app must cover Calc (data_range) AND Writer/Draw (document
    # tools), not assume non-Calc.
    g = get_domain_guidance("python", agent_label=None)
    assert "data_range" in g
    assert "document tools" in g.lower()


def test_run_venv_python_advertises_superset_when_doc_unknown():
    # With no document (doc_type=None) the python tool must advertise the superset schema
    # so discovery still surfaces data_range (Calc-only), not just `code`.
    from plugin.calc.python.venv import RunVenvPythonScript
    params = RunVenvPythonScript().get_parameters(None)
    assert "data_range" in params["properties"]


def test_run_venv_python_description_neutral_when_doc_unknown():
    # The no-doc description must match the superset schema: mention Calc's data_range AND
    # the Writer/Draw path, not the Writer-only "does not inject" text.
    from plugin.calc.python.venv import RunVenvPythonScript
    desc = RunVenvPythonScript().get_description(None)
    assert "data_range" in desc
    assert "document tools" in desc.lower()


def test_execute_tolerates_unhashable_name():
    # A malformed schema whose `name` is unhashable (a list) must not crash the sidebar filter.
    registry = MagicMock()
    registry.get_schemas.return_value = [
        {"name": ["bad"], "description": "x", "inputSchema": {}},
        _schema("footnotes_insert", "insert a footnote"),
    ]
    registry.get_tools.return_value = []
    result = FindTools().execute(_ctx(registry), query="footnote")
    assert result["status"] == "ok"


def test_execute_drops_schemas_with_unusable_names():
    # Beyond not crashing: a schema with a non-string/empty name is dropped, not returned
    # (it isn't a callable MCP tool).
    registry = MagicMock()
    registry.get_schemas.return_value = [
        {"name": ["bad"], "description": "x", "inputSchema": {}},
        {"name": "", "description": "y", "inputSchema": {}},
        _schema("footnotes_insert", "insert a footnote"),
    ]
    registry.get_tools.return_value = []
    names = [t.get("name") for t in FindTools().execute(_ctx(registry), query="footnote")["tools"]]
    assert ["bad"] not in names and "" not in names
    assert "footnotes_insert" in names


def test_direct_flat_lists_app_specific_tools_without_document():
    # direct_flat with no document open must still list app-specific specialized tools
    # (uno_services), since that mode has no find_tools fallback.
    reg = ToolRegistry(MagicMock())
    for cls in (FindTools, _FtInsert, _AppSpecificTool):
        reg.register(cls())
    names = {t["name"] for t in _handler_real("direct_flat", reg)._mcp_tools_list({})["tools"]}
    assert "create_chart" in names        # app-specific surfaces despite no doc
    assert "footnotes_insert" in names


def test_delegate_no_doc_keeps_doc_type_filtering():
    # The no-doc broad catalog is direct_flat-only: delegate (default) must stay byte-for-
    # byte unchanged -- an app-specific core tool is still filtered out with no document.
    reg = ToolRegistry(MagicMock())
    for cls in (_CoreTool, _AppSpecificCoreTool):
        reg.register(cls())
    names = {t["name"] for t in _handler_real("delegate", reg)._mcp_tools_list({})["tools"]}
    assert "apply_document_content" in names   # universal core stays
    assert "calc_only_core" not in names       # app-specific core filtered (no document)


def test_direct_flat_unresolved_document_url_does_not_broaden():
    # An explicit but unresolvable X-Document-URL is NOT "no target": don't broaden the
    # catalog -- keep normal filtering (the app-specific tool stays filtered).
    reg = ToolRegistry(MagicMock())
    for cls in (FindTools, _FtInsert, _AppSpecificTool):
        reg.register(cls())
    handler = _handler_real("direct_flat", reg)
    handler.services.document.resolve_document_by_url.return_value = (None, None)
    names = {t["name"] for t in handler._mcp_tools_list({}, document_url="file:///missing.odt")["tools"]}
    assert "create_chart" not in names


def test_real_active_document_treats_start_center_as_none():
    # The Start Center is a live (non-document) component, so it does not support the
    # OfficeDocument service. _real_active_document must normalize that to None so "no
    # document open" is detected -- the live state that doc=None unit tests alone don't reach.
    # A real but unsupported doc (Math/Base) DOES support OfficeDocument, so it passes through
    # and is left to fail with the clearer "unsupported document" error.
    from plugin.mcp.mcp_protocol import _real_active_document

    doc_svc = MagicMock()
    phantom = MagicMock()
    phantom.supportsService.return_value = False             # Start Center: not a document
    doc_svc.get_active_document.return_value = phantom
    assert _real_active_document(doc_svc) is None

    real = MagicMock()
    real.supportsService.return_value = True                 # real doc (any type) passes through
    doc_svc.get_active_document.return_value = real
    assert _real_active_document(doc_svc) is real

    doc_svc.get_active_document.return_value = None
    assert _real_active_document(doc_svc) is None             # None stays None


def test_direct_flat_treats_start_center_as_no_document():
    # End to end: with the Start Center active (no real doc), direct_flat must broaden to
    # the full catalog, not return almost nothing.
    reg = ToolRegistry(MagicMock())
    for cls in (FindTools, _FtInsert, _AppSpecificTool):
        reg.register(cls())
    handler = _handler_real("direct_flat", reg)
    phantom = MagicMock()
    phantom.supportsService.return_value = False             # Start Center: not a document
    handler.services.document.get_active_document.return_value = phantom
    names = {t["name"] for t in handler._mcp_tools_list({})["tools"]}
    assert "create_chart" in names
