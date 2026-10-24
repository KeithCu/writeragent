from __future__ import annotations

import logging
from typing import Any, Callable, ClassVar

from plugin.doc.document_research import (
    DOC_RESEARCH_DISCOVERY_TOOL_NAMES,
    filter_document_research_discovery_tools,
)
from plugin.framework.errors import ToolExecutionError, format_error_payload
from plugin.framework.prompts import (
    WRITER_APPLY_DOCUMENT_HTML_RULES,
    get_chat_response_format_instructions,
)
from plugin.framework.tool import ToolBase, ToolContext

log = logging.getLogger("writeragent.sub_agent_tools")


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


def run_subagent_tool(
    agent_label: str,
    runner: Callable[..., dict[str, Any]],
    ctx: ToolContext,
    **kwargs: Any,
) -> dict[str, Any]:
    """Execute a specialized subagent runner with standard error handling.

    Failures are logged with ``log.exception`` so the traceback stays in the
    log. The payload shown to the model and UI is the exception text only.

    Args:
        agent_label: Human-readable label for logging and errors (e.g. 'Writing plan', 'PPT-Master').
        runner: Callable that executes the subagent turn (takes ctx and keyword arguments).
        ctx: ToolContext for the tool execution.
        **kwargs: Arguments passed to the tool (query, history_text, topic, etc.).

    Returns:
        Result dictionary from runner or formatted ToolExecutionError payload.
    """
    query = kwargs.get("query")
    try:
        return runner(ctx, **kwargs)
    except Exception as e:
        # This except is the boundary for subagent runners. log.exception
        # records the traceback; the payload keeps the exception text only.
        log.exception("%s execution failed", agent_label)
        err = ToolExecutionError(f"{agent_label} failed: {str(e)}", details={"query": query})
        return format_error_payload(err)



