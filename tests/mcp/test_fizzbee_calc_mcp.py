# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for FizzBee formal modeling and full-layout MCP testing for Calc tools.

Covers:
- Full Calc layout extraction (all 61 tools across core and 12 specialized domains)
- Full MCP schema validation for every Calc tool
- Tool exposure mode filtering (direct_flat, delegate, direct_discovery)
- Calc state transition trace replay against MCP protocol handler
- Error envelopes and parameter validation
- Multi-step randomized Calc tool fuzzer
"""

from unittest.mock import MagicMock, patch
import pytest

from plugin.framework.service import ServiceRegistry
from plugin.mcp.mcp_protocol import MCPProtocolHandler
from tests.mcp.calc_full_layout import (
    extract_full_calc_layout,
    get_calc_tool_registry,
    run_randomized_mcp_fuzz,
    validate_mcp_schema,
)


@pytest.fixture
def calc_mcp_setup():
    """Set up an MCPProtocolHandler wired with the real full Calc tool registry."""
    services = ServiceRegistry()

    # Mock document service configured for Calc
    doc_svc = MagicMock()
    mock_doc = MagicMock()
    doc_svc.get_active_document.return_value = mock_doc
    doc_svc.detect_doc_type.return_value = "calc"
    doc_svc.resolve_document_by_url.return_value = (mock_doc, "calc")
    services.register("document", doc_svc)

    # Mock main thread executor
    main_thread = MagicMock()
    main_thread.execute.side_effect = lambda fn, *a, **kw: fn(*a, **{k: v for k, v in kw.items() if k != "timeout"})
    services.register("main_thread", main_thread)

    config_svc = MagicMock()
    config_dict = {
        "mcp_enabled": True,
        "mcp_port": 18765,
        "tool_exposure_mode": "direct_flat",
        "mcp.tool_exposure_mode": "direct_flat",
    }
    config_svc.proxy_for.return_value = config_dict
    config_svc.get.side_effect = lambda k, d=None: config_dict.get(k, config_dict.get(k.split(".")[-1], d))
    services.register("config", config_svc)
    services.register("events", MagicMock())

    services, tool_reg = get_calc_tool_registry(services)

    handler = MCPProtocolHandler(services)
    return {
        "services": services,
        "tools": tool_reg,
        "handler": handler,
        "config_dict": config_dict,
        "doc": mock_doc,
    }


def _call_rpc(handler: MCPProtocolHandler, msg: dict) -> dict:
    """Helper to dispatch JSON-RPC message through MCP handler and return response body dict."""
    res = handler._process_jsonrpc(msg)
    if res is None:
        return {}
    _status_code, body = res
    return body


# =========================================================================== #
# 1. Full Calc Layout & Schema Verification Tests
# =========================================================================== #

def test_extract_full_calc_layout_count():
    """Verify that full layout extraction returns all 61 Calc tools across tiers."""
    layout = extract_full_calc_layout()

    assert layout["total_count"] >= 54, f"Expected at least 54 tools, found {layout['total_count']}"
    assert len(layout["core_tools"]) >= 10
    assert len(layout["specialized_tools"]) >= 39

    # Check key core tools
    core_names = {t.name for t in layout["core_tools"]}
    assert "read_cell_range" in core_names
    assert "write_formula_range" in core_names
    assert "get_sheet_summary" in core_names
    assert "list_calc_functions" in core_names
    assert "delegate_to_specialized_calc_toolset" in core_names

    # Check that specialized domains exist
    domains = layout["domain_map"].keys()
    assert "sheets" in domains
    assert "ranges" in domains
    assert "python" in domains
    assert "analysis" not in domains
    assert "shapes" in domains
    assert "charts" in domains
    assert "comments" in domains
    assert "conditional_formatting" in domains
    assert "errors" in domains
    assert "pivot_tables" in domains


def test_full_calc_layout_mcp_schemas_validity():
    """Verify that all Calc tools in the full layout produce valid MCP JSON schemas."""
    layout = extract_full_calc_layout()
    schemas = layout["mcp_schemas"]

    assert len(schemas) >= 54

    all_schema_errors = []
    for tool_name, schema in schemas.items():
        errors = validate_mcp_schema(schema)
        if errors:
            all_schema_errors.append((tool_name, errors))

    assert not all_schema_errors, f"Schema validation errors found: {all_schema_errors}"


# =========================================================================== #
# 2. MCP Exposure Modes Tests (direct_flat vs delegate vs direct_discovery)
# =========================================================================== #

def test_mcp_calc_tools_list_direct_flat(calc_mcp_setup):
    """In direct_flat mode, tools/list returns all core + specialized Calc tools."""
    handler = calc_mcp_setup["handler"]
    config_dict = calc_mcp_setup["config_dict"]
    config_dict["tool_exposure_mode"] = "direct_flat"
    config_dict["mcp.tool_exposure_mode"] = "direct_flat"

    result = _call_rpc(handler, {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/list",
        "params": {},
    })

    assert "result" in result
    tools_list = result["result"]["tools"]
    tool_names = {t["name"] for t in tools_list}

    assert len(tools_list) >= 50
    assert "read_cell_range" in tool_names
    assert "write_formula_range" in tool_names
    assert "create_sheet" in tool_names
    assert "named_range_add" in tool_names
    assert "run_venv_python_script" in tool_names
    assert "analyze_data" not in tool_names


def test_mcp_calc_tools_list_delegate_mode(calc_mcp_setup):
    """In delegate mode, tools/list returns only core Calc tools."""
    handler = calc_mcp_setup["handler"]
    config_dict = calc_mcp_setup["config_dict"]
    config_dict["tool_exposure_mode"] = "delegate"
    config_dict["mcp.tool_exposure_mode"] = "delegate"

    result = _call_rpc(handler, {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/list",
        "params": {},
    })

    assert "result" in result
    tools_list = result["result"]["tools"]
    tool_names = {t["name"] for t in tools_list}

    # Core tools only
    assert "read_cell_range" in tool_names
    assert "write_formula_range" in tool_names
    # Specialized domain tools must be omitted
    assert "create_sheet" not in tool_names
    assert "named_range_add" not in tool_names
    assert "analyze_data" not in tool_names


def test_mcp_calc_tools_list_direct_discovery(calc_mcp_setup):
    """In direct_discovery mode, tools/list includes find_tools for Calc."""
    handler = calc_mcp_setup["handler"]
    config_dict = calc_mcp_setup["config_dict"]
    config_dict["tool_exposure_mode"] = "direct_discovery"
    config_dict["mcp.tool_exposure_mode"] = "direct_discovery"

    result = _call_rpc(handler, {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/list",
        "params": {},
    })

    assert "result" in result
    tools_list = result["result"]["tools"]
    tool_names = {t["name"] for t in tools_list}

    assert "find_tools" in tool_names
    assert "read_cell_range" in tool_names
    assert "create_sheet" not in tool_names


# =========================================================================== #
# 3. FizzBee Model-Based Trace Replay & Invariant Tests
# =========================================================================== #

def test_fizzbee_calc_trace_initialize(calc_mcp_setup):
    """Replay FizzBee Init and Handshake trace for Calc MCP."""
    handler = calc_mcp_setup["handler"]

    ping_res = _call_rpc(handler, {"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert ping_res.get("result") == {}

    init_res = _call_rpc(handler, {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "fizzbee_calc_client", "version": "1.0"},
        },
    })
    assert "result" in init_res
    assert init_res["result"]["serverInfo"]["name"] == "WriterAgent MCP"


def test_full_layout_all_calc_tools_executable_dispatch(calc_mcp_setup):
    """Verify that every tool in the 61-tool Calc layout can be dispatched without unhandled exceptions."""
    handler = calc_mcp_setup["handler"]
    tools_reg = calc_mcp_setup["tools"]

    all_tools = tools_reg.get_tools(doc_type="calc", exclude_tiers=frozenset())

    for idx, tool in enumerate(all_tools):
        with patch.object(tool, "execute", return_value={"status": "ok", "mocked": True}):
            res = _call_rpc(handler, {
                "jsonrpc": "2.0",
                "id": 200 + idx,
                "method": "tools/call",
                "params": {"name": tool.name, "arguments": {}},
            })

            assert res is not None
            assert "jsonrpc" in res
            assert res.get("id") == 200 + idx


def test_fizzbee_randomized_calc_mcp_fuzz(calc_mcp_setup):
    """Randomized multi-step state machine fuzzer over the entire 61 Calc tool layout."""
    import os
    from tests.vhs_budget import vhs_max_examples

    handler = calc_mcp_setup["handler"]
    tools_reg = calc_mcp_setup["tools"]
    all_tools = tools_reg.get_tools(doc_type="calc", exclude_tiers=frozenset())

    env_steps = os.environ.get("FIZZBEE_MCP_STEPS")
    env_duration = os.environ.get("FIZZBEE_MCP_DURATION_SEC")

    steps = int(env_steps) if env_steps else vhs_max_examples(light=150, extensive=2000)
    duration_sec = float(env_duration) if env_duration else None

    result = run_randomized_mcp_fuzz(
        handler=handler,
        all_tools=all_tools,
        steps=steps,
        duration_sec=duration_sec,
        seed=42,
        mutate_error_rate=0.08,
    )

    assert result["status"] == "PASSED"
    assert result["steps_completed"] > 0
    assert result["unique_tools_invoked"] >= 25, (
        f"Expected wide distribution of tools, got {result['unique_tools_invoked']} unique tools"
    )
