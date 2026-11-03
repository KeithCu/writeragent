# WriterAgent - moved from plugin.framework.prompts
"""Specialized domain catalog for delegate tools and MCP discovery."""
from __future__ import annotations

from typing import Any

from plugin.framework.prompts import (
    CALC_HIDDEN_SPECIALIZED_DOMAINS,
    CALC_SPECIALIZED_DELEGATION_TEMPLATE,
    DRAW_SPECIALIZED_DELEGATION_TEMPLATE,
    IMPRESS_DRAW_SIDEBAR_ONLY_DOMAINS,
    WRITER_SIDEBAR_ONLY_DOMAINS,
    WRITER_SPECIALIZED_DELEGATION_TEMPLATE,
)

DOMAIN_LOOP_HINTS: dict[str, dict[str | None, str]] = {
    "footnotes": {
        None: (
            "For footnotes_insert: if the task quotes or names the document anchor "
            "(e.g. a sentence), pass that exact string as insert_after so the "
            "note is placed after that text."
        ),
        "Writer": (
            "For footnotes_insert: if the task quotes or names the document anchor (e.g. a sentence), "
            "pass that exact string as insert_after so the note is placed after that text; "
            "the task executor cannot move the view cursor."
        ),
        "Calc": (
            "For footnotes_insert: if the task quotes or names the document anchor (e.g. a sentence), "
            "pass that exact string as insert_after so the note is placed after that text; "
            "the task executor cannot move the view cursor."
        ),
        "Draw": (
            "For footnotes_insert: if the task quotes or names the document anchor (e.g. a sentence), "
            "pass that exact string as insert_after so the note is placed after that text; "
            "the task executor cannot move the view cursor."
        ),
    },
    "charts": {
        "Calc": "When creating a chart in Calc, you MUST specify the data range explicitly (e.g. data_range='A1:B10').",
        "Writer": "When creating or editing a chart in Writer or Draw/Impress, you MUST specify both the `headers` and `rows` parameters.",
        "Draw": "When creating or editing a chart in Writer or Draw/Impress, you MUST specify both the `headers` and `rows` parameters.",
        None: (
            "For charts: in Calc you MUST pass an explicit data range (e.g. "
            "data_range='A1:B10'); in Writer or Draw/Impress you MUST pass both the "
            "`headers` and `rows` parameters."
        ),
    },
}

def _catalog_entries_from_base(*, agent_label: str | None = None, ctx: Any = None, for_discovery: bool = False) -> list[dict[str, str]]:
    """Build ``[{domain, description}, …]`` for one specialized base class (delegate/MCP catalog).

    ``for_discovery`` skips the exclusions that only shape a chat prompt. CALC_HIDDEN_SPECIALIZED_
    DOMAINS keeps Python delegation out of the Calc sidebar's suggestions, but the MCP tool list
    exposes those tools regardless — so applying it to find_tools made them callable and
    undiscoverable at once. The sidebar-only domains stay excluded either way: the flat tool list
    drops them too, so both modes agree without help.
    """
    base_cls = _special_base(agent_label)
    entries: list[dict[str, str]] = []
    for cls in base_cls.__subclasses__():
        domain = getattr(cls, "specialized_domain", None)
        desc = getattr(cls, "specialized_domain_description", None)
        if not domain:
            continue
        if agent_label == "Calc" and domain in CALC_HIDDEN_SPECIALIZED_DOMAINS and not for_discovery:
            continue
        if agent_label == "Writer" and domain in WRITER_SIDEBAR_ONLY_DOMAINS:
            continue
        if agent_label == "Draw" and domain in IMPRESS_DRAW_SIDEBAR_ONLY_DOMAINS:
            continue
        if ctx is not None:
            from plugin.vision.vision_availability import specialized_domain_available

            if not specialized_domain_available(str(domain), ctx):
                continue
        entries.append({"domain": str(domain), "description": str(desc or "")})
    return entries


def _special_base(agent_label: str | None) -> Any:
    if agent_label == "Calc":
        from plugin.calc.base import ToolCalcSpecialBase
        return ToolCalcSpecialBase
    if agent_label == "Draw":
        from plugin.draw.base import ToolDrawSpecialBase
        return ToolDrawSpecialBase
    from plugin.writer.specialized_base import ToolWriterSpecialBase
    return ToolWriterSpecialBase


