# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Deep Research sidebar sub-agent: breadth/depth web research + document apply."""

from __future__ import annotations

from typing import Any, Callable, ClassVar, TYPE_CHECKING

from plugin.chatbot.sub_agent_tools import (
    SubAgentResearchWebBase,
    SubAgentSessionToolBase,
    collect_sub_agent_tools,
    get_sub_agent_instructions,
)

if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext

DEEP_RESEARCH_SUB_AGENT_INSTRUCTIONS = """DEEP RESEARCH MODE:
You perform multi-step public web research and write formatted results into the active document when appropriate.

WORKFLOW:
1. Read document context when helpful (get_document_content / get_document_tree / search_in_document).
2. Run deep_research_web for the user's research query. This may take several minutes (parallel searches, adaptive rounds).
3. reply_to_user with a brief HTML summary of what you researched. Do NOT paste the full report into reply_to_user; highlight key findings only.

TOOLS:
- deep_research_web: multi-step adaptive web research only (not the shallow web_research tool).
- reply_to_user: short HTML confirmation when the turn is complete."""

DEEP_RESEARCH_WRITER_INSERT_INSTRUCTIONS = """WRITER DOCUMENT INSERT (apply_document_content is in your tool list):
After step 2, convert the plain-text report to HTML and insert it with apply_document_content (JSON array of HTML strings; target end unless the user asked otherwise). Do NOT paste the full report into reply_to_user.
Then reply_to_user with a brief HTML summary of what you researched and where it was inserted.

HTML RULES (CRITICAL):
- apply_document_content content must be a JSON array of HTML strings — no Markdown (#, **, ```).
- reply_to_user must be HTML and brief (status/summary only).
- Do NOT use HTML entity escaping (&lt;p&gt;) — send real tags.
- Rewrite plain-text deep_research_web results as structured HTML (headings, paragraphs, lists) before apply_document_content.

- apply_document_content: the ONLY way to write research into the document."""

DEEP_RESEARCH_REPLY_ONLY_INSTRUCTIONS = """OUTPUT (no document insert tools):
Your reply_to_user HTML summary is the only output the user receives from this research run. Include the most important findings there; do not claim text was inserted into the document."""


def compose_deep_research_mode_instructions(tool_names: list[str] | tuple[str, ...]) -> str:
    """Base deep-research prompt plus Writer insert steps only when apply_document_content is available."""
    parts = [DEEP_RESEARCH_SUB_AGENT_INSTRUCTIONS.strip()]
    if "apply_document_content" in tool_names:
        parts.append(DEEP_RESEARCH_WRITER_INSERT_INSTRUCTIONS.strip())
    else:
        parts.append(DEEP_RESEARCH_REPLY_ONLY_INSTRUCTIONS.strip())
    return "\n\n".join(parts)

class DeepResearchWebTool(SubAgentResearchWebBase):
    """Multi-step public web research (sidebar Deep Research only; not shallow web_research)."""

    specialized_domain: ClassVar[str | None] = "deep_research"
    specialized_cross_cutting: ClassVar[bool] = True
    required_core_tools: ClassVar[frozenset[str] | None] = frozenset([
        "get_document_content",
        "get_document_tree",
        "search_in_document",
        "apply_document_content",
    ])
    doc_types: list[str] | None = ["writer", "calc", "draw", "impress"]
    name: str | None = "deep_research_web"
    description: str = (
        "Run breadth/depth public web research on a topic. Returns plain text. "
        "Summarize findings in reply_to_user; insert formatted HTML with apply_document_content "
        "only when that tool is in your tool list."
    )
    deep_research: ClassVar[bool] = True


def _run_deep_research_agent(ctx: ToolContext, *, query: str = "", history_text: str | None = None) -> dict[str, Any]:
    """Run one turn of the Deep Research smol sub-agent."""
    from plugin.chatbot.smol_agent import run_smol_side_turn

    tools = collect_sub_agent_tools(ctx, "deep_research", include_document_research=False)
    tool_names = [t.name for t in tools if t.name]
    mode_text = compose_deep_research_mode_instructions(tool_names)
    instructions = get_sub_agent_instructions(mode_text, tool_names=tool_names)
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


class DeepResearchSessionTool(SubAgentSessionToolBase):
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
