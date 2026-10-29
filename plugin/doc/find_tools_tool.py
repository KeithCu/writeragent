# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""``find_tools`` -- MCP discovery meta-tool for the ``direct_discovery`` exposure mode.

The MCP server advertises a small core tool set; the ~100+ specialized tools are
callable by name but hidden from ``tools/list`` to keep it short. ``find_tools`` exposes
the same full specialized **domain catalog** the sidebar/delegate gateway uses, then
returns ready-to-call MCP schemas for a chosen ``domain`` -- so MCP-only hosts (no
WriterAgent LLM endpoint configured) can reach the full tool set without bloating
``tools/list``.

Workflow: ``find_tools()`` → pick a domain from ``available_domains`` →
``find_tools(domain=…)`` → ``tools/call`` by name.

Direct modes intentionally skip the delegate sub-agent; the host model orchestrates
specialized tools itself. Future enhancement: optional live context injection (Calc
snapshot, shapes canvas, open-docs list) like ``specialized_base.py`` for delegated runs.

Only advertised in ``tools/list`` when ``mcp.tool_exposure_mode == "direct_discovery"``
(gated by name in ``plugin/mcp/mcp_protocol.py``); ``tier="mcp"`` keeps it off the chat
agent's tool list.
"""
from __future__ import annotations

from typing import Any

from plugin.doc.doc_type import agent_label_for_doc_type
from plugin.framework.prompts import (
    IMPRESS_DRAW_SIDEBAR_ONLY_DOMAINS,
    WRITER_SIDEBAR_ONLY_DOMAINS,
    get_specialized_domain_catalog,
)
from plugin.framework.tool import ToolBase, ToolContext

_FINISH_TOOL = "specialized_workflow_finished"
_SIDEBAR_ONLY_DOMAINS = WRITER_SIDEBAR_ONLY_DOMAINS | IMPRESS_DRAW_SIDEBAR_ONLY_DOMAINS


def _doc_filter(doc: Any) -> dict[str, Any]:
    """Registry kwargs for the active document.

    With no open document the registry filters out every app-specific tool (by
    ``uno_services``), which would leave discovery almost empty -- so pass
    ``filter_doc_type=False`` to list the whole catalog instead.
    """
    return {} if doc is not None else {"filter_doc_type": False}


def get_domain_guidance(domain: str, *, agent_label: str | None = "Writer") -> str:
    """Extra per-domain usage hints for direct callers (beyond catalog descriptions).

    Returns ``""`` when there is no supplementary hint beyond ``specialized_domain_description``.
    """
    if domain == "footnotes":
        return ("For footnotes_insert: if the task quotes or names the document anchor "
                "(e.g. a sentence), pass that exact string as insert_after so the "
                "note is placed after that text.")
    if domain == "charts":
        if agent_label == "Calc":
            return "When creating a chart in Calc, you MUST specify the data range explicitly (e.g. data_range='A1:B10')."
        if agent_label is None:
            return ("For charts: in Calc you MUST pass an explicit data range (e.g. "
                    "data_range='A1:B10'); in Writer or Draw/Impress you MUST pass both the "
                    "`headers` and `rows` parameters.")
        return ("When creating or editing a chart in Writer or Draw/Impress, you MUST "
                "specify both the `headers` and `rows` parameters.")
    if domain == "images":
        from plugin.framework.prompts import images_specialized_sub_agent_hint

        return images_specialized_sub_agent_hint().strip()
    if domain == "python":
        if agent_label is None:
            return ("run_venv_python_script: in Calc, pass `data_range` (an A1 address) to inject "
                    "cell values as `data`; in Writer or Draw/Impress it does not inject "
                    "spreadsheet data -- use document tools for content.")
        from plugin.framework.prompts import python_specialized_sub_agent_hint
        return (python_specialized_sub_agent_hint(agent_label) or "").strip()
    if domain == "document_research":
        # MCP never advertises peer tools, so leave the peer-choice block out.
        from plugin.framework.prompts import get_document_research_workflow_hint
        return (get_document_research_workflow_hint() or "").strip()
    return ""


def sidebar_only_tool_names(
    registry: Any,
    doc: Any,
    *,
    doc_type: str | None = None,
    uno_services_supported: frozenset[str] | None = None,
) -> frozenset[str]:
    """Tool names in sidebar-only domains (MCP ``direct_flat`` only; ``find_tools`` uses the domain filter).

    The live document is not forwarded to the registry. With a document open,
    callers must pass ``doc_type`` and ``uno_services_supported`` (as
    ``direct_flat`` does). Otherwise ``get_tools`` rejects every tool that
    declares ``uno_services``, the name set is empty, and those tools leak.
    """
    try:
        tools = registry.get_tools(
            doc_type=doc_type,
            uno_services_supported=uno_services_supported,
            exclude_tiers=(),
            **_doc_filter(doc),
        )
    except Exception:
        return frozenset()
    out = set()
    for t in tools:
        if getattr(t, "specialized_domain", None) in _SIDEBAR_ONLY_DOMAINS:
            name = getattr(t, "name", None)
            if isinstance(name, str):
                out.add(name)
    return frozenset(out)


class FindTools(ToolBase):
    """Discover specialized tools by domain catalog + per-domain schema listing.

    Returns the same domain catalog as the delegate gateway; call with ``domain`` to get
    every MCP schema in that area. Document-optional.
    """

    name: str | None = "find_tools"
    description: str = (
        "Discover additional tools that are available but not listed here. This MCP "
        "server exposes a small core tool set; many specialized capabilities are "
        "callable by name but hidden from the default list to keep it short. Call "
        "find_tools with no arguments to get the full specialized domain catalog "
        "(same list as the delegate gateway). Then call find_tools with a `domain` to "
        "get every tool schema in that area, ready to call directly. Always prefer "
        "calling a tool returned by find_tools over giving up because a capability "
        "seems missing."
    )
    tier: str = "mcp"
    is_mutation: bool | None = False
    requires_document: bool = False  # discovery needs no open document
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "domain": {
                "type": "string",
                "description": ("Specialized area to list tools for (e.g. 'footnotes', "
                                "'charts', 'styles'). Call find_tools with no arguments first "
                                "to see the available domains and their descriptions."),
            },
        },
        "required": [],
    }

    def execute(self, ctx: ToolContext, domain: str | None = None, **kwargs: Any) -> dict[str, Any]:
        registry = ctx.services.get("tools") if ctx.services else None
        if registry is None:
            return self._tool_error("Tool registry unavailable.", code="SERVICE_UNAVAILABLE")

        domain = domain.strip().lower() if isinstance(domain, str) and domain.strip() else None

        doc = getattr(ctx, "doc", None)
        agent_label = agent_label_for_doc_type(getattr(ctx, "doc_type", None)) if doc is not None else None
        # for_discovery: this catalog is what a direct_discovery client uses to find tools, so it
        # must cover everything the flat tool list exposes. Exclusions that only shape a chat
        # prompt would otherwise make a listed, callable tool impossible to discover.
        catalog = get_specialized_domain_catalog(agent_label=agent_label, ctx=getattr(ctx, "ctx", None),
                                                 for_discovery=True)

        out: dict[str, Any] = {
            "status": "ok",
            "domain": domain,
            "available_domains": catalog,
            "tools": [],
        }
        if not domain:
            if doc is None:
                out["note"] = ("No document is open; the catalog lists all apps. "
                               "Open the matching document before calling an app-specific tool.")
            return out

        # Catalog ids use underscores; only ppt-master has a hyphen.
        if domain in _SIDEBAR_ONLY_DOMAINS or domain.replace("_", "-") in _SIDEBAR_ONLY_DOMAINS:
            out["note"] = "Sidebar-only flow; its tools are not exposed over MCP."
            return out
        if domain not in {str(e.get("domain") or "") for e in catalog}:
            return self._tool_error(
                f"Unknown or unavailable specialized domain '{domain}'.",
                code="UNKNOWN_DOMAIN",
                available_domains=catalog,
            )

        schemas = registry.get_schemas("mcp", doc=doc, active_domain=domain, **_doc_filter(doc))
        from plugin.mcp.mcp_protocol import drop_unavailable_domains
        schemas = drop_unavailable_domains(schemas, registry, getattr(ctx, "ctx", None))

        tools: list[dict[str, Any]] = []
        for s in (schemas or []):
            if not isinstance(s, dict):
                continue
            name = s.get("name")
            if not isinstance(name, str) or not name:
                continue
            if name == _FINISH_TOOL:
                continue
            tools.append(s)
        tools.sort(key=lambda s: str(s.get("name") or ""))

        out["tools"] = tools
        if doc is None:
            out["note"] = ("No document is open, so results span all document types; "
                           "open the matching document before calling an app-specific tool.")
        guidance = get_domain_guidance(domain, agent_label=agent_label)
        if guidance:
            out["domain_guidance"] = {domain: guidance}
        return out
