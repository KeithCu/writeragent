# WriterAgent - AI Writing Assistant for LibreOffice
"""Specialized domain catalog for delegate tools and MCP discovery."""
from __future__ import annotations

from collections import Counter
from typing import Any, NamedTuple, Sequence, cast

from plugin.framework.prompts import (
    CALC_HIDDEN_SPECIALIZED_DOMAINS,
    IMPRESS_DRAW_SIDEBAR_ONLY_DOMAINS,
    SHAPE_UNITS_RULE,
    WRITER_SIDEBAR_ONLY_DOMAINS,
)
from plugin.framework.tool import ToolBase, tool_supports_document

_FOOTNOTES_DIRECT_HINT = (
    "For footnote_insert: if the task quotes or names the document anchor "
    "(e.g. a sentence), pass that exact string as insert_after so the "
    "note is placed after that text."
)
_FOOTNOTES_LOOP_HINT = (
    "For footnote_insert: if the task quotes or names the document anchor (e.g. a sentence), "
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

_SHAPES_HINT = (
    f"{SHAPE_UNITS_RULE} "
    'For arrowheads on lines or connectors, pass line_start="arrow" or line_end="arrow" ("none" clears) to shape_upsert, shape_connect, or each shape_create_diagram connection.'
)

_INDEXES_HINT = "For a read-only table-of-contents request, report each entry's text and level (links only when asked)."

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
    "shapes": dict.fromkeys((None, "Writer", "Calc", "Draw"), _SHAPES_HINT),
    "indexes": dict.fromkeys((None, "Writer"), _INDEXES_HINT),
}


def loop_hint(domain: str, agent_label: str | None) -> str:
    """Prompt sentence a specialized loop adds for *domain*, or "" when there is none.

    agent_label is "Writer" / "Calc" / "Draw" for a running sub-agent loop, or None for
    MCP tool_find direct guidance with no app known (the host model runs the tools
    itself, so footnotes has no "task executor" clause and charts names both apps).
    Charts differ per app (Calc passes data_range; Writer and Draw pass headers/rows).
    Footnotes is one sentence for every app. Shapes is the coordinate-unit sentence for
    every app. Indexes carries the TOC read-only reporting rule here (rather than in the
    delegate catalog) because only the running sub-agent or MCP tool_find caller reports
    index results. Unknown domain or label -> "".
    """
    return _LOOP_HINTS.get(domain, {}).get(agent_label, "")


def catalog_entries_from_base(
    *,
    agent_label: str | None = None,
    ctx: Any = None,
    for_discovery: bool = False,
    for_mcp: bool = False,
    uno_services: frozenset[str] = frozenset(),
) -> list[dict[str, str]]:
    """Build ``[{domain, description}, …]`` for one specialized base class (delegate/MCP catalog).

    This is the single "bound for this document" filter, used by both the delegate description
    and the delegate enum; when ``uno_services`` is non-empty, domains whose base class does not
    support those services are omitted.

    ``for_discovery`` (MCP ``tool_find``) skips the exclusions that only shape a chat prompt
    (``CALC_HIDDEN_SPECIALIZED_DOMAINS`` — the MCP tool list exposes those tools regardless,
    so hiding them in ``tool_find`` made them callable but undiscoverable), while hiding
    ``tier == "chat"`` domains just like ``for_mcp`` does. Sidebar-only domains stay
    excluded either way: the flat tool list drops them too, so both modes agree without help.
    """
    base_cls = _special_base(agent_label)
    entries: list[dict[str, str]] = []
    for cls in base_cls.__subclasses__():
        domain = getattr(cls, "specialized_domain", None)
        desc = getattr(cls, "specialized_domain_description", None)
        if not domain:
            continue
        if (for_mcp or for_discovery) and _is_chat_tier(cls):
            continue
        if agent_label == "Calc" and domain in CALC_HIDDEN_SPECIALIZED_DOMAINS and not for_discovery:
            continue
        if agent_label == "Writer" and domain in WRITER_SIDEBAR_ONLY_DOMAINS:
            continue
        if agent_label == "Draw" and domain in IMPRESS_DRAW_SIDEBAR_ONLY_DOMAINS:
            continue
        if uno_services and not tool_supports_document(cast("ToolBase", cls), doc_type=None, uno_services_supported=uno_services):
            continue
        if ctx is not None or uno_services:
            # A live list, where the UNO context or the document's services are known, hides domains that cannot run.
            # Only the enum in DelegateToSubagent.get_parameters, read with neither, keeps every domain,
            # so a venv or MCP server added later in Settings still passes validation without a restart.
            # Lazy: vision_availability loads the vision venv and scripting sandbox modules.
            from plugin.vision.vision_availability import specialized_domain_available

            if not specialized_domain_available(str(domain), ctx):
                continue
        entries.append({"domain": str(domain), "description": str(desc or "")})
    return entries


