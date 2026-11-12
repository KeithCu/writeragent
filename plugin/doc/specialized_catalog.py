# WriterAgent - moved from plugin.framework.prompts
"""Specialized domain catalog for delegate tools and MCP discovery."""
from __future__ import annotations

from typing import Any, NamedTuple, Sequence

from plugin.framework.prompts import (
    CALC_HIDDEN_SPECIALIZED_DOMAINS,
    CALC_SPECIALIZED_DELEGATION_TEMPLATE,
    DRAW_SPECIALIZED_DELEGATION_TEMPLATE,
    IMPRESS_DRAW_SIDEBAR_ONLY_DOMAINS,
    WRITER_SIDEBAR_ONLY_DOMAINS,
    WRITER_SPECIALIZED_DELEGATION_TEMPLATE,
)

_FOOTNOTES_DIRECT_HINT = (
    "For footnotes_insert: if the task quotes or names the document anchor "
    "(e.g. a sentence), pass that exact string as insert_after so the "
    "note is placed after that text."
)
_FOOTNOTES_LOOP_HINT = (
    "For footnotes_insert: if the task quotes or names the document anchor (e.g. a sentence), "
    "pass that exact string as insert_after so the note is placed after that text; "
    "the task executor cannot move the view cursor."
)
_CHARTS_CALC_HINT = (
    "When creating a chart in Calc, you MUST specify the data range explicitly (e.g. data_range='A1:B10')."
)
_CHARTS_HEADERS_ROWS_HINT = (
    "When creating or editing a chart in Writer or Draw/Impress, you MUST specify both the `headers` and `rows` parameters."
)
_CHARTS_NEUTRAL_HINT = (
    "For charts: in Calc you MUST pass an explicit data range (e.g. "
    "data_range='A1:B10'); in Writer or Draw/Impress you MUST pass both the "
    "`headers` and `rows` parameters."
)

_LOOP_HINTS: dict[str, dict[str | None, str]] = {
    "footnotes": {
        None: _FOOTNOTES_DIRECT_HINT,
        "Writer": _FOOTNOTES_LOOP_HINT,
        "Calc": _FOOTNOTES_LOOP_HINT,
        "Draw": _FOOTNOTES_LOOP_HINT,
    },
    "charts": {
        "Calc": _CHARTS_CALC_HINT,
        "Writer": _CHARTS_HEADERS_ROWS_HINT,
        "Draw": _CHARTS_HEADERS_ROWS_HINT,
        None: _CHARTS_NEUTRAL_HINT,
    },
}


def loop_hint(domain: str, agent_label: str | None) -> str:
    """Prompt sentence a specialized loop adds for *domain*, or "" when there is none.

    agent_label is "Writer" / "Calc" / "Draw" for a running sub-agent loop, or None for
    MCP find_tools direct guidance with no app known (the host model runs the tools
    itself, so footnotes has no "task executor" clause and charts names both apps).
    Charts differ per app (Calc passes data_range; Writer and Draw pass headers/rows).
    Footnotes is one sentence for every app. Unknown domain or label -> "".
    """
    return _LOOP_HINTS.get(domain, {}).get(agent_label, "")


