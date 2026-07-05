# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Topic-based delivery of the shared behavioral pieces (MCP / external-agent channel).

The SOURCE OF TRUTH for the cross-cutting behavioral rules lives in plugin/framework/constants.py:
the original chat system prompt pieces (TOOL_USAGE_PATTERNS, WRITER_APPLY_DOCUMENT_HTML_RULES,
TRANSLATION_RULES), updated in place and extended with new pieces (WRITER_REVIEW_MODES_RULES,
WRITER_SEARCH_RULES, WRITER_NAVIGATION_RULES, WRITER_IMAGES_RULES). The sidebar system prompt
template assembles those pieces directly, adding its sidebar-only parts (persona, chat format,
delegation routing, memory).

This module is the on-demand assembly of the same pieces: get_guidance(topic) serves ONE topic at
a time, mapping topic -> piece per document type and adding the MCP-only extras (the HTTP 429
concurrency contract, which the in-process sidebar never needs). Its consumers:
- external MCP clients (they drop/truncate connect-time instructions, so pull is their ONLY
  channel, plus JIT state in tool results);
- the sidebar agent, for the reference pieces (search/navigation/images) its HYBRID prompt keeps
  out of the ambient text — get_guidance is tier "core" so the sidebar can call it;
- the agent-backend path (send_handlers), which injects full_manual() — it talks to the HTTP
  server, so it takes the whole manual including the MCP extras.

Same pieces, different assembly per channel — update a rule in constants.py and every consumer
sees it; only genuinely channel-specific text lives outside the shared pieces.

Sections are per document type: Writer has the full manual; Calc and Draw currently get the generic
sections (tools-not-chat, confirmation by structured fields, concurrency) until app-specific prose is
written. ``get_guidance`` resolves the target document's type the same way every other tool does, so
a Calc session never reads Writer advice."""
from __future__ import annotations

from plugin.framework.constants import (
    GENERIC_EDIT_CONFIRMATION_RULES,
    TOOL_USAGE_PATTERNS,
    TRANSLATION_RULES,
    WRITER_APPLY_DOCUMENT_HTML_RULES,
    WRITER_IMAGES_RULES,
    WRITER_NAVIGATION_RULES,
    WRITER_REVIEW_MODES_RULES,
    WRITER_SEARCH_RULES,
)

# ---------------------------------------------------------------------------
# MCP-only pieces. The HTTP server's concurrency contract: real for every client of the MCP
# server (external clients and the agent-backend path alike), meaningless for the in-process
# sidebar — which is why it lives here and not with the shared pieces in constants.py.
# ---------------------------------------------------------------------------
_MCP_CONCURRENCY_RULES = (
    "CONCURRENCY / MULTI-DOCUMENT:\n"
    "- The server runs ONE operation at a time and returns HTTP 429 'busy' when overloaded.\n"
    "- On 429, wait briefly and retry that same call — do not fire many calls in parallel.\n"
    "- With more than one document open, ALWAYS pass document_url (a url or uid from "
    "list_open_documents): without it every call targets whatever window the user has focused, "
    "which can change between your calls. Every result echoes the document it acted on "
    "(document: {name, uid}) — check it when in doubt."
)

# ---------------------------------------------------------------------------
# Writer topics (topic -> shared piece). The agent pulls a section on demand (MCP) or gets them
# all concatenated (agent backend), so a topic can bundle the related pieces.
# ---------------------------------------------------------------------------
# Appended to the editing topic ONLY when it is served alone (get_section): in full_manual the
# editing-html section follows immediately, so a "go read that topic" pointer would dangle.
_EDITING_HTML_POINTER = (
    "\n\nFor apply_document_content's full contract (targets, the JSON array of HTML "
    "fragments, math, named styles, reach), read the editing-html topic."
)

MANUAL_SECTIONS: dict[str, str] = {
    # The one-line opener is this channel's counterpart of the sidebar-only SIDEBAR_VS_DOCUMENT
    # piece: same rule, phrased without the sidebar mechanics. The editing topic covers the
    # WORKFLOW (confirmation, patterns, translation); the apply_document_content contract is its
    # own subdivision (editing-html) so a model can pull just the part it needs.
    "editing": (
        "Change the document with tools, not chat.\n\n"
        + TOOL_USAGE_PATTERNS.strip()
        + "\n\n"
        + TRANSLATION_RULES.strip()
    ),
    "editing-html": WRITER_APPLY_DOCUMENT_HTML_RULES,
    "review-modes": WRITER_REVIEW_MODES_RULES,
    "search": WRITER_SEARCH_RULES,
    "navigation": WRITER_NAVIGATION_RULES,
    "images": WRITER_IMAGES_RULES,
    "concurrency": _MCP_CONCURRENCY_RULES,
}

# Stable display order + one-line summaries for the Writer index.
_TOPIC_SUMMARY: list[tuple[str, str]] = [
    ("editing", "edit workflow: confirm by structured fields, patterns, translation"),
    ("editing-html", "apply_document_content contract: targets, HTML array, math, styles, reach"),
    ("review-modes", "tracked changes off/record/wait; never resolve your own"),
    ("search", "find text anywhere (body, tables, boxes, shapes, headers, comments) + where it is"),
    ("navigation", "map-first reading of large documents"),
    ("images", "insert/replace/resize/crop + how a vision model sees images"),
    ("concurrency", "one operation at a time; retry on HTTP 429; multi-document targeting"),
]

# ---------------------------------------------------------------------------
# Generic sections for document types without app-specific prose yet (Calc, Draw). Honest minimum:
# only rules that genuinely apply everywhere. NO Writer advice leaks into a Calc/Draw session.
# ---------------------------------------------------------------------------
_GENERIC_SECTIONS: dict[str, str] = {
    # Same object the Calc/Draw sidebar prompts embed (constants) — single source there too.
    "editing": GENERIC_EDIT_CONFIRMATION_RULES,
    "concurrency": _MCP_CONCURRENCY_RULES,
}

_GENERIC_TOPIC_SUMMARY: list[tuple[str, str]] = [
    ("editing", "use the tools, confirm edits by structured fields, re-read before targeted edits"),
    ("concurrency", "one operation at a time; retry on HTTP 429; multi-document targeting"),
]

_SECTIONS_BY_APP: dict[str, dict[str, str]] = {
    "writer": MANUAL_SECTIONS,
    "calc": _GENERIC_SECTIONS,
    "draw": _GENERIC_SECTIONS,
    # No document open: only the always-true generic rules are safe to serve.
    "generic": _GENERIC_SECTIONS,
}

_SUMMARY_BY_APP: dict[str, list[tuple[str, str]]] = {
    "writer": _TOPIC_SUMMARY,
    "calc": _GENERIC_TOPIC_SUMMARY,
    "draw": _GENERIC_TOPIC_SUMMARY,
    "generic": _GENERIC_TOPIC_SUMMARY,
}

# Common aliases -> canonical topic.
_ALIASES = {
    "edit": "editing", "edits": "editing", "translation": "editing", "translate": "editing",
    "apply": "editing-html", "apply_document_content": "editing-html",
    "html": "editing-html", "html-rules": "editing-html", "styles": "editing-html", "math": "editing-html",
    "tracked-changes": "review-modes", "tracked_changes": "review-modes", "redlines": "review-modes",
    "review": "review-modes", "review_modes": "review-modes", "review-mode": "review-modes",
    "find": "search", "search_in_document": "search",
    "navigate": "navigation", "outline": "navigation", "large-documents": "navigation",
    "image": "images", "crop": "images", "vision": "images",
    "429": "concurrency", "rate-limit": "concurrency", "rate_limit": "concurrency",
    "documents": "concurrency", "multi-document": "concurrency", "document-url": "concurrency",
    "document_url": "concurrency",
}


def _app(doc_type: str | None) -> str:
    if doc_type is None:
        return "generic"  # no document open -> only the generic rules apply
    return doc_type if doc_type in _SECTIONS_BY_APP else "writer"


def doc_type_of(doc) -> str | None:
    """'writer' / 'calc' / 'draw' for a document model, or None when there is no document.

    Same resolution every other tool relies on (lazy import mirrors get_core_directives)."""
    if doc is None:
        return None
    try:
        from plugin.doc.document_helpers import is_calc, is_draw

        if is_calc(doc):
            return "calc"
        if is_draw(doc):
            return "draw"
    except Exception:
        pass
    return "writer"


def list_topics(doc_type: str | None = "writer") -> list[str]:
    """Canonical topic ids in display order for a document type."""
    return [t for t, _ in _SUMMARY_BY_APP[_app(doc_type)]]


def normalize_topic(topic: str | None, doc_type: str | None = "writer") -> str | None:
    """Map a raw topic string (aliases/case/spacing) to a canonical topic of that app, else None."""
    if not topic:
        return None
    sections = _SECTIONS_BY_APP[_app(doc_type)]
    key = topic.strip().lower().replace(" ", "-").replace("_", "-")
    if key in sections:
        return key
    alias = _ALIASES.get(key) or _ALIASES.get(key.replace("-", "_"))
    return alias if alias in sections else None


def get_section(topic: str | None, doc_type: str | None = "writer") -> str | None:
    """The manual section for a topic (alias-aware) in that app's manual, or None if unknown."""
    app = _app(doc_type)
    canon = normalize_topic(topic, doc_type)
    text = _SECTIONS_BY_APP[app].get(canon) if canon else None
    if text is not None and canon == "editing" and app == "writer":
        text += _EDITING_HTML_POINTER  # served alone -> point to the subdivision
    return text


