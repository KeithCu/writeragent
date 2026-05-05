# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# Shared text for web research in the chat response (multi-line) — same
# formatting for approval and non-approval paths.

from __future__ import annotations


def search_engine_preview_line(query_for_engine: str) -> str:
    """Sentence used for the search-engine step (DDG query), approval and info."""
    from plugin.framework.i18n import _

    return _("This search query '%s' will be sent to the search engine.") % (query_for_engine or "",)


def web_search_engine_step_chat_text(query_for_engine: str, step_index: int, *, approval_required: bool) -> str:
    """Chat text for each internal ``web_search`` step: full block on first step, compact on later steps."""
    from plugin.framework.i18n import _

    if step_index <= 0:
        header = _("[Web search — approval required]") if approval_required else _("[Web search]")
        block = "\n" + header + "\n"
        block += _("Tool: %s") % "web_search" + "\n"
        block += search_engine_preview_line(query_for_engine) + "\n\n"
        return block

    block = "\n" + _("[Additional web search]") + "\n"
    block += _("Tool: %s") % "web_search" + "\n"
    block += search_engine_preview_line(query_for_engine) + "\n\n"
    return block


def web_research_engine_chat_block(query_for_engine: str, *, approval_required: bool) -> str:
    """Block shown when the sub-agent is about to send a query to the search engine (first step)."""
    return web_search_engine_step_chat_text(query_for_engine, 0, approval_required=approval_required)


def web_research_outer_chat_block(outer_query: str, history_text: str | None = None) -> str:
    """Format the main model's ``web_research`` arguments (research request + optional history).

    Chat UI no longer prepends this automatically; the response area shows internal
    ``web_search`` step text from the sub-agent instead. Kept for callers that need
    the same wording (e.g. tests, logging).
    """
    from plugin.framework.i18n import _

    block = "\n" + _("[Web research]") + "\n"
    block += _("Research request:") + "\n%s\n" % (outer_query or "").strip()
    if history_text and str(history_text).strip():
        hist = str(history_text).strip()
        if len(hist) > 8000:
            hist = hist[:8000] + "\n…"
        block += "\n" + _("Context for the research agent:") + "\n%s\n" % hist
    block += "\n"
    return block
