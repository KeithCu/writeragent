# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""WriterAgent / LibrePy bootstrap hook registration (application → framework)."""

from __future__ import annotations

import weakref
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from plugin.framework.tool import SchemaFilterContext, ToolRegistry

_tool_filters_registered_for: weakref.WeakSet[ToolRegistry] = weakref.WeakSet()


def register_writeragent_prompt_hooks() -> None:
    """Prompt catalog, system blocks, python extras — WriterAgent and eval harness.

    Safe to call repeatedly: each call re-sets the same hooks.
    """

    from plugin.framework import config_schema
    from plugin.framework.prompts import (
        set_prompt_catalog,
        set_python_prompt_extras,
        set_python_sub_agent_prompt_helpers,
        set_system_prompt_block,
    )

    def _norm(endpoint_str: str, is_openwebui: bool) -> str:
        from plugin.chatbot.config_ui_helpers import endpoint_from_selector_text

        return endpoint_from_selector_text(endpoint_str)

    config_schema.set_endpoint_normalizer(_norm)

    from plugin.scripting.sandbox import install_alias_authorizer

    install_alias_authorizer()

    from plugin.scripting.import_policy import (
        format_matplotlib_plot_hint,
        format_units_helper_hint,
        format_venv_import_policy_for_prompt,
    )

    set_python_prompt_extras(lambda _agent: format_venv_import_policy_for_prompt(compact=True))
    set_python_sub_agent_prompt_helpers(
        lambda: format_venv_import_policy_for_prompt(compact=False),
        lambda doc_type: format_matplotlib_plot_hint(doc_type=doc_type),
        format_units_helper_hint,
    )

    from plugin.chatbot.skills import format_humanizer_guidance_prompt_block
    from plugin.doc import specialized_catalog
    from plugin.doc.peer_prompt import get_peer_inner_choice_block, get_peer_messaging_prompt_block
    from plugin.vision.vision_availability import get_vision_core_directive

    set_prompt_catalog(
        specialized_catalog.get_specialized_domain_catalog,
        specialized_catalog.get_specialized_delegation_tool_hint,
        specialized_catalog.format_specialized_domains_description,
    )
    set_system_prompt_block("peer_outer", get_peer_messaging_prompt_block)
    set_system_prompt_block("peer_inner", get_peer_inner_choice_block)
    set_system_prompt_block("vision", get_vision_core_directive)
    set_system_prompt_block("humanizer", lambda: format_humanizer_guidance_prompt_block())


def register_writeragent_chat_tooling(tools: ToolRegistry) -> None:
    """Prompt catalog/blocks/extras plus production schema and tool filters."""
    register_writeragent_prompt_hooks()
    register_writeragent_tool_filters(tools)


def register_writeragent_tool_filters(tools: ToolRegistry) -> None:
    """Production schema/tool filters on a ToolRegistry instance."""
    # Keyed on the instance, not id(): a new registry can reuse a freed id.
    if tools in _tool_filters_registered_for:
        return
    _tool_filters_registered_for.add(tools)

    from plugin.doc.peer_message import filter_peer_message_schemas
    from plugin.doc.specialized_base import narrow_delegate_domain_enums
    from plugin.vision.vision_availability import filter_vision_delegate_schemas, filter_vision_specialized_tools

    def _filter_vision_tools(tool_list: list[Any], ctx: Any = None) -> list[Any]:
        if ctx is None:
            return list(tool_list)
        return filter_vision_specialized_tools(list(tool_list), ctx)

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

    tools.tool_filters.append(_filter_vision_tools)
    tools.schema_filters.extend([_narrow_schemas, _filter_vision_schemas, _filter_peer_schemas])
