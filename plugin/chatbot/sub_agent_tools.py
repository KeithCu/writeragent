from __future__ import annotations

from typing import Any, Callable, ClassVar

from plugin.doc.document_research import (
    DOC_RESEARCH_DISCOVERY_TOOL_NAMES,
    filter_document_research_discovery_tools,
)
from plugin.framework.prompts import (
    WRITER_APPLY_DOCUMENT_HTML_RULES,
    get_chat_response_format_instructions,
)
from plugin.framework.tool import ToolBase, ToolContext


class SubAgentResearchWebBase(ToolBase):
    """Base class for sub-agent web research tools."""

    tier: str = "specialized"
    is_mutation: bool | None = False
    long_running: bool = True
    deep_research: ClassVar[bool] = False
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Research question or topic."},
        },
        "required": ["query"],
    }

    def is_async(self) -> bool:
        return True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        from plugin.chatbot.web_research import WebResearchTool

        query = kwargs["query"]
        if self.deep_research:
            assert isinstance(query, str)
            return WebResearchTool().execute(ctx, query=query, deep=True)
        return WebResearchTool().execute(ctx, query=query)


def get_sub_agent_instructions(mode_instructions: str, ctx: Any | None = None) -> str:
    """Full system instructions for a smol sub-agent."""
    parts = [
        mode_instructions,
        WRITER_APPLY_DOCUMENT_HTML_RULES,
        get_chat_response_format_instructions(ctx),
    ]
    return "\n\n".join(parts)


def collect_sub_agent_tools(ctx: ToolContext, active_domain: str) -> list[ToolBase]:
    """Tools for a specialized sub-agent (mode domain + doc research discovery reads)."""
    registry = ctx.services.get("tools")
    primary = registry.get_tools(
        doc_type=ctx.doc_type,
        uno_services_supported=ctx.uno_services_supported,
        active_domain=active_domain,
        exclude_tiers=(),
    )
    doc_res = registry.get_tools(
        doc_type=ctx.doc_type,
        uno_services_supported=ctx.uno_services_supported,
        active_domain="document_research",
        exclude_tiers=(),
    )
    doc_res = filter_document_research_discovery_tools(doc_res, ctx.ctx)
    allow = set(DOC_RESEARCH_DISCOVERY_TOOL_NAMES)
    by_name = {t.name: t for t in primary if t.name}
    for t in doc_res:
        if t.name in allow and t.name not in by_name:
            by_name[t.name] = t
    return list(by_name.values())


def execute_subagent_tool(
    agent_label: str,
    runner: Callable[..., dict[str, Any]],
    ctx: ToolContext,
    kwargs: dict[str, Any],
) -> dict[str, Any]:
    from plugin.chatbot.smol_agent import run_subagent_tool

    return run_subagent_tool(agent_label, runner, ctx, **kwargs)
