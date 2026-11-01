# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Writing Plan sub-agent: multi-turn plan-driven document writing via specialized delegate."""

from __future__ import annotations

from typing import Any, Callable, ClassVar, TYPE_CHECKING

from plugin.chatbot.sub_agent_tools import (
    HtmlContentToolBase,
    SUB_AGENT_DOC_CORE_TOOLS,
    SubAgentResearchWebBase,
    SubAgentSessionToolBase,
    _session_tool_parameters,
    run_domain_sub_agent,
)
from plugin.writer.specialized_base import ToolWriterSpecialBase

if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext

WRITING_SUB_AGENT_INSTRUCTIONS = """WRITING PLAN MODE:
You help write documents collaboratively using a structured, plan-driven approach.

WORKFLOW (in order):
1. Explore context: read the active document (get_document_content / get_document_tree) or design spec to understand the user's goal, and search the public web using `writing_research_web` if needed to collect details.
2. Propose a structured Writing Plan/Outline - ONE outline of sections/headings as HTML. Ask the user if they want to modify the outline.
3. Keep the outline in the conversation history as a roadmap. Do NOT write the full outline/headings list to the document at the start (as headings will be written with section content and would appear twice).
4. Implement sections one-by-one:
   - Generate high-quality content for a single section as HTML (including its heading).
   - Insert it into the document using `write_document_section`.
   - Ask the user for approval or feedback on the written section before moving to the next section.
5. Once all sections are written, call reply_to_user with a handoff answer and writing_plan_finished=true.

HTML RULES (CRITICAL):
- All reply_to_user answer text must be HTML.
- write_document_section content must be a JSON array of HTML strings — no Markdown (#, **, ```).
- Do NOT use HTML entity escaping (&lt;p&gt;) — send real tags.

COMPLETION TOOLS:
- reply_to_user: continue the writing plan conversation (questions, section drafts, summaries).
- reply_to_user with writing_plan_finished=true: END the session after all sections are completed and reviewed.
- write_document_section: write content for a section to the document.
- writing_research_web: search the public web for context or information."""


class WritingResearchWeb(ToolWriterSpecialBase, SubAgentResearchWebBase):
    """Web research for writing plans (public topics); returns plain text for the sub-agent to format as HTML."""

    specialized_domain: ClassVar[str | None] = "writing_plan"
    required_core_tools: ClassVar[frozenset[str] | None] = SUB_AGENT_DOC_CORE_TOOLS
    name: str | None = "writing_research_web"
    description: str = "Search the public web for context during document writing. Reformats findings as HTML in reply_to_user."


class WriteDocumentSection(HtmlContentToolBase):
    """Write a specific section of the document."""

    specialized_domain: ClassVar[str | None] = "writing_plan"
    required_core_tools: ClassVar[frozenset[str] | None] = SUB_AGENT_DOC_CORE_TOOLS
    name: str | None = "write_document_section"
    description: str = "Insert or replace a section of document content with formatted HTML."


def _run_writing_agent(ctx: ToolContext, *, query: str = "", history_text: str | None = None, topic: str | None = None) -> dict[str, Any]:
    """Run one turn of the writing plan smol sub-agent."""
    from plugin.chatbot.sticky_reply import WRITING_PLAN_REPLY_SPEC

    return run_domain_sub_agent(
        ctx,
        domain="writing_plan",
        base_instructions=WRITING_SUB_AGENT_INSTRUCTIONS,
        reply_spec=WRITING_PLAN_REPLY_SPEC,
        topic_header="WRITING TASK / CONTEXT",
        status_message="Writing...",
        stop_message="Writing stopped by user.",
        error_prefix="Writing failed",
        query=query,
        history_text=history_text,
        topic=topic,
    )


class WritingPlanSessionTool(SubAgentSessionToolBase):
    """Orchestrator for one turn of the writing plan sub-agent (sidebar session)."""

    name: str | None = "writing_plan_session"
    description: str = "Writing Plan document-generation sub-agent."
    agent_label: str = "Writing plan"
    runner: Callable[..., dict[str, Any]] | None = staticmethod(_run_writing_agent)
    doc_types: list[str] | None = ["writer"]
    parameters: dict[str, Any] | None = _session_tool_parameters(
        "Original context/topic for writing task."
    )
