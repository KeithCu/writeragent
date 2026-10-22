# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Calc full toolset layout extraction and MCP schema validation helper.

Extracts all Calc tools across all tiers (core, specialized, specialized_control)
and provides verification against MCP wire format requirements.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple
from unittest.mock import MagicMock

from plugin.calc import CalcModule
from plugin.framework.service import ServiceRegistry
from plugin.framework.tool import ToolBase, ToolRegistry, to_mcp_schema
from tests.mcp.writer_full_layout import (
    _ensure_headless_config,
    generate_random_tool_arguments,
    run_randomized_mcp_fuzz,
    validate_mcp_schema,
)

__all__ = [
    "extract_full_calc_layout",
    "generate_random_tool_arguments",
    "get_calc_tool_registry",
    "run_randomized_mcp_fuzz",
    "validate_mcp_schema",
]


def get_calc_tool_registry(
    services: ServiceRegistry | None = None,
) -> Tuple[ServiceRegistry, ToolRegistry]:
    """Create and initialize an isolated ServiceRegistry and ToolRegistry loaded with Calc tools."""
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

    # Initialize Calc module to discover and register all Calc tools
    calc_mod = CalcModule()
    calc_mod.initialize(services)

    # Discover shared doc tools (such as find_tools, list_open_documents)
    tools.auto_discover_package("plugin.doc")

    return services, tools


def extract_full_calc_layout(
    tools: ToolRegistry | None = None,
) -> Dict[str, Any]:
    """Extract the complete categorized layout of Calc tools.

    Returns a dictionary containing:
    - total_count: Total number of Calc tools
    - core_tools: List of core ToolBase instances
    - specialized_tools: List of specialized ToolBase instances
    - control_tools: List of specialized_control ToolBase instances
    - domain_map: Dict mapping domain name -> list of ToolBase instances
    - all_tools: List of all ToolBase instances
    - mcp_schemas: Dict mapping tool_name -> MCP schema dict
    """
    if tools is None:
        _services, tools = get_calc_tool_registry()

    all_tools: List[ToolBase] = tools.get_tools(
        doc_type="calc",
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
        mcp_schemas[t.name] = to_mcp_schema(t, doc_type="calc")

    return {
        "total_count": len(all_tools),
        "core_tools": core_tools,
        "specialized_tools": specialized_tools,
        "control_tools": control_tools,
        "domain_map": domain_map,
        "all_tools": all_tools,
        "mcp_schemas": mcp_schemas,
    }