def catalog_entries_from_base(*, agent_label: str | None = None, ctx: Any = None, for_discovery: bool = False) -> list[dict[str, str]]:
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
        for entry in catalog_entries_from_base(
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
    for entry in sorted(catalog_entries_from_base(agent_label=agent_label, ctx=ctx), key=lambda e: e["domain"]):
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


class InnerToolCall(NamedTuple):
    """One tool call the inner smol agent made, in call order."""

    name: str
    result: Any
    mutating: bool


FINISH_TOOL_NAME = "specialized_workflow_finished"
NO_EDIT_TOOL_NOTE = "No document-editing tool ran during this delegation; the document is unchanged by it."
READ_ONLY_DOMAINS: frozenset[str] = frozenset({"document_research", "web_research"})


def expects_edits(domains: str | Sequence[str] | frozenset[str]) -> bool:
    """Whether any of the given domain(s) expect document-mutating edits.

    Read-only domains (document_research, web_research) do not expect edits.
    """
    if isinstance(domains, str):
        return domains not in READ_ONLY_DOMAINS
    domain_set = set(domains)
    return bool(domain_set) and not domain_set <= READ_ONLY_DOMAINS


def _err_text(err: Any) -> str:
    """Format an error entry into a diagnostic message string."""
    if isinstance(err, dict):
        return str(err.get("message") or err.get("error") or err)
    return str(err)


def partition_tool_errors(
    calls: Sequence[InnerToolCall],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Partition tool error dicts into unrecovered and recovered groups.

    An error call is considered recovered when a later call to the same tool name returned a
    non-error dict, including retries with corrected arguments.
    """
    unrecovered: list[dict[str, Any]] = []
    recovered: list[dict[str, Any]] = []
    for idx, call in enumerate(calls):
        if not (isinstance(call.result, dict) and call.result.get("status") == "error"):
            continue
        is_recovered = any(
            later.name == call.name
            and isinstance(later.result, dict)
            and later.result.get("status") != "error"
            for later in calls[idx + 1 :]
        )
        if is_recovered:
            recovered.append(call.result)
        else:
            unrecovered.append(call.result)
    return unrecovered, recovered


def apply_inner_outcome(
    payload: dict[str, Any],
    final_ans: Any,
    calls: Sequence[InnerToolCall],
    *,
    expect_edits: bool,
) -> dict[str, Any]:
    """Set status/code/message on a sub-agent payload from the observed call log.

    Mutates and returns payload so the main agent sees an accurate report of what tools
    ran, whether failures were recovered, and whether any document edits took place.
    """
    unrecovered, recovered = partition_tool_errors(calls)
    if unrecovered:
        # Unrecovered tool errors fail the delegation; the finish answer is kept in answer.
        messages = [_err_text(err) for err in unrecovered]
        text = "; ".join(messages) or "Specialized tool error."
        payload["status"] = "error"
        payload["code"] = "SPECIALIZED_TOOL_ERROR"
        payload["message"] = text
        payload["error"] = text
        return payload

    finish_seen = any(c.name == FINISH_TOOL_NAME for c in calls)
    finished = finish_seen or (isinstance(final_ans, dict) and final_ans.get("finished") is True)

    if not finished:
        # A plain-text return without finish is not a silent ok; move it from result to message
        # so the main agent sees it once.
        if isinstance(final_ans, dict):
            text = str(final_ans.get("message") or final_ans.get("answer") or final_ans.get("result") or final_ans)
        else:
            popped = payload.pop("result", "")
            text = "" if final_ans is None else str(popped)
        payload.update(status="incomplete", code="SPECIALIZED_WORKFLOW_INCOMPLETE", message=text)
        return payload

    if recovered:
        payload["recovered_tool_errors"] = [_err_text(err) for err in recovered]

    if expect_edits and not any(c.mutating for c in calls):
        payload["note"] = NO_EDIT_TOOL_NOTE

    return payload


def start_inner_payload(
    final_ans: Any,
    domains: Sequence[str],
    *,
    text_message: str | None = None,
) -> dict[str, Any]:
    """Shape the inner loop's final answer into the payload apply_inner_outcome then grades.

    An error dict (user stop, crash) is returned as the same object so the caller can
    pass it through unchanged. A dict with a status is copied. Anything else is the
    loop's plain text: {"status": "ok", "result": text} (None -> ""), plus
    "message": text_message when given. Every non-error payload gets "domains".
    """
    if isinstance(final_ans, dict) and final_ans.get("status") == "error":
        return final_ans
    payload: dict[str, Any]
    if isinstance(final_ans, dict) and "status" in final_ans:
        payload = dict(final_ans)
    else:
        payload = {"status": "ok", "result": "" if final_ans is None else str(final_ans)}
        if text_message is not None:
            payload["message"] = text_message
    payload["domains"] = list(domains)
    return payload


def normalize_inner_result(
    final_ans: Any,
    domains: list[str],
    calls: Sequence[InnerToolCall],
) -> dict[str, Any]:
    """Map inner domain agent results to the standard specialized tool response payload."""
    payload = start_inner_payload(final_ans, domains)
    if payload.get("status") == "error":
        return payload
    return apply_inner_outcome(
        payload,
        final_ans,
        calls,
        expect_edits=expects_edits(domains),
    )