def _special_base(agent_label: str | None) -> Any:
    # Lazy: plugin.writer.specialized_base imports plugin.doc.specialized_base, which
    # imports this module (import cycle); the Calc and Draw bases are cross-app.
    if agent_label == "Calc":
        from plugin.calc.base import ToolCalcSpecialBase
        return ToolCalcSpecialBase
    if agent_label == "Draw":
        from plugin.draw.base import ToolDrawSpecialBase
        return ToolDrawSpecialBase
    from plugin.writer.specialized_base import ToolWriterSpecialBase
    return ToolWriterSpecialBase


def get_specialized_domain_catalog(
    *,
    agent_label: str | None,
    ctx: Any = None,
    for_discovery: bool = False,
    for_mcp: bool = False,
) -> list[dict[str, str]]:
    """Full specialized domain catalog — same entries as sidebar/delegate domain hints.

    ``agent_label`` is ``Writer`` / ``Calc`` / ``Draw`` for one app, or ``None`` to merge
    all three (e.g. MCP ``tool_find`` with no document open).

    ``for_discovery`` is set by MCP ``tool_find``: it keeps the domains whose exclusion only
    shapes a chat prompt, so discovery covers everything the flat tool list exposes. The
    no-document merge uses the same flag as the per-app branches.
    ``for_mcp`` hides ``tier == "chat"`` domains without widening Calc exclusions.
    """
    labels = (agent_label,) if agent_label in ("Writer", "Calc", "Draw") else ("Writer", "Calc", "Draw")
    seen: dict[str, str] = {}
    # for_discovery must be forwarded. python is hidden on Calc chat only.
    for label in labels:
        for entry in catalog_entries_from_base(
            agent_label=label, ctx=ctx, for_discovery=for_discovery, for_mcp=for_mcp,
        ):
            dom = entry["domain"]
            desc = entry["description"]
            # labels iterates Writer, Calc, Draw; keep the first non-empty description so Writer wins cross-app merges regardless of length.
            if dom not in seen or not seen[dom]:
                seen[dom] = desc
    return [{"domain": dom, "description": seen[dom]} for dom in sorted(seen)]


def _is_chat_tier(cls: Any) -> bool:
    return getattr(cls, "tier", None) == "chat"


def chat_tier_domains() -> frozenset[str]:
    """Return specialized domains whose base class is marked ``tier == 'chat'``."""
    found: set[str] = set()
    for label in ("Writer", "Calc", "Draw"):
        for cls in _special_base(label).__subclasses__():
            domain = getattr(cls, "specialized_domain", None)
            if domain and _is_chat_tier(cls):
                found.add(str(domain))
    return frozenset(found)


def format_domain_catalog(entries: Sequence[dict[str, str]]) -> str:
    """Format catalog entries as a sorted ``domain: description`` semicolon list."""
    parts = []
    for entry in sorted(entries, key=lambda e: e["domain"]):
        if entry["description"]:
            parts.append(f"{entry['domain']}: {entry['description']}")
        else:
            parts.append(entry["domain"])
    return "; ".join(parts)


def format_specialized_domains_description(
    *,
    agent_label: str | None = None,
    ctx: Any = None,
    uno_services: frozenset[str] = frozenset(),
) -> str:
    """Domain enum help for MCP/OpenAPI (more compact than the sidebar delegate description's catalog)."""
    entries = catalog_entries_from_base(agent_label=agent_label, ctx=ctx, for_mcp=True, uno_services=uno_services)
    domains = format_domain_catalog(entries)
    if not domains:
        return "The specialized domain to activate."
    # Compact form for the enum property description to reduce bloat in MCP schema
    compact = domains.replace("; ", ", ")
    return f"domain one of: {compact}"


class InnerToolCall(NamedTuple):
    """One tool call the inner smol agent made, in call order.

    ``mutating`` is the tool kind (``detects_mutation()``), except that a call that
    never ran is recorded False.
    """

    name: str
    result: Any
    mutating: bool


FINISH_TOOL_NAME = "workflow_finished"
NO_EDIT_TOOL_NOTE = "No document-editing tool ran during this delegation; the document is unchanged by it."
READ_ONLY_DOMAINS: frozenset[str] = frozenset({"document_research", "mcp", "web_research"})


