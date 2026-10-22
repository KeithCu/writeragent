# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Writer full toolset layout extraction and MCP schema validation helper.

Extracts all Writer tools across all tiers (core, specialized, specialized_control)
and provides verification against MCP wire format requirements.
"""

from __future__ import annotations

import tempfile
from typing import Any, Dict, List, Mapping, Tuple
from unittest.mock import MagicMock

from plugin.framework.config import init_config
from plugin.framework.service import ServiceRegistry
from plugin.framework.tool import ToolBase, ToolRegistry, to_mcp_schema
from plugin.writer import WriterModule


def _ensure_headless_config() -> None:
    """Ensure config is initialized with a valid UserConfig temp directory for headless runs."""
    from plugin.framework.config import _resolved_config_path

    if _resolved_config_path is not None:
        return
    ctx = MagicMock()
    path_settings = ctx.getServiceManager.return_value.createInstanceWithContext.return_value
    path_settings.UserConfig = tempfile.mkdtemp(prefix="wa-mcp-cfg-")
    init_config(ctx)


def get_writer_tool_registry(
    services: ServiceRegistry | None = None,
) -> Tuple[ServiceRegistry, ToolRegistry]:
    """Create and initialize an isolated ServiceRegistry and ToolRegistry loaded with Writer tools."""
    # Ensure config initialized with mock context for headless/isolated test execution
    _ensure_headless_config()

    if services is None:
        services = ServiceRegistry()
        services.register("config", MagicMock())
        services.register("document", MagicMock())
        services.register("events", MagicMock())

    if "tools" not in services:
        tools = ToolRegistry(services)
        services.register("tools", tools)
    else:
        tools = services.tools

    # Initialize Writer module to discover and register all Writer tools
    writer_mod = WriterModule()
    writer_mod.initialize(services)

    # Discover shared doc tools (such as find_tools)
    tools.auto_discover_package("plugin.doc")

    return services, tools


def extract_full_writer_layout(
    tools: ToolRegistry | None = None,
) -> Dict[str, Any]:
    """Extract the complete categorized layout of Writer tools.

    Returns a dictionary containing:
    - total_count: Total number of Writer tools
    - core_tools: List of core ToolBase instances
    - specialized_tools: List of specialized ToolBase instances
    - control_tools: List of specialized_control ToolBase instances
    - domain_map: Dict mapping domain name -> list of ToolBase instances
    - all_tools: List of all ToolBase instances
    - mcp_schemas: Dict mapping tool_name -> MCP schema dict
    """
    if tools is None:
        _services, tools = get_writer_tool_registry()

    all_tools: List[ToolBase] = tools.get_tools(
        doc_type="writer",
        exclude_tiers=frozenset(),
    )

    core_tools: List[ToolBase] = [t for t in all_tools if getattr(t, "tier", None) == "core"]
    specialized_tools: List[ToolBase] = [t for t in all_tools if getattr(t, "tier", None) == "specialized"]
    control_tools: List[ToolBase] = [t for t in all_tools if getattr(t, "tier", None) == "specialized_control"]

    domain_map: Dict[str, List[ToolBase]] = {}
    for t in specialized_tools:
        domain = getattr(t, "specialized_domain", getattr(t, "domain", "general"))
        domain_map.setdefault(domain or "general", []).append(t)

    mcp_schemas: Dict[str, Dict[str, Any]] = {}
    for t in all_tools:
        mcp_schemas[t.name] = to_mcp_schema(t, doc_type="writer")

    return {
        "total_count": len(all_tools),
        "core_tools": core_tools,
        "specialized_tools": specialized_tools,
        "control_tools": control_tools,
        "domain_map": domain_map,
        "all_tools": all_tools,
        "mcp_schemas": mcp_schemas,
    }


def validate_mcp_schema(schema: Dict[str, Any]) -> List[str]:
    """Validate that an MCP tool schema complies with MCP protocol requirements.

    Returns a list of error descriptions (empty list if valid).
    """
    errors: List[str] = []

    if not isinstance(schema, dict):
        return ["Schema is not a dictionary"]

    name = schema.get("name")
    if not name or not isinstance(name, str):
        errors.append(f"Missing or invalid tool name: {name}")

    desc = schema.get("description")
    if desc is None or not isinstance(desc, str):
        errors.append(f"Missing or invalid description for tool '{name}'")

    input_schema = schema.get("inputSchema")
    if not isinstance(input_schema, dict):
        errors.append(f"Missing or invalid inputSchema for tool '{name}'")
    else:
        if input_schema.get("type") != "object":
            errors.append(f"inputSchema type must be 'object' for tool '{name}', got {input_schema.get('type')}")

        properties = input_schema.get("properties")
        if properties is not None and not isinstance(properties, dict):
            errors.append(f"inputSchema properties must be a dict for tool '{name}'")
        elif isinstance(properties, dict):
            # All Writer MCP tools should have document_url in properties
            if "document_url" not in properties:
                errors.append(f"Missing 'document_url' in properties for tool '{name}'")

    return errors


def generate_random_tool_arguments(
    schema: Dict[str, Any],
    rng: Any,
    mutate_error_rate: float = 0.05,
) -> Dict[str, Any]:
    """Generate randomized input arguments based on a tool's JSON Schema."""
    args: Dict[str, Any] = {}
    input_schema = schema.get("inputSchema", {})
    properties = input_schema.get("properties", {})
    required = set(input_schema.get("required", []))

    for prop_name, prop_spec in properties.items():
        if not isinstance(prop_spec, dict):
            continue

        # For optional properties, occasionally omit them
        if prop_name not in required and rng.random() < 0.3:
            continue

        # If mutating, occasionally inject malformed data
        if rng.random() < mutate_error_rate:
            args[prop_name] = rng.choice([None, {"unexpected": "dict"}, ["invalid", "array"], 99999999])
            continue

        prop_type = prop_spec.get("type", "string")
        if isinstance(prop_type, list):
            prop_type = next((t for t in prop_type if t != "null"), "string")

        enum_vals = prop_spec.get("enum")
        if enum_vals and isinstance(enum_vals, list):
            args[prop_name] = rng.choice(enum_vals)
        elif prop_type == "string":
            if "target" in prop_name:
                args[prop_name] = rng.choice(["end", "start", "replace_all", "selection"])
            elif "name" in prop_name or "id" in prop_name:
                args[prop_name] = rng.choice(["item_1", "item_2", "test_id", "Section_A"])
            elif "text" in prop_name or "content" in prop_name:
                args[prop_name] = rng.choice(["Hello world", "<p>Paragraph text</p>", "Sample test line.\n", ""])
            else:
                args[prop_name] = rng.choice(["test_val", "default", "custom_opt"])
        elif prop_type == "integer" or prop_type == "number":
            args[prop_name] = rng.choice([0, 1, 2, 5, 10, -1])
        elif prop_type == "boolean":
            args[prop_name] = rng.choice([True, False])
        elif prop_type == "array":
            args[prop_name] = rng.choice([["sample1"], ["row1", "row2"], []])
        else:
            args[prop_name] = "test_data"

    return args