def manual_index(doc_type: str | None = "writer") -> str:
    """Short index for get_guidance() with no topic — what can be pulled WITHOUT reading it all.

    With doc_type=None (no document open) the index is neutral: it lists the always-available
    generic topics and says the full set follows the open document's type."""
    if doc_type is None:
        lines = [
            "WriterAgent how-to topics — call get_guidance(topic) to read one. No document is open; "
            "topics follow the open document's type (writer, calc, draw). Available now:"
        ]
        lines += ["- %s: %s" % (t, s) for t, s in _GENERIC_TOPIC_SUMMARY]
        return "\n".join(lines)
    lines = ["WriterAgent how-to topics — call get_guidance(topic) to read one:"]
    lines += ["- %s: %s" % (t, s) for t, s in _SUMMARY_BY_APP[_app(doc_type)]]
    return "\n".join(lines)


def full_manual(doc_type: str | None = "writer") -> str:
    """The whole manual for one document type, sections in display order.

    This is what the AGENT-BACKEND path injects into its system prompt (send_handlers) — it talks
    to the MCP HTTP server, so it takes the shared pieces AND the MCP extras in one string. The
    sidebar does not use this: its template assembles the shared pieces directly (constants)."""
    app = _app(doc_type)
    sections = _SECTIONS_BY_APP[app]
    parts = [sections[t] for t, _ in _SUMMARY_BY_APP[app]]
    return "HOW TO WORK WITH THE DOCUMENT:\n\n" + "\n\n".join(parts)


def full_manual_for_model(model) -> str:
    """full_manual() resolved from a document model (agent-backend convenience)."""
    return full_manual(doc_type_of(model) or "writer")
