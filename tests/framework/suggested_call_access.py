# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Test-only schema validation helpers for suggested_call payloads.

Shared between unit tests and live UNO tests (testing_runner). Kept free of pytest
imports so it can be imported inside LibreOffice's bundled Python runtime.
"""

from __future__ import annotations

from typing import Any

from plugin.framework.tool import ToolRegistry

_TEST_TOOL_REGISTRY: ToolRegistry | None = None


def get_test_tool_registry() -> ToolRegistry:
    """Return a ToolRegistry populated with WriterAgent tools for test validation.

    Loads the writer tool package so schema verification tests can look up
    tools such as apply_style without initializing a live LibreOffice instance.
    """
    global _TEST_TOOL_REGISTRY
    if _TEST_TOOL_REGISTRY is None:
        reg = ToolRegistry(None)
        reg.auto_discover_package("plugin.writer")
        _TEST_TOOL_REGISTRY = reg
    return _TEST_TOOL_REGISTRY


def validate_suggested_call(
    suggested_call: Any,
    *,
    registry: ToolRegistry | None = None,
) -> tuple[bool, str | None]:
    """Validate a suggested_call structure against a tool's parameter schema.

    Verifies that the suggestion dict provides valid 'tool' and 'arguments' keys,
    resolves the schema for the named tool, and ensures arguments satisfy all
    required parameters, matching types and enums, with no undeclared keys.
    """
    if not isinstance(suggested_call, dict):
        return False, f"suggested_call must be a dict, got {type(suggested_call).__name__}"

    tool_name = suggested_call.get("tool")
    if not isinstance(tool_name, str) or not tool_name.strip():
        return False, "suggested_call must contain non-empty 'tool' string"

    arguments = suggested_call.get("arguments")
    if not isinstance(arguments, dict):
        return False, "suggested_call must contain 'arguments' dictionary"

    reg = registry if registry is not None else get_test_tool_registry()
    target_tool = reg.get(tool_name)
    if target_tool is None:
        return False, f"Tool '{tool_name}' not found for schema validation"

    ok, err = target_tool.validate(**arguments)
    if not ok:
        return False, err
    schema = target_tool.get_parameters() or {}
    props = schema.get("properties") if isinstance(schema, dict) else None
    if props is not None:
        # Model-facing suggested_call must not include scripting_only_parameters
        # which validate() allows for Python scripts.
        for key in arguments:
            if key not in props:
                return False, f"Unknown parameter: {key}"
    return True, None


def assert_valid_suggested_call(
    suggested_call: Any,
    *,
    registry: ToolRegistry | None = None,
) -> None:
    """Assert that a suggested_call payload strictly conforms to its tool schema."""
    ok, err = validate_suggested_call(suggested_call, registry=registry)
    assert ok, f"suggested_call failed schema validation: {err}"