# Mutate-nested-then-read: independent tool picks never put a cursor in a cell
# then read it. These pairs stay on the MCP wire (MagicMock execute) but the
# *order* is what found apply_document_content using body XText inside a cell.
WRITER_MUTATE_READ_PAIRS: Mapping[str, Tuple[str, ...]] = {
    "table_insert": ("get_document_content", "apply_document_content", "table_get_cells"),
    "table_set_cell": ("get_document_content", "apply_document_content"),
    "manage_table_structure": ("get_document_content", "table_get_cells"),
    "table_delete": ("get_document_content", "table_list"),
    "apply_document_content": ("get_document_content",),
    "set_selection": ("apply_document_content", "get_document_content"),
    "search_in_document": ("get_document_content", "apply_document_content"),
}


def pick_followup_tool(
    last_tool: str,
    tool_names: List[str],
    rng: Any,
    pairs: Mapping[str, Tuple[str, ...]] | None = None,
) -> str | None:
    """Return a follow-up tool name if last_tool has a pair present in tool_names."""
    table = WRITER_MUTATE_READ_PAIRS if pairs is None else pairs
    candidates = [name for name in table.get(last_tool, ()) if name in tool_names]
    if not candidates:
        return None
    return rng.choice(candidates)


def run_randomized_mcp_fuzz(
    handler: Any,
    all_tools: List[ToolBase],
    steps: int = 100,
    duration_sec: float | None = None,
    seed: int = 42,
    mutate_error_rate: float = 0.05,
    pair_bias: float = 0.4,
) -> Dict[str, Any]:
    """Execute a randomized fuzzing loop over the full layout of Writer tools.

    Runs either for a fixed number of steps or until duration_sec expires.
    Validates JSON-RPC envelopes and MCP protocol invariants on every single request.

    pair_bias (default 0.4): after a mutate tool, this fraction of tool_calls
    pick a follow-up from WRITER_MUTATE_READ_PAIRS instead of an independent tool.
    """
    import random
    import time

    rng = random.Random(seed)
    start_time = time.time()
    call_counts: Dict[str, int] = {}
    completed_steps = 0
    errors_encountered = 0
    paired_followups = 0

    tool_map = {t.name: t for t in all_tools}
    tool_names = list(tool_map.keys())
    last_tool_name: str | None = None

    while True:
        if duration_sec is not None:
            if time.time() - start_time >= duration_sec:
                break
        elif completed_steps >= steps:
            break

        completed_steps += 1
        action_type = rng.choices(["tool_call", "tools_list", "ping", "initialize"], weights=[80, 10, 5, 5])[0]

        if action_type == "tool_call":
            tool_name = None
            if last_tool_name and pair_bias > 0 and rng.random() < pair_bias:
                tool_name = pick_followup_tool(last_tool_name, tool_names, rng)
                if tool_name is not None:
                    paired_followups += 1
            if tool_name is None:
                tool_name = rng.choice(tool_names)
            last_tool_name = tool_name
            tool = tool_map[tool_name]
            schema = to_mcp_schema(tool, doc_type="writer")
            arguments = generate_random_tool_arguments(schema, rng, mutate_error_rate=mutate_error_rate)
            if tool_name == "apply_document_content" and rng.random() < 0.5:
                arguments["target"] = "selection"

            # Patch tool.execute to return valid structured responses without external network I/O
            if not hasattr(tool, "_fuzz_patched"):
                tool.execute = lambda *args, **kwargs: {"status": "ok", "fuzz_mocked": True}  # type: ignore[method-assign]
                setattr(tool, "_fuzz_patched", True)

            req = {
                "jsonrpc": "2.0",
                "id": completed_steps,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": arguments},
            }
            call_counts[tool_name] = call_counts.get(tool_name, 0) + 1

        elif action_type == "tools_list":
            req = {"jsonrpc": "2.0", "id": completed_steps, "method": "tools/list", "params": {}}
        elif action_type == "ping":
            req = {"jsonrpc": "2.0", "id": completed_steps, "method": "ping"}
        else:
            req = {
                "jsonrpc": "2.0",
                "id": completed_steps,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "fizzbee_fuzzer", "version": "1.0"},
                },
            }

        res = handler._process_jsonrpc(req)
        assert res is not None, f"Handler returned None on step {completed_steps} for request: {req}"

        _status_code, body = res
        assert isinstance(body, dict), f"Handler response body is not a dict on step {completed_steps}: {body}"
        assert body.get("jsonrpc") == "2.0", f"Missing or invalid jsonrpc version: {body}"
        assert body.get("id") == completed_steps, f"Mismatched request id: expected {completed_steps}, got {body.get('id')}"

        if "error" in body or body.get("isError") is True:
            errors_encountered += 1

    elapsed = time.time() - start_time
    rate = completed_steps / max(elapsed, 0.001)

    return {
        "status": "PASSED",
        "steps_completed": completed_steps,
        "elapsed_sec": round(elapsed, 3),
        "calls_per_second": round(rate, 1),
        "errors_handled": errors_encountered,
        "unique_tools_invoked": len(call_counts),
        "call_distribution": call_counts,
        "paired_followups": paired_followups,
    }

