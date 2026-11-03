# Test helpers for ToolRegistry filters registered at WriterAgent bootstrap.
from __future__ import annotations

from typing import Any


def register_prompt_catalog_for_tests() -> None:
    from plugin.doc import peer_prompt, specialized_catalog
    from plugin.chatbot.memory import format_user_memory_prompt_block
    from plugin.chatbot.skills import format_humanizer_guidance_prompt_block
    from plugin.framework.prompts import set_prompt_catalog, set_system_prompt_block
    from plugin.vision.vision_availability import get_vision_core_directive

    set_prompt_catalog(
        specialized_catalog.get_specialized_domain_catalog,
        specialized_catalog.get_specialized_delegation_tool_hint,
        specialized_catalog.format_specialized_domains_description,
    )
    set_system_prompt_block("peer_outer", peer_prompt.get_peer_messaging_prompt_block)
    set_system_prompt_block("peer_inner", peer_prompt.get_peer_inner_choice_block)
    set_system_prompt_block("vision", get_vision_core_directive)
    set_system_prompt_block("memory", lambda: format_user_memory_prompt_block())
    set_system_prompt_block("humanizer", lambda: format_humanizer_guidance_prompt_block())
    import plugin.scripting.import_policy  # noqa: F401


def append_production_tool_registry_filters(registry: Any) -> None:
    from plugin.doc.peer_message import filter_peer_message_schemas
    from plugin.doc.specialized_base import narrow_delegate_domain_enums
    from plugin.framework.tool import SchemaFilterContext
    from plugin.vision.vision_availability import filter_vision_delegate_schemas, filter_vision_specialized_tools

    def _filter_vision_tools(tools: list[Any], ctx: Any = None) -> list[Any]:
        if ctx is None:
            return list(tools)
        return filter_vision_specialized_tools(list(tools), ctx)

    def _narrow_schemas(schemas: list[dict[str, Any]], context: SchemaFilterContext) -> list[dict[str, Any]]:
        return narrow_delegate_domain_enums(schemas, context.tools, context.uno_services)

    def _filter_vision_schemas(schemas: list[dict[str, Any]], context: SchemaFilterContext) -> list[dict[str, Any]]:
        if context.protocol != "openai" or context.ctx is None:
            return schemas
        return filter_vision_delegate_schemas(schemas, context.ctx)

    def _filter_peer_schemas(schemas: list[dict[str, Any]], context: SchemaFilterContext) -> list[dict[str, Any]]:
        peer_domain = context.active_domain if context.protocol == "openai" else None
        return filter_peer_message_schemas(
            schemas, context.ctx, doc=context.doc, active_domain=peer_domain
        )

    registry.tool_filters.append(_filter_vision_tools)
    registry.schema_filters.extend([_narrow_schemas, _filter_vision_schemas, _filter_peer_schemas])
