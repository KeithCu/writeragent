# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for FizzBee formal modeling and full-layout MCP testing for Writer tools.

Covers:
- Full layout extraction (all 100 Writer tools across core, specialized, and control tiers)
- Full MCP schema validation for every Writer tool
- Tool exposure mode filtering (direct_flat, delegate, direct_discovery)
- Domain discovery and catalog resolution
- FizzBee state transition trace replay against MCP protocol handler
- Error envelopes and parameter validation
"""

from unittest.mock import MagicMock, patch
import pytest

from plugin.framework.service import ServiceRegistry
from plugin.mcp.mcp_protocol import MCPProtocolHandler
from tests.mcp.writer_full_layout import (
    extract_full_writer_layout,
    get_writer_tool_registry,
    validate_mcp_schema,
)


@pytest.fixture
def writer_mcp_setup():
    """Set up an MCPProtocolHandler wired with the real full Writer tool registry."""
    services = ServiceRegistry()

    # Mock document service
    doc_svc = MagicMock()
    mock_doc = MagicMock()
    doc_svc.get_active_document.return_value = mock_doc
    doc_svc.detect_doc_type.return_value = "writer"
    doc_svc.resolve_document_by_url.return_value = (mock_doc, "writer")
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

    services, tool_reg = get_writer_tool_registry(services)

    handler = MCPProtocolHandler(services)
    return {
        "services": services,
        "tools": tool_reg,
        "handler": handler,
        "config_dict": config_dict,
        "doc": mock_doc,
    }


# =========================================================================== #
# 1. Full Layout & Schema Verification Tests
# =========================================================================== #

def test_extract_full_writer_layout_count():
    """Verify that full layout extraction returns the complete catalog of Writer tools across tiers."""
    layout = extract_full_writer_layout()

    assert layout["total_count"] >= 100, f"Expected at least 100 tools, found {layout['total_count']}"
    assert len(layout["core_tools"]) >= 10
    assert len(layout["specialized_tools"]) >= 85
    assert len(layout["control_tools"]) >= 1

    # Check key core tools
    core_names = {t.name for t in layout["core_tools"]}
    assert "apply_document_content" in core_names
    assert "get_document_content" in core_names
    assert "get_document_tree" in core_names
    assert "search_in_document" in core_names
    assert "delegate_to_specialized_writer_toolset" in core_names

    # Check that specialized domains exist
    domains = layout["domain_map"].keys()
    assert "bookmarks" in domains
    assert "footnotes" in domains
    assert "tables" in domains
    assert "tracking" in domains
    assert "page" in domains
    assert "structural" in domains
    assert "styles" in domains


def test_full_writer_layout_mcp_schemas_validity():
    """Verify that all Writer tools in the full layout produce valid MCP JSON schemas."""
    layout = extract_full_writer_layout()
    schemas = layout["mcp_schemas"]

    assert len(schemas) >= 100

    all_schema_errors = []
    for tool_name, schema in schemas.items():
        errors = validate_mcp_schema(schema)
        if errors:
            all_schema_errors.append((tool_name, errors))

    assert not all_schema_errors, f"Schema validation errors found: {all_schema_errors}"


def _call_rpc(handler: MCPProtocolHandler, msg: dict) -> dict:
    """Helper to dispatch JSON-RPC message through MCP handler and return response body dict."""
    res = handler._process_jsonrpc(msg)
    if res is None:
        return {}
    _status_code, body = res
    return body


# =========================================================================== #
# 2. MCP Exposure Modes Tests (direct_flat vs delegate vs direct_discovery)
# =========================================================================== #

def test_mcp_tools_list_direct_flat_exposes_full_layout(writer_mcp_setup):
    """In direct_flat mode, tools/list returns all core + specialized Writer tools."""
    handler = writer_mcp_setup["handler"]
    config_dict = writer_mcp_setup["config_dict"]
    config_dict["tool_exposure_mode"] = "direct_flat"
    config_dict["mcp.tool_exposure_mode"] = "direct_flat"

    # Call tools/list
    result = _call_rpc(handler, {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/list",
        "params": {},
    })

    assert "result" in result
    tools_list = result["result"]["tools"]
    tool_names = {t["name"] for t in tools_list}

    # In direct_flat, all specialized + core tools are listed (specialized_control excluded)
    assert len(tools_list) >= 95
    assert "apply_document_content" in tool_names
    assert "bookmark_create" in tool_names
    assert "footnotes_insert" in tool_names
    assert "table_list" in tool_names
    assert "manage_table_structure" in tool_names
    assert "specialized_workflow_finished" not in tool_names  # control tool excluded


def test_mcp_tools_list_delegate_mode_only_core(writer_mcp_setup):
    """In delegate mode, tools/list returns only core Writer tools."""
    handler = writer_mcp_setup["handler"]
    config_dict = writer_mcp_setup["config_dict"]
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

    # Specialized tools must not be listed in delegate mode
    assert "apply_document_content" in tool_names
    assert "bookmark_create" not in tool_names
    assert "footnotes_insert" not in tool_names
    assert "table_list" not in tool_names


def test_mcp_tools_list_direct_discovery_mode(writer_mcp_setup):
    """In direct_discovery mode, tools/list includes find_tools for dynamic lookup."""
    handler = writer_mcp_setup["handler"]
    config_dict = writer_mcp_setup["config_dict"]
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
    assert "apply_document_content" in tool_names
    assert "bookmark_create" not in tool_names


# =========================================================================== #
# 3. FizzBee Model-Based Trace Replay & Invariant Tests
# =========================================================================== #

def test_fizzbee_trace_initialize_and_session_handshake(writer_mcp_setup):
    """Replay FizzBee Init and Handshake trace on MCP handler."""
    handler = writer_mcp_setup["handler"]

    # 1. Ping
    ping_res = _call_rpc(handler, {"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert ping_res.get("result") == {}

    # 2. Initialize
    init_res = _call_rpc(handler, {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "fizzbee_test_client", "version": "1.0"},
        },
    })
    assert "result" in init_res
    assert init_res["result"]["serverInfo"]["name"] == "WriterAgent MCP"

    # 3. Resources & Prompts list (empty envelopes per spec)
    res_list = _call_rpc(handler, {"jsonrpc": "2.0", "id": 3, "method": "resources/list"})
    assert res_list["result"] == {"resources": []}

    prompts_list = _call_rpc(handler, {"jsonrpc": "2.0", "id": 4, "method": "prompts/list"})
    assert prompts_list["result"] == {"prompts": []}


def test_fizzbee_trace_tool_call_dispatch_and_error_invariants(writer_mcp_setup):
    """Replay FizzBee CallTool trace verifying safety invariants on error handling."""
    handler = writer_mcp_setup["handler"]

    # 1. Unknown tool returns structured error envelope
    bad_res = _call_rpc(handler, {
        "jsonrpc": "2.0",
        "id": 5,
        "method": "tools/call",
        "params": {"name": "non_existent_tool_xyz", "arguments": {}},
    })
    assert bad_res.get("isError") is True or "error" in bad_res or "isError" in bad_res.get("result", {})

    # 2. Gating invariant: Calling find_tools in delegate mode fails with UNKNOWN_TOOL error
    config_dict = writer_mcp_setup["config_dict"]
    config_dict["tool_exposure_mode"] = "delegate"

    find_tools_res = _call_rpc(handler, {
        "jsonrpc": "2.0",
        "id": 6,
        "method": "tools/call",
        "params": {"name": "find_tools", "arguments": {"domain": "bookmarks"}},
    })
    assert find_tools_res.get("isError") is True or (
        isinstance(find_tools_res.get("result"), dict) and find_tools_res["result"].get("isError") is True
    )


def test_full_layout_all_tools_executable_dispatch(writer_mcp_setup):
    """Verify that every tool in the 100-tool Writer layout can be dispatched without unhandled exceptions."""
    handler = writer_mcp_setup["handler"]
    tools_reg = writer_mcp_setup["tools"]

    all_tools = tools_reg.get_tools(doc_type="writer", exclude_tiers=frozenset())

    for idx, tool in enumerate(all_tools):
        # Dispatch tools/call with empty args (mocking execute to succeed)
        with patch.object(tool, "execute", return_value={"status": "ok", "mocked": True}):
            res = _call_rpc(handler, {
                "jsonrpc": "2.0",
                "id": 100 + idx,
                "method": "tools/call",
                "params": {"name": tool.name, "arguments": {}},
            })

            # Check that a response envelope is returned (never None or unhandled crash)
            assert res is not None
            assert "jsonrpc" in res
            assert res.get("id") == 100 + idx


def test_fizzbee_randomized_mcp_fuzz(writer_mcp_setup):
    """Randomized multi-step state machine fuzzer over the entire 108 Writer tool layout.

    Configurable via:
    - FIZZBEE_MCP_STEPS: Number of random tool requests to dispatch (default: 150, or 2000 under VHS extensive)
    - FIZZBEE_MCP_DURATION_SEC: Time limit in seconds (overrides steps count)
    """
    import os
    from tests.mcp.writer_full_layout import run_randomized_mcp_fuzz
    from tests.vhs_budget import vhs_max_examples

    handler = writer_mcp_setup["handler"]
    tools_reg = writer_mcp_setup["tools"]
    all_tools = tools_reg.get_tools(doc_type="writer", exclude_tiers=frozenset())

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
    assert result["unique_tools_invoked"] >= 40, (
        f"Expected wide distribution of tools, got {result['unique_tools_invoked']} unique tools"
    )

