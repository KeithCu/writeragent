# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""``find_tools`` -- MCP discovery meta-tool for the ``direct_discovery`` exposure mode.

The MCP server advertises a small core tool set; the ~100+ specialized tools are
callable by name but hidden from ``tools/list`` to keep it short. ``find_tools`` lets a
client search that hidden catalog by free-text query and/or specialized domain and get
back ready-to-call MCP schemas -- so any MCP client (Claude, Codex, generic) can reach
the full tool set without the delegate sub-agent (no LLM backend) and without bloating
context.

Ranking is pure-Python lexical (BM25-lite + substring bonus): no venv, no numpy, no
embeddings, so the ranker itself can't fail when those aren't ready. find_tools is
document-optional (``requires_document = False``) -- discovery works whether or not a
document is open. A semantic backend is a clean later upgrade behind the embeddings
venv -- ``_rank``'s signature stays stable.

Only advertised in ``tools/list`` when ``mcp.tool_exposure_mode == "direct_discovery"``
(gated by name in ``plugin/mcp/mcp_protocol.py``); ``tier="mcp"`` keeps it off the chat
agent's tool list.
"""
from __future__ import annotations

import math
import re
import unicodedata
from typing import Any

from plugin.framework.tool import ToolBase, ToolContext

_DEFAULT_LIMIT = 8
_MAX_LIMIT = 50
_FINISH_TOOL = "specialized_workflow_finished"
# In direct_discovery mode the delegate gateway is not the intended route, so keep
# its (core-tier) gateway tools out of discovery results.
_GATEWAY_PREFIX = "delegate_to_specialized_"
_TOKEN_RE = re.compile(r"[a-z0-9]+")
# Dropped from query token scoring (kept for the raw-substring bonus) so common words
# like "a"/"the" don't surface unrelated tools.
_STOPWORDS = frozenset({
    "a", "an", "the", "to", "from", "of", "and", "or", "for", "with", "in", "on",
    "is", "it", "this", "that", "my", "your", "into", "as", "at", "by",
})


def _tokenize(text: Any) -> list[str]:
    # Strip accents so e.g. "rodapé"/"gráfico" tokenize like their unaccented forms --
    # a small help for non-English queries. Full cross-language matching needs the
    # semantic ranker / per-domain synonyms (a documented later upgrade).
    s = unicodedata.normalize("NFKD", str(text or "").lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return _TOKEN_RE.findall(s)


def _doc_tokens(schema: dict) -> list[str]:
    """Searchable token bag for a tool schema: name tokens weighted x3, description x1."""
    name_toks = _tokenize(str(schema.get("name") or "").replace("_", " "))
    body_toks = _tokenize(schema.get("description") or "")
    return name_toks * 3 + body_toks


def _doc_filter(doc: Any) -> dict:
    """Registry kwargs for the active document.

    With no open document the registry filters out every app-specific tool (by
    ``uno_services``), which would leave discovery almost empty -- so pass
    ``filter_doc_type=False`` to list the whole catalog instead.
    """
    return {} if doc is not None else {"filter_doc_type": False}


def _rank(schemas: list[dict], query: str | None, limit: int) -> list[dict]:
    """Rank tool schemas against a free-text query (BM25-lite + substring bonus).

    Deterministic and dependency-free. Empty query -> registry order, truncated to
    ``limit``. With a query, schemas with no token overlap and no raw-substring hit
    score 0 and are dropped. Stable secondary sort by name.
    """
    q = (query or "").strip()
    if not q:
        return schemas[:limit]

    q_all = set(_tokenize(q))
    q_tokens = (q_all - _STOPWORDS) or q_all  # if the query is all stopwords, keep them
    raw_q = q.lower()
    docs = [(s, _doc_tokens(s)) for s in schemas]
    n = len(docs)
    if not n:
        return []
    avgdl = sum(len(d) for _, d in docs) / n or 0.0
    k1, b = 1.2, 0.75
    df = {qt: sum(1 for _, d in docs if qt in d) for qt in q_tokens}

    scored: list[tuple[float, str, dict]] = []
    for schema, d in docs:
        dl = len(d)
        score = 0.0
        for qt in q_tokens:
            f = d.count(qt)
            if not f:
                continue
            n_q = df.get(qt) or 1
            idf = math.log(1 + (n - n_q + 0.5) / (n_q + 0.5))
            denom = f + k1 * (1 - b + b * (dl / avgdl if avgdl else 0.0))
            score += idf * (f * (k1 + 1)) / denom
        name = str(schema.get("name") or "")
        if raw_q in name.lower():
            score += 2.0
        elif raw_q in str(schema.get("description") or "").lower():
            score += 0.5
        if score > 0:
            scored.append((score, name, schema))

    scored.sort(key=lambda x: (-x[0], x[1]))
    return [s for _, _, s in scored[:limit]]


def _agent_label_for_doc_type(doc_type: str | None) -> str:
    return {"calc": "Calc", "draw": "Draw", "impress": "Draw",
            "writer": "Writer"}.get((doc_type or "").lower(), "Writer")


def get_domain_guidance(domain: str, *, agent_label: str = "Writer", ctx: Any = None) -> str:
    """Static per-domain usage guidance for direct callers (mirrors the delegate's hints).

    Returns ``""`` for live-context domains (shapes canvas / spreadsheet snapshot /
    open-docs) that require a running document and for unknown domains.
    """
    if domain == "footnotes":
        return ("For footnotes_insert: if the task quotes or names the document anchor "
                "(e.g. a sentence), pass that exact string as insert_after_text so the "
                "note is placed after that text.")
    if domain == "charts":
        if agent_label == "Calc":
            return "When creating a chart in Calc, you MUST specify the data range explicitly (e.g. data_range='A1:B10')."
        if agent_label is None:
            # App unknown (no document open): cover both so the advice isn't Writer-biased.
            return ("For charts: in Calc you MUST pass an explicit data range (e.g. "
                    "data_range='A1:B10'); in Writer or Draw/Impress you MUST pass both the "
                    "`headers` and `rows` parameters.")
        return ("When creating or editing a chart in Writer or Draw/Impress, you MUST "
                "specify both the `headers` and `rows` parameters.")
    if domain == "images":
        return ("Discover local image files with list_nearby_image_files before insert_image "
                "when the user refers to a photo in the folder.")
    if domain == "analysis":
        return ("For stats, cleaning, regression, clustering, or simulation on tabular data "
                "use analyze_data; for charts use plot_data (or auto_plot=true); for live "
                "single-cell what-if use calc_goal_seek; for constrained optimization use "
                "calc_solver. Always pass a data_range (A1 address) for bulk data.")
    if domain == "python":
        if agent_label is None:
            # App unknown (no document open): cover both so the advice isn't Writer-biased.
            return ("run_venv_python_script: in Calc, pass `data_range` (an A1 address) to inject "
                    "cell values as `data`; in Writer or Draw/Impress it does not inject "
                    "spreadsheet data -- use document tools for content.")
        try:
            from plugin.framework.constants import python_specialized_sub_agent_hint
            return (python_specialized_sub_agent_hint(agent_label) or "").strip()
        except Exception:
            return ""
    if domain == "document_research":
        try:
            from plugin.doc.document_research import get_document_research_workflow_hint
            return (get_document_research_workflow_hint(getattr(ctx, "ctx", None)) or "").strip()
        except Exception:
            return ""
    return ""


def sidebar_only_tool_names(registry: Any, doc: Any) -> frozenset:
    """Tool names in Writer sidebar-only domains (e.g. brainstorming, writing_plan).

    Those flows depend on bespoke session orchestration / finish tools that the direct
    MCP modes don't provide, so the delegate keeps them off MCP (``tool.py`` /
    ``WRITER_SIDEBAR_ONLY_DOMAINS``). The direct modes -- ``find_tools`` and
    ``direct_flat``'s ``tools/list`` -- must exclude them the same way.
    """
    try:
        from plugin.framework.constants import WRITER_SIDEBAR_ONLY_DOMAINS
        tools = registry.get_tools(doc=doc, exclude_tiers=(), **_doc_filter(doc))
    except Exception:
        return frozenset()
    out = set()
    for t in tools:
        if getattr(t, "specialized_domain", None) in WRITER_SIDEBAR_ONLY_DOMAINS:
            name = getattr(t, "name", None)
            if isinstance(name, str):
                out.add(name)
    return frozenset(out)


class FindTools(ToolBase):
    """Discover registry tools by free-text query and/or specialized domain.

    Returns ready-to-call MCP schemas for tools that are callable by name but hidden
    from the default ``tools/list``. Pure-Python lexical ranking (no venv, no embeddings)
    and document-optional, so discovery works even with no document open.
    """

    name = "find_tools"
    description = (
        "Discover additional tools that are available but not listed here. This MCP "
        "server exposes a small core tool set; many specialized capabilities (e.g. "
        "footnotes, charts, images, data analysis, document research) are callable by "
        "name but hidden from the default list to keep it short. Call find_tools with a "
        "natural-language `query` describing what you want to do (e.g. \"insert a "
        "footnote\", \"make a bar chart from a range\") to get the matching tools and "
        "their full input schemas, ready to call directly. Pass a `domain` to list every "
        "tool in one area. Call with no arguments to see the available domains and a "
        "sample of tools. Always prefer calling a tool returned by find_tools over giving "
        "up because a capability seems missing."
    )
    tier = "mcp"
    is_mutation = False
    requires_document = False  # discovery needs no open document
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": ("Natural-language description of the task you want to accomplish "
                                "(e.g. 'insert a footnote', 'create a chart from a cell range'). "
                                "Tools are ranked by relevance to it. Optional."),
            },
            "domain": {
                "type": "string",
                "description": ("Optional specialized area to scope results to (e.g. 'footnotes', "
                                "'charts', 'images', 'analysis', 'document_research'). Call "
                                "find_tools with no arguments to see the available domains."),
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of tools to return (default 8).",
                "minimum": 1,
                "maximum": _MAX_LIMIT,
            },
        },
        "required": [],
    }

    def execute(self, ctx: ToolContext, query: str | None = None,
                domain: str | None = None, limit: int | None = None,
                **kwargs: Any) -> dict[str, Any]:
        registry = ctx.services.get("tools") if ctx.services else None
        if registry is None:
            return self._tool_error("Tool registry unavailable.", code="SERVICE_UNAVAILABLE")

        # Normalize client-supplied inputs (an MCP client can send anything).
        query = query if isinstance(query, str) else None
        domain = domain.strip().lower() if isinstance(domain, str) and domain.strip() else None
        # A domain listing promises "every tool in one area", so with no explicit limit
        # default it to the max (a domain rarely has more); a free-text query keeps the
        # small default to stay token-cheap.
        default_n = _MAX_LIMIT if domain else _DEFAULT_LIMIT
        try:
            top_n = int(limit) if limit else default_n
        except (TypeError, ValueError, OverflowError):
            top_n = default_n
        top_n = max(1, min(top_n, _MAX_LIMIT))

        doc = getattr(ctx, "doc", None)
        domains = self._available_domains(registry, doc)

        from plugin.framework.constants import WRITER_SIDEBAR_ONLY_DOMAINS
        if domain and domain in WRITER_SIDEBAR_ONLY_DOMAINS:
            # Sidebar-only flows aren't reachable over MCP (they need bespoke session
            # orchestration), so don't even surface their required-core tools.
            schemas = []
        elif domain:
            # active_domain narrowing: the domain's specialized tools + their required
            # core tools (the registry bypasses tier exclusion when active_domain is set).
            schemas = registry.get_schemas("mcp", doc=doc, active_domain=domain, **_doc_filter(doc))
        else:
            # global search: surface the hidden specialized tools only -- exclude core
            # (already in tools/list, so re-listing it just pollutes discovery), the mcp
            # tier (so the discovery tools don't return themselves), and workflow-control.
            schemas = registry.get_schemas(
                "mcp", doc=doc, exclude_tiers=frozenset({"mcp", "specialized_control", "core"}),
                **_doc_filter(doc))

        # Drop the workflow finish tool (re-added by the domain narrowing), the delegate
        # gateway tools, and Writer sidebar-only flows (which need bespoke orchestration
        # the direct modes don't provide); guard against malformed (non-dict) schemas.
        sidebar_only = sidebar_only_tool_names(registry, doc)
        candidates = []
        for s in (schemas or []):
            if not isinstance(s, dict):
                continue
            name = s.get("name")
            # Drop schemas without a usable, callable name: a malformed schema can carry a
            # non-string or even unhashable name (e.g. a list). Returning it would both
            # break the `in sidebar_only` membership test and hand back an uncallable tool.
            if not isinstance(name, str) or not name:
                continue
            if name == _FINISH_TOOL or name.startswith(_GATEWAY_PREFIX) or name in sidebar_only:
                continue
            candidates.append(s)
        ranked = _rank(candidates, query, top_n)

        result: dict[str, Any] = {
            "status": "ok",
            "query": query,
            "domain": domain,
            "available_domains": domains,
            "tools": ranked,
        }
        if doc is None:
            result["note"] = ("No document is open, so results span all document types; "
                              "open the matching document before calling an app-specific tool.")
        # Per-domain usage guidance (the load-bearing hints the delegate injects).
        # Always a dict {domain: text}: for an explicit `domain` it's that one; for a
        # free-text `query` we infer the domains of the returned tools so the caller
        # still gets the hints (e.g. "charts need a data_range") it would otherwise
        # lose vs the delegate path.
        # With no open document the target app is unknown, so use neutral (app-agnostic)
        # guidance instead of defaulting to Writer (which would give wrong chart advice).
        doc_type = getattr(ctx, "doc_type", None) if doc is not None else None
        label = _agent_label_for_doc_type(doc_type) if doc_type else None
        if domain:
            target_domains = [domain]
        else:
            name_to_domain = self._tool_domains(registry, doc)
            seen: set[str] = set()
            target_domains = []
            for s in ranked:
                d = name_to_domain.get(str(s.get("name") or ""))
                if d and d not in seen:
                    seen.add(d)
                    target_domains.append(d)
        guidance = {}
        for d in target_domains:
            g = get_domain_guidance(d, agent_label=label, ctx=ctx)
            if g:
                guidance[d] = g
        if guidance:
            result["domain_guidance"] = guidance
        return result

    @staticmethod
    def _available_domains(registry: Any, doc: Any) -> list[str]:
        """Distinct specialized domains reachable over MCP, sorted.

        Excludes Writer sidebar-only domains (they need bespoke session orchestration the
        direct modes can't provide), matching the delegate.
        """
        from plugin.framework.constants import WRITER_SIDEBAR_ONLY_DOMAINS
        try:
            tools = registry.get_tools(doc=doc, exclude_tiers=(), **_doc_filter(doc))
        except Exception:
            return []
        return sorted({
            d for t in tools
            if (d := getattr(t, "specialized_domain", None)) and d not in WRITER_SIDEBAR_ONLY_DOMAINS
        })

    @staticmethod
    def _tool_domains(registry: Any, doc: Any) -> dict[str, str]:
        """Map tool name -> its specialized domain, to infer guidance from query results."""
        try:
            tools = registry.get_tools(doc=doc, exclude_tiers=(), **_doc_filter(doc))
        except Exception:
            return {}
        out: dict[str, str] = {}
        for t in tools:
            d = getattr(t, "specialized_domain", None)
            name = getattr(t, "name", None)
            if d and isinstance(name, str):
                out[name] = str(d)
        return out
