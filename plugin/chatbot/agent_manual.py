# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Topic-based delivery of the shared behavioral pieces (MCP / external-agent channel).

The SOURCE OF TRUTH for the cross-cutting behavioral rules lives in plugin/framework/prompts.py:
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

Same pieces, different assembly per channel — update a rule in prompts.py and every consumer
sees it; only genuinely channel-specific text lives outside the shared pieces.

Sections are per document type: Writer has the full manual; Calc and Draw currently get the generic
sections (tools-not-chat, confirmation by structured fields, concurrency) until app-specific prose is
written. ``get_guidance`` resolves the target document's type the same way every other tool does, so
a Calc session never reads Writer advice."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from plugin.framework.errors import best_effort
from plugin.framework.prompts import (
    CONFIRM_EDITS_FROM_STRUCTURED_FIELDS,
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
# sidebar — which is why it lives here and not with the shared pieces in prompts.py.
# ---------------------------------------------------------------------------
_MCP_CONCURRENCY_RULES = (
    "CONCURRENCY / MULTI-DOCUMENT:\n"
    "- Fast tools are serialized (one at a time); under contention the server returns HTTP 429 "
    "'busy'. On 429, wait briefly and retry that same call.\n"
    "- Long-running calls can overlap EXCEPT two edits to the SAME document (those are serialized), "
    "so parallel work is fine across different documents but avoid overlapping edits to one document.\n"
    "- With more than one document open, ALWAYS pass document_url (a url or uid from "
    "list_open_documents): without it every call targets whatever window the user has focused, "
    "which can change between your calls. Every result echoes the document it acted on "
    "(document: {name, uid}) — check it when in doubt."
)

# ---------------------------------------------------------------------------
# Appended to the editing topic ONLY when it is served alone (get_section): in full_manual the
# editing-html section follows immediately, so a "go read that topic" pointer would dangle.
# ---------------------------------------------------------------------------
_EDITING_HTML_POINTER = (
    "\n\nFor apply_document_content's full contract (targets, the JSON array of HTML "
    "fragments, math, named styles, reach), read the editing-html topic."
)


@dataclass(frozen=True)
class _Topic:
    id: str
    text: str
    summary: str
    standalone_suffix: str = ""


_WRITER_TOPICS: tuple[_Topic, ...] = (
    _Topic(
        "editing",
        (
            "Change the document with tools, not chat.\n\n"
            + CONFIRM_EDITS_FROM_STRUCTURED_FIELDS.strip()
            + "\n\n"
            + TOOL_USAGE_PATTERNS.strip()
            + "\n\n"
            + TRANSLATION_RULES.strip()
        ),
        "edit workflow: confirm by structured fields, patterns, translation",
        standalone_suffix=_EDITING_HTML_POINTER,
    ),
    _Topic(
        "editing-html",
        WRITER_APPLY_DOCUMENT_HTML_RULES,
        "apply_document_content contract: targets, HTML array, math, styles, reach",
    ),
    _Topic(
        "review-modes",
        WRITER_REVIEW_MODES_RULES,
        "tracked changes off/record/wait; never resolve your own",
    ),
    _Topic(
        "search",
        WRITER_SEARCH_RULES,
        "find text anywhere (body, tables, boxes, shapes, headers, comments) + where it is",
    ),
    _Topic(
        "navigation",
        WRITER_NAVIGATION_RULES,
        "map-first reading of large documents",
    ),
    _Topic(
        "images",
        WRITER_IMAGES_RULES,
        "insert/replace/resize/crop, highlight passages on a picture + how a vision model sees images",
    ),
    _Topic(
        "concurrency",
        _MCP_CONCURRENCY_RULES,
        "same-document edits serialize; retry on HTTP 429; multi-document targeting",
    ),
)

_GENERIC_TOPICS: tuple[_Topic, ...] = (
    _Topic(
        "editing",
        GENERIC_EDIT_CONFIRMATION_RULES,
        "use the tools, confirm edits by structured fields, re-read before targeted edits",
    ),
    _Topic(
        "concurrency",
        _MCP_CONCURRENCY_RULES,
        "same-document edits serialize; retry on HTTP 429; multi-document targeting",
    ),
)

_MANUALS: dict[str, tuple[_Topic, ...]] = {
    "writer": _WRITER_TOPICS,
    "calc": _GENERIC_TOPICS,
    "draw": _GENERIC_TOPICS,
    "generic": _GENERIC_TOPICS,
}

MANUAL_SECTIONS: dict[str, str] = {topic.id: topic.text for topic in _WRITER_TOPICS}

# Common aliases -> canonical topic, grouped by canonical id.
_ALIASES_BY_TOPIC: dict[str, tuple[str, ...]] = {
    "editing": ("edit", "edits", "translation", "translate"),
    "editing-html": ("apply", "apply-document-content", "html", "html-rules", "styles", "math"),
    "review-modes": ("tracked-changes", "redlines", "review", "review-mode", "review-modes"),
    "search": ("find", "search-in-document"),
    "navigation": ("navigate", "outline", "large-documents"),
    "images": ("image", "crop", "vision", "highlight"),
    "concurrency": ("429", "rate-limit", "documents", "multi-document", "document-url"),
}

_ALIASES: dict[str, str] = {
    alias: canon
    for canon, aliases in _ALIASES_BY_TOPIC.items()
    for alias in aliases
}


def _app(doc_type: str | None) -> str:
    if doc_type is None:
        return "generic"
    label = str(doc_type).strip().lower()
    if label == "impress":
        return "draw"
    return label if label in _MANUALS else "writer"


def doc_type_of(doc: Any) -> str | None:
    """'writer' / 'calc' / 'draw' for a document model, or None when there is no document.

    Same resolution every other tool relies on (lazy import of is_calc / is_draw)."""
    if doc is None:
        return None

    def _classify() -> str:
        from plugin.doc.doc_type import is_calc, is_draw

        if is_calc(doc):
            return "calc"
        if is_draw(doc):
            return "draw"
        return "writer"

    return best_effort(_classify, "doc_type_of: could not classify document; defaulting to writer", "writer")


def list_topics(doc_type: str | None = "writer") -> list[str]:
    """Canonical topic ids in display order for a document type."""
    return [t.id for t in _MANUALS[_app(doc_type)]]


def normalize_topic(topic: str | None, doc_type: str | None = "writer") -> str | None:
    """Map a raw topic string (aliases/case/spacing) to a canonical topic of that app, else None."""
    if not topic:
        return None
    manual = _MANUALS[_app(doc_type)]
    valid_ids = {t.id for t in manual}
    key = topic.strip().lower().replace(" ", "-").replace("_", "-")
    if key in valid_ids:
        return key
    alias = _ALIASES.get(key)
    return alias if alias in valid_ids else None


def get_section(topic: str | None, doc_type: str | None = "writer") -> str | None:
    """The manual section for a topic (alias-aware) in that app's manual, or None if unknown."""
    app = _app(doc_type)
    canon = normalize_topic(topic, doc_type)
    if not canon:
        return None
    for t in _MANUALS[app]:
        if t.id == canon:
            return f"{t.text}{t.standalone_suffix}" if t.standalone_suffix else t.text
    return None


def manual_index(doc_type: str | None = "writer") -> str:
    """Short index for get_guidance() with no topic — what can be pulled WITHOUT reading it all.

    With doc_type=None (no document open) the index is neutral: it lists the always-available
    generic topics and says the full set follows the open document's type."""
    if doc_type is None:
        lines = [
            "WriterAgent how-to topics — call get_guidance(topic) to read one. No document is open; "
            "topics follow the open document's type (writer, calc, draw). Available now:"
        ]
        lines += [f"- {t.id}: {t.summary}" for t in _GENERIC_TOPICS]
        return "\n".join(lines)
    lines = ["WriterAgent how-to topics — call get_guidance(topic) to read one:"]
    lines += [f"- {t.id}: {t.summary}" for t in _MANUALS[_app(doc_type)]]
    return "\n".join(lines)


def full_manual(doc_type: str | None = "writer") -> str:
    """The whole manual for one document type, sections in display order.

    This is what the AGENT-BACKEND path injects into its system prompt (send_handlers) — it talks
    to the MCP HTTP server, so it takes the shared pieces AND the MCP extras in one string. The
    sidebar does not use this: its template assembles the shared pieces directly (constants)."""
    parts = [t.text for t in _MANUALS[_app(doc_type)]]
    return "HOW TO WORK WITH THE DOCUMENT:\n\n" + "\n\n".join(parts)


def full_manual_for_model(model: Any) -> str:
    """full_manual() resolved from a document model (agent-backend convenience)."""
    return full_manual(doc_type_of(model) or "writer")
