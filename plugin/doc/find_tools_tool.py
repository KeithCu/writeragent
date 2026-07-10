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

from plugin.framework.prompts import get_specialized_domain_catalog
from plugin.framework.tool import ToolBase, ToolContext

_FINISH_TOOL = "specialized_workflow_finished"
# In direct_discovery mode the delegate gateway is not the intended route, so keep
# its (core-tier) gateway tools out of discovery results.
_GATEWAY_PREFIX = "delegate_to_specialized_"


def _doc_filter(doc: Any) -> dict:
    """Registry kwargs for the active document.

    With no open document the registry filters out every app-specific tool (by
    ``uno_services``), which would leave discovery almost empty -- so pass
    ``filter_doc_type=False`` to list the whole catalog instead.
    """
    return {} if doc is not None else {"filter_doc_type": False}


def _agent_label_for_doc_type(doc_type: str | None) -> str | None:
    if not doc_type:
        return None
    return {"calc": "Calc", "draw": "Draw", "impress": "Draw",
            "writer": "Writer"}.get(doc_type.lower())


def get_domain_guidance(domain: str, *, agent_label: str | None = "Writer", ctx: Any = None) -> str:
    """Extra per-domain usage hints for direct callers (beyond catalog descriptions).

    Returns ``""`` when there is no supplementary hint beyond ``specialized_domain_description``.
    """
    if domain == "footnotes":
        return ("For footnotes_insert: if the task quotes or names the document anchor "
                "(e.g. a sentence), pass that exact string as insert_after_text so the "
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
        return ("Discover local image files with list_nearby_image_files before insert_image "
                "when the user refers to a photo in the folder.")
    if domain == "analysis":
        return ("For stats, cleaning, regression, clustering, or simulation on tabular data "
                "use analyze_data; for charts use plot_data (or auto_plot=true); for live "
                "single-cell what-if use calc_goal_seek; for constrained optimization use "
                "calc_solver. Always pass a data_range (A1 address) for bulk data.")
    if domain == "python":
        if agent_label is None:
            return ("run_venv_python_script: in Calc, pass `data_range` (an A1 address) to inject "
                    "cell values as `data`; in Writer or Draw/Impress it does not inject "
                    "spreadsheet data -- use document tools for content.")
        try:
            from plugin.framework.prompts import python_specialized_sub_agent_hint
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


def sidebar_only_tool_names(
    registry: Any,
    doc: Any,
    *,
    doc_type: str | None = None,
    uno_services_supported: frozenset[str] | None = None,
) -> frozenset:
    """Tool names in sidebar-only domains (brainstorming, writing_plan, ppt-master)."""
    try:
        from plugin.framework.prompts import IMPRESS_DRAW_SIDEBAR_ONLY_DOMAINS, WRITER_SIDEBAR_ONLY_DOMAINS

        sidebar_only = WRITER_SIDEBAR_ONLY_DOMAINS | IMPRESS_DRAW_SIDEBAR_ONLY_DOMAINS
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
        if getattr(t, "specialized_domain", None) in sidebar_only:
            name = getattr(t, "name", None)
            if isinstance(name, str):
                out.add(name)
    return frozenset(out)


class FindTools(ToolBase):
    """Discover specialized tools by domain catalog + per-domain schema listing.

    Returns the same domain catalog as the delegate gateway; call with ``domain`` to get
    every MCP schema in that area. Document-optional.
    """

    name = "find_tools"
    description = (
        "Discover additional tools that are available but not listed here. This MCP "
        "server exposes a small core tool set; many specialized capabilities are "
        "callable by name but hidden from the default list to keep it short. Call "
        "find_tools with no arguments to get the full specialized domain catalog "
        "(same list as the delegate gateway). Then call find_tools with a `domain` to "
        "get every tool schema in that area, ready to call directly. Always prefer "
        "calling a tool returned by find_tools over giving up because a capability "
        "seems missing."
    )
    tier = "mcp"
    is_mutation = False
    requires_document = False  # discovery needs no open document
    parameters = {
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
        agent_label = _agent_label_for_doc_type(getattr(ctx, "doc_type", None)) if doc is not None else None
        catalog = get_specialized_domain_catalog(agent_label=agent_label, ctx=getattr(ctx, "ctx", None))

        if not domain:
            out: dict[str, Any] = {
                "status": "ok",
                "domain": None,
                "available_domains": catalog,
                "tools": [],
            }
            if doc is None:
                out["note"] = ("No document is open; the catalog lists all apps. "
                               "Open the matching document before calling an app-specific tool.")
            return out

        from plugin.framework.prompts import WRITER_SIDEBAR_ONLY_DOMAINS
        if domain in WRITER_SIDEBAR_ONLY_DOMAINS:
            schemas = []
        else:
            schemas = registry.get_schemas("mcp", doc=doc, active_domain=domain, **_doc_filter(doc))

        sidebar_only = sidebar_only_tool_names(registry, doc)
        tools: list[dict] = []
        for s in (schemas or []):
            if not isinstance(s, dict):
                continue
            name = s.get("name")
            if not isinstance(name, str) or not name:
                continue
            if name == _FINISH_TOOL or name.startswith(_GATEWAY_PREFIX) or name in sidebar_only:
                continue
            tools.append(s)
        tools.sort(key=lambda s: str(s.get("name") or ""))

        result: dict[str, Any] = {
            "status": "ok",
            "domain": domain,
            "available_domains": catalog,
            "tools": tools,
        }
        if doc is None:
            result["note"] = ("No document is open, so results span all document types; "
                              "open the matching document before calling an app-specific tool.")
        label = agent_label
        guidance = get_domain_guidance(domain, agent_label=label, ctx=ctx)
        if guidance:
            result["domain_guidance"] = {domain: guidance}
        return result