def expects_edits(domains: str | Sequence[str] | frozenset[str]) -> bool:
    """Whether any of the given domain(s) expect document-mutating edits.

    Read-only domains (document_research, mcp, web_research) do not expect edits.
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


def _tool_error_entry(call: InnerToolCall) -> dict[str, str]:
    """Build a structured error entry keeping tool name, message, and optional code."""
    entry: dict[str, str] = {"tool": call.name, "message": _err_text(call.result)}
    if isinstance(call.result, dict):
        code = call.result.get("code")
        if code:
            entry["code"] = str(code)
    return entry


def _is_error_result(result: Any) -> bool:
    """Return True when a tool result dict has status 'error'."""
    return isinstance(result, dict) and result.get("status") == "error"


def partition_tool_errors(
    calls: Sequence[InnerToolCall],
) -> tuple[list[InnerToolCall], list[InnerToolCall]]:
    """Partition tool error calls into unrecovered and recovered groups.

    An error call is considered recovered when a later call to the same tool name returned a
    non-error dict, including retries with corrected arguments.
    """
    unrecovered: list[InnerToolCall] = []
    recovered: list[InnerToolCall] = []
    for idx, call in enumerate(calls):
        if not _is_error_result(call.result):
            continue
        is_recovered = any(
            later.name == call.name
            and isinstance(later.result, dict)
            and later.result.get("status") != "error"
            for later in calls[idx + 1 :]
        )
        if is_recovered:
            recovered.append(call)
        else:
            unrecovered.append(call)
    return unrecovered, recovered


def record_edits(payload: dict[str, Any], calls: Sequence[InnerToolCall]) -> None:
    """Record landed editing-tool counts and the no-edit note onto *payload*.

    The note is true on every exit because mutating is the tool kind; if no editing
    tool ran at all, the document is unchanged whether the run finished, stopped
    early, or failed. A nested delegation that failed after its own edits landed
    counts once, under the delegate tool's name; its inner tool names are not merged in.
    """
    payload["edits"] = dict(
        Counter(
            c.name
            for c in calls
            if c.mutating and (not _is_error_result(c.result) or bool(c.result.get("edits")))
        )
    )
    if not any(c.mutating for c in calls):
        payload["note"] = NO_EDIT_TOOL_NOTE


def apply_inner_outcome(
    payload: dict[str, Any],
    final_ans: Any,
    calls: Sequence[InnerToolCall],
    *,
    expect_edits: bool,
) -> dict[str, Any]:
    """Set status, code, message, note, edits, and tool_errors on a sub-agent payload.

    Mutates and returns payload so the main agent sees an accurate report of what tools
    ran, whether failures were recovered, and—on any exit (ok, incomplete, or error)—
    counts of successful editing-tool calls (`edits`) when `expect_edits` is true; on
    error or incomplete, `edits` reports any edits that did land. A payload that is
    already an error gets only `edits` and the no-edit note.
    """
    unrecovered, recovered = partition_tool_errors(calls)
    if expect_edits:
        record_edits(payload, calls)
    # A user stop or crash keeps its own status, code and message.
    if payload.get("status") == "error":
        return payload
    if unrecovered:
        # Unrecovered tool errors fail the delegation; the finish answer is kept in answer.
        messages = [_err_text(c.result) for c in unrecovered]
        text = "; ".join(messages) or "Specialized tool error."
        payload["status"] = "error"
        payload["code"] = "SPECIALIZED_TOOL_ERROR"
        payload["message"] = text
        payload["error"] = text
        payload["tool_errors"] = [_tool_error_entry(c) for c in unrecovered]
        return payload

    # The accepted workflow_finished result is the run's final answer. A finish call in
    # *calls* is not proof: a later failure in its turn rejects it and the run goes on.
    if not (isinstance(final_ans, dict) and final_ans.get("finished") is True):
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
        payload["recovered_tool_errors"] = [_err_text(c.result) for c in recovered]

    return payload


def start_inner_payload(
    final_ans: Any,
    domains: Sequence[str],
) -> dict[str, Any]:
    """Shape the inner loop's final answer into the payload apply_inner_outcome then grades.

    An error dict (user stop, crash) is returned as the same object; apply_inner_outcome
    keeps its status, code and message and adds only edit counts. A dict with a
    status is copied. Anything else is the loop's plain text:
    {"status": "ok", "result": text} (None -> ""). Every non-error payload gets "domains".
    """
    if isinstance(final_ans, dict) and final_ans.get("status") == "error":
        return final_ans
    payload: dict[str, Any]
    if isinstance(final_ans, dict) and "status" in final_ans:
        payload = dict(final_ans)
    else:
        payload = {"status": "ok", "result": "" if final_ans is None else str(final_ans)}
    payload["domains"] = list(domains)
    return payload


def normalize_inner_result(
    final_ans: Any,
    domains: list[str],
    calls: Sequence[InnerToolCall],
) -> dict[str, Any]:
    """Map inner domain agent results to the standard specialized tool response payload."""
    payload = start_inner_payload(final_ans, domains)
    return apply_inner_outcome(
        payload,
        final_ans,
        calls,
        expect_edits=expects_edits(domains),
    )

