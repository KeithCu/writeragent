# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# Shared text for research steps in the chat response (web search-engine + document open status).

from __future__ import annotations

import os
import posixpath
from typing import Any, Mapping

from plugin.framework.html_stripper import strip_html_tags


def _message_text(content: Any) -> str:
    """Normalize user/assistant message content to plain text."""
    if content is None:
        return ""
    if isinstance(content, list):
        bits = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                bits.append(str(part.get("text", "")))
        return strip_html_tags("\n".join(bits))
    return strip_html_tags(str(content))


def format_sub_agent_conversation_history(session: Any, *, current_query: str | None = None) -> str:
    """Build CONVERSATION HISTORY text for web-research / librarian sub-agents from ChatSession."""
    messages = getattr(session, "messages", None) or []
    parts: list[str] = []
    for i, msg in enumerate(messages):
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "")
        if role in ("system", "tool"):
            continue
        content = _message_text(msg.get("content"))
        if role == "user":
            if current_query is not None and i == len(messages) - 1 and content == current_query:
                continue
            if not content.strip():
                continue
            parts.append("User: %s" % content)
        elif role == "assistant":
            if not content.strip() and msg.get("tool_calls"):
                content = "[Thinking...]"
            if not content.strip():
                continue
            parts.append(content)
    return "\n\n".join(parts)


def search_engine_preview_line(query_for_engine: str) -> str:
    """Sentence used for the search-engine step (DDG query), approval and info."""
    from plugin.framework.i18n import _

    return _("This search query '%s' will be sent to the search engine.") % (query_for_engine or "",)


def web_search_engine_step_chat_text(query_for_engine: str, step_index: int) -> str:
    """Chat history for each internal web_search step (Tool: web_search + search-engine preview).

    Appended from WebResearchTool.tool_call_handler. When prompt_for_web_research is on, the
    preview is shown before Accept/Change/Reject (reject leaves that line in chat). Approval UI:
    panel.begin_inline_web_approval.
    """
    from plugin.framework.i18n import _

    del step_index  # format does not vary by step index
    block = "\n" + _("Tool: %s") % "web_search" + "\n"
    block += search_engine_preview_line(query_for_engine) + "\n\n"
    return block


def web_research_engine_chat_block(query_for_engine: str, *, approval_required: bool = False) -> str:
    """Same as web_search_engine_step_chat_text for step 0 (approval_required is legacy, ignored)."""
    del approval_required
    return web_search_engine_step_chat_text(query_for_engine, 0)


def format_research_cache_result_chat(result_data: Mapping[str, Any]) -> str:
    """Sidebar block from web_research result payload (delegate or direct tool)."""
    cache_key = result_data.get("research_cache_key")
    if not cache_key:
        return ""
    return web_research_cache_chat_text(result_data)


def web_research_cache_chat_text(fields: Mapping[str, Any]) -> str:
    """Sidebar notice when a web research report is served from or written to the research cache."""
    from plugin.framework.i18n import _

    event = str(fields.get("research_cache_event") or "saved")
    cache_key = str(fields.get("research_cache_key") or "")

    if event == "hit_fuzzy":
        jaccard = fields.get("research_cache_jaccard")
        pct = int(round(float(jaccard) * 100)) if jaccard is not None else 0
        lang = fields.get("research_cache_lang") or ""
        matched = fields.get("research_cache_matched_key") or ""
        lang_bit = f"{lang}: " if lang else ""
        block = "\n" + _("Research cache hit (fuzzy, %(pct)s%% match: %(lang_bit)s%(cache_key)s → %(matched)s)") % {
            "pct": pct,
            "lang_bit": lang_bit,
            "cache_key": cache_key,
            "matched": matched,
        } + "\n"
    elif event == "hit_embedding":
        similarity = fields.get("research_cache_similarity")
        pct = int(round(float(similarity) * 100)) if similarity is not None else 0
        lang = fields.get("research_cache_lang") or ""
        matched = fields.get("research_cache_matched_key") or ""
        lang_bit = f"{lang}: " if lang else ""
        block = "\n" + _("Research cache hit (embedding, %(pct)s%% match: %(lang_bit)s%(cache_key)s → %(matched)s)") % {
            "pct": pct,
            "lang_bit": lang_bit,
            "cache_key": cache_key,
            "matched": matched,
        } + "\n"
    elif event == "hit":
        block = "\n" + _("Research cache hit (key: %s)") % (cache_key,) + "\n"
    else:
        block = "\n" + _("Research cache saved (key: %s)") % (cache_key,) + "\n"
    block += "\n"
    return block


def display_name_for_path_or_name(path_or_name: str) -> str:
    """Basename for absolute paths; otherwise the string as given (basename, filter, URL fragment)."""
    raw = (path_or_name or "").strip()
    if not raw:
        return ""
    if raw.startswith("/"):
        return posixpath.basename(raw) or raw
    if os.path.isabs(raw):
        return os.path.basename(raw) or raw
    return raw


def document_open_preview_line(path_or_name: str) -> str:
    """Sentence shown before a read-only sibling document open."""
    from plugin.framework.i18n import _

    label = display_name_for_path_or_name(path_or_name)
    return _("Opening '%s' for read-only access.") % (label,)


def document_open_step_chat_text(path_or_name: str, step_index: int) -> str:
    """Chat text for each delegate_read_document step (tool name + open preview only)."""
    from plugin.framework.i18n import _

    del step_index  # callers pass index; format is the same for every step
    block = "\n" + _("Tool: %s") % "delegate_read_document" + "\n"
    block += document_open_preview_line(path_or_name) + "\n\n"
    return block
