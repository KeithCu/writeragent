# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Deep Research sidebar sub-agent: breadth/depth web research + document apply."""

from __future__ import annotations

from typing import Any, Callable, ClassVar

from plugin.chatbot.sub_agent_tools import (
    SubAgentResearchWebBase,
    _SubagentSessionToolBase,
    collect_sub_agent_tools,
    get_sub_agent_instructions,
)
from plugin.framework.tool import ToolBase, ToolContext

DEEP_RESEARCH_SUB_AGENT_INSTRUCTIONS = """DEEP RESEARCH MODE:
You perform multi-step public web research and write formatted results into the active Writer document when appropriate.

WORKFLOW:
1. Read document context when helpful (get_document_content / get_document_tree / search_in_document).
2. Run deep_research_web for the user's research query. This may take several minutes (parallel searches, adaptive rounds).
3. Convert the plain-text report to HTML and insert it with apply_document_content (JSON array of HTML strings; target end unless the user asked otherwise). Do NOT paste the full report into reply_to_user.
4. reply_to_user with a brief HTML summary of what you researched and where it was inserted.

HTML RULES (CRITICAL):
- apply_document_content content must be a JSON array of HTML strings — no Markdown (#, **, ```).
- reply_to_user must be HTML and brief (status/summary only).
- Do NOT use HTML entity escaping (&lt;p&gt;) — send real tags.
- Rewrite plain-text deep_research_web results as structured HTML (headings, paragraphs, lists) before apply_document_content.

TOOLS:
- deep_research_web: multi-step adaptive web research only (not the shallow web_research tool).
- apply_document_content: the ONLY way to write research into the document.
- reply_to_user: short chat confirmation when the turn is complete."""

_DEEP_RESEARCH_CORE_TOOLS = frozenset(["get_document_content", "get_document_tree", "search_in_document", "apply_document_content"])


class DeepResearchWebTool(SubAgentResearchWebBase):
    """Multi-step public web research (sidebar Deep Research only; not shallow web_research)."""

    specialized_domain: ClassVar[str | None] = "deep_research"
    specialized_cross_cutting: ClassVar[bool] = True
    required_core_tools: ClassVar[frozenset[str] | None] = _DEEP_RESEARCH_CORE_TOOLS
    doc_types: list[str] | None = ["writer", "calc", "draw", "impress"]
    intent: str | None = "review"
    name: str | None = "deep_research_web"
    description: str = (
        "Run breadth/depth public web research on a topic. Returns plain text; "
        "format as HTML and insert with apply_document_content."
    )
    deep_research: ClassVar[bool] = True


def _run_deep_research_agent(ctx: ToolContext, *, query: str = "", history_text: str | None = None, **kwargs: Any) -> dict[str, Any]:
    """Run one turn of the Deep Research smol sub-agent."""
    from plugin.chatbot.smol_agent import run_smol_side_turn

    tools = collect_sub_agent_tools(ctx, "deep_research", include_document_research=False)
    tool_names = [t.name for t in tools if t.name]
    instructions = get_sub_agent_instructions(DEEP_RESEARCH_SUB_AGENT_INSTRUCTIONS, tool_names=tool_names)
    return run_smol_side_turn(
        ctx,
        query=query,
        history_text=history_text,
        collector=lambda c: tools,
        instructions=instructions,
        examples_key="deep_research",
        status_message="Deep research...",
        stop_message="Deep research stopped by user.",
        error_prefix="Deep research failed",
    )


class DeepResearchSessionTool(_SubagentSessionToolBase):
    """Orchestrator for one turn of the Deep Research sub-agent (sidebar session)."""

    name: str | None = "deep_research_session"
    description: str = "Deep Research sub-agent (multi-step web research + optional document insert)."
    agent_label: str = "Deep research"
    runner: Callable[..., dict[str, Any]] | None = staticmethod(_run_deep_research_agent)
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "User message or research task."},
            "history_text": {"type": "string", "description": "Previous conversation text."},
        },
        "required": ["query"],
    }