def get_specialized_domain_catalog(*, agent_label: str | None, ctx: Any = None, for_discovery: bool = False) -> list[dict[str, str]]:
    """Full specialized domain catalog — same entries as sidebar/delegate domain hints.

    ``agent_label`` is ``Writer`` / ``Calc`` / ``Draw`` for one app, or ``None`` to merge
    all three (e.g. MCP ``find_tools`` with no document open).

    ``for_discovery`` is set by MCP ``find_tools``: it keeps the domains whose exclusion only
    shapes a chat prompt, so discovery covers everything the flat tool list exposes. The
    no-document merge uses the same flag as the per-app branches.
    """
    labels = (agent_label,) if agent_label in ("Writer", "Calc", "Draw") else ("Writer", "Calc", "Draw")
    seen: dict[str, str] = {}
    # for_discovery must be forwarded. python is hidden on Calc chat only.
    for label in labels:
        for entry in _catalog_entries_from_base(
            agent_label=label, ctx=ctx, for_discovery=for_discovery,
        ):
            dom = entry["domain"]
            desc = entry["description"]
            if dom not in seen or len(desc) > len(seen[dom]):
                seen[dom] = desc
    return [{"domain": dom, "description": seen[dom]} for dom in sorted(seen)]


def _get_specialized_domains_str(*, agent_label: str | None = None, ctx: Any = None) -> str:
    """Build a compact domain list for delegation hints and MCP schemas."""
    parts = []
    for entry in sorted(_catalog_entries_from_base(agent_label=agent_label, ctx=ctx), key=lambda e: e["domain"]):
        if entry["description"]:
            parts.append(f"{entry['domain']}: {entry['description']}")
        else:
            parts.append(entry["domain"])
    return "; ".join(parts)


def format_specialized_domains_description(*, agent_label: str | None = None, ctx: Any = None) -> str:
    """Domain enum help for MCP/OpenAPI (more compact than the full delegation hint)."""
    domains = _get_specialized_domains_str(agent_label=agent_label, ctx=ctx)
    if not domains:
        return "The specialized domain to activate."
    # Compact form for the enum property description to reduce bloat in MCP schema
    compact = domains.replace("; ", ", ")
    return f"domain one of: {compact}"


def get_specialized_delegation_tool_hint(agent_label: str, *, ctx: Any = None) -> str:
    """Full specialized-delegation guidance (sidebar system prompt and MCP ``tools/list``)."""
    domains_str = _get_specialized_domains_str(agent_label=agent_label, ctx=ctx)
    template = {
        "Calc": CALC_SPECIALIZED_DELEGATION_TEMPLATE,
        "Draw": DRAW_SPECIALIZED_DELEGATION_TEMPLATE,
    }.get(agent_label, WRITER_SPECIALIZED_DELEGATION_TEMPLATE)
    return template.format(domains=domains_str)


# Bugfix: the inner loop returned status ok for inner tool errors and for
# max-step prose, because _compact_result never inspected either. Record
# inner error payloads and treat a run that did not call the finish tool as
# incomplete (same contract as DelegateToSpecializedBase).
def normalize_inner_result(final_ans: Any, domains: list[str], tool_errors: list[dict[str, Any]]) -> dict[str, Any]:
    if isinstance(final_ans, dict) and final_ans.get("status") == "error":
        return final_ans
    payload: dict[str, Any]
    if isinstance(final_ans, dict) and "status" in final_ans:
        payload = dict(final_ans)
    else:
        payload = {"status": "ok", "result": "" if final_ans is None else str(final_ans)}
    payload["domains"] = list(domains)
    if tool_errors:
        # Discussion #2268: the finish answer stays in answer only, not copied into message and error.
        messages = [str(err.get("message") or err.get("error") or err) for err in tool_errors]
        text = "; ".join(messages) or "Specialized tool error."
        payload.update(status="error", code="SPECIALIZED_TOOL_ERROR", message=text, error=text)
        return payload
    if payload.get("finished") is True:
        return payload
    # Discussion #2262: a plain-text return sits in result; move it to message instead of copying it.
    if isinstance(final_ans, dict):
        text = str(final_ans.get("message") or final_ans.get("answer") or final_ans.get("result") or final_ans)
    else:
        text = payload.pop("result")
    payload.update(status="incomplete", code="SPECIALIZED_WORKFLOW_INCOMPLETE", message=text)
    return payload


