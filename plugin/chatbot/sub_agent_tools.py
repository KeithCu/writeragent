# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable, ClassVar, Sequence

from plugin.doc.document_research import (
    DOC_RESEARCH_DISCOVERY_TOOL_NAMES,
    filter_document_research_discovery_tools,
)
from plugin.framework.errors import ToolExecutionError, format_error_payload
from plugin.framework.tool import ToolBase

if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext

log = logging.getLogger("writeragent.sub_agent_tools")

HTML_CONTENT_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "content": {
            "type": "array",
            "items": {"type": "string"},
            "description": "List of HTML fragments (e.g. <h1>, <p>, <ul>). No Markdown.",
        },
        "target": {
            "type": "string",
            "enum": ["beginning", "end", "full_document"],
            "description": "Where to insert. Default end. Use full_document only when the doc is empty.",
        },
    },
    "required": ["content"],
}


def normalize_html_content_array(content: Any) -> list[str] | None:
    """Accept list of HTML strings or a single string (coerce to one-element list). Reject non-strings."""
    if content is None:
        return None
    if isinstance(content, str):
        text = content.strip()
        return [text] if text else None
    if isinstance(content, list):
        out: list[str] = []
        for item in content:
            if item is None:
                continue
            if not isinstance(item, str):
                return None
            s = item.strip()
            if s:
                out.append(s)
        return out if out else None
    return None


def apply_html_content(tool: ToolBase, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
    """Shared implementation for apply_document_content wrapper tools."""
    content = normalize_html_content_array(kwargs.get("content"))
    if not content:
        return tool._tool_error(
            "content must be a non-empty array of HTML strings.",
            code="INVALID_CONTENT",
        )

    target = kwargs.get("target") or "end"
    if target not in ("beginning", "end", "full_document"):
        target = "end"

    registry = ctx.services.get("tools") if ctx.services else None
    apply_tool = registry.get("apply_document_content") if registry else None
    if apply_tool is None:
        return tool._tool_error("apply_document_content is not available.", code="TOOL_NOT_FOUND")

    return apply_tool.execute_safe(ctx, content=content, target=target)


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
            return WebResearchTool().execute(ctx, query=query, deep=True)
        return WebResearchTool().execute(ctx, query=query)


class _SubagentSessionToolBase(ToolBase):
    """Nameless base class for sidebar subagent session orchestrators."""

    tier: str = "specialized_control"
    is_mutation: bool | None = False
    long_running: bool = True
    agent_label: str = ""
    runner: Callable[..., dict[str, Any]] | None = None

    def is_async(self) -> bool:
        return True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        if self.runner is None:
            raise NotImplementedError("Subagent session tool must define runner")
        return run_subagent_tool(self.agent_label, self.runner, ctx, **kwargs)


def get_sub_agent_instructions(mode_instructions: str, *, tool_names: Sequence[str] = ()) -> str:
    """Full system instructions for a smol sub-agent."""
    from plugin.framework.prompts import (
        APPLY_DOCUMENT_TARGET_RULES,
        SUB_AGENT_HTML_CONTENT_RULES,
        format_humanizer_guidance_prompt_block,
        get_chat_response_format_instructions,
    )

    parts = [mode_instructions]
    tool_set = set(tool_names)
    if tool_set & {"apply_document_content", "write_document_section", "save_design_spec"}:
        parts.append(SUB_AGENT_HTML_CONTENT_RULES.strip())
    if "apply_document_content" in tool_set:
        parts.append(APPLY_DOCUMENT_TARGET_RULES.strip())
    parts.append(get_chat_response_format_instructions())
    h_block = format_humanizer_guidance_prompt_block()
    if h_block:
        parts.append(h_block.strip())
    return "\n\n".join(parts)


def collect_sub_agent_tools(
    ctx: ToolContext,
    active_domain: str,
    *,
    include_document_research: bool = True,
) -> list[ToolBase]:
    """Tools for a specialized sub-agent (mode domain + optional doc research discovery reads)."""
    registry = ctx.services.get("tools")
    primary = registry.get_tools(
        doc_type=ctx.doc_type,
        uno_services_supported=ctx.uno_services_supported,
        active_domain=active_domain,
        exclude_tiers=(),
    )
    by_name = {t.name: t for t in primary if t.name}
    if include_document_research:
        doc_res = registry.get_tools(
            doc_type=ctx.doc_type,
            uno_services_supported=ctx.uno_services_supported,
            active_domain="document_research",
            exclude_tiers=(),
        )
        doc_res = filter_document_research_discovery_tools(doc_res)
        allow = set(DOC_RESEARCH_DISCOVERY_TOOL_NAMES)
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
    """Execute a specialized subagent runner with standard error handling."""
    query = kwargs.get("query")
    try:
        return runner(ctx, **kwargs)
    except Exception as e:
        log.exception("%s execution failed", agent_label)
        from plugin.framework.errors import (
            WriterAgentException,
            is_real_disposal,
            is_tool_document_disposed,
            resolve_exception_message,
        )

        doc = getattr(ctx, "doc", None)
        if is_real_disposal(e) or (doc is not None and is_tool_document_disposed(e, doc)):
            raise

        if isinstance(e, WriterAgentException):
            payload = format_error_payload(e)
            payload["details"] = {**(e.details or {}), "query": query}
            msg = resolve_exception_message(e)
            if msg:
                payload["message"] = msg
            return payload

        msg = resolve_exception_message(e)
        if not msg:
            msg = f"{agent_label} failed: {type(e).__name__}"
        else:
            msg = f"{agent_label} failed: {msg}"
        err = ToolExecutionError(msg, details={"query": query})
        return format_error_payload(err)
