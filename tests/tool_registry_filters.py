# Test helpers for ToolRegistry filters registered at WriterAgent bootstrap.
from __future__ import annotations

from typing import Any


def register_prompt_catalog_for_tests() -> None:
    from plugin.writeragent_hooks import register_writeragent_prompt_hooks

    register_writeragent_prompt_hooks()


def append_production_tool_registry_filters(registry: Any) -> None:
    from plugin.writeragent_hooks import register_writeragent_tool_filters

    register_writeragent_tool_filters(registry)
