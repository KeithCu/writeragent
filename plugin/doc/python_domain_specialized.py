# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Two-level python specialized agent: outer venv loop, inner domain-tool agent.

Mirrors document research (``delegate_read_document`` → ``run_inner_read_agent``).
The outer python agent (``domain="python"``) does venv / symbolic work itself and
does not receive specialized domain tools. When it needs them it calls
``delegate_tool_domains`` with ``domains`` + ``task``.

The inner agent gets ``run_venv_python_script`` with ``python_tool_domain`` set
to those domains plus ``core``, so one script can loop ``writeragent_api``
(``wa.shape.upsert``, ``wa.core.list_open_documents``, and the rest of the
catalog). The inner instructions include that catalog (full proxy docstrings
and Args). The venv sandbox blocks ``inspect`` / ``dir`` / ``__doc__``, so the
model cannot read the signatures itself. Shapes mutators stay off the inner
LLM list (``shape_summary`` remains for a check). Other domains still pass
their ``ToolBase`` tools. ``specialized_workflow_finished`` is always included.

The gateway parameter ``python_tool_domain`` stays commented out. This module
sets the host-only allowlist on the inner ``ToolContext``, not on the outer
gateway.
"""

from __future__ import annotations

import json
import logging
from typing import Any, ClassVar

from plugin.chatbot.smol_agent import SmolToolAdapter, run_inner_tool_agent
from plugin.doc.doc_type import agent_label_for_context
from plugin.framework.errors import user_stopped_payload
from plugin.framework.queue_executor import run_on_main
from plugin.framework.tool import ToolBase, ToolContext

log = logging.getLogger(__name__)

DELEGATE_TOOL_DOMAINS = "delegate_tool_domains"
_PYTHON_DOMAIN = "python"
_FINISH_TOOL = "specialized_workflow_finished"
_VENV_SCRIPT_TOOL = "run_venv_python_script"
_DOMAINS_ERROR = "domains must be a non-empty list of specialized domain names."
# Shapes-first: bulk geometry is a script loop, not N LLM mutator calls.
# Other domains (footnotes, sheets, …) keep their ToolBase tools.
_SCRIPT_PLACEMENT_DOMAINS = frozenset({"shapes"})
_SCRIPT_PLACEMENT_LLM_KEEP = frozenset({"shape_summary"})


def allowed_specialized_domains(agent_label: str, uno_ctx: Any = None) -> frozenset[str]:
    """Specialized domains this app's python agent may hand to an inner agent.

    ``python`` is omitted: the outer agent already has that toolset, and nesting
    it would expose ``delegate_tool_domains`` again.
    """
    from plugin.framework.prompts import get_specialized_domain_catalog

    catalog = get_specialized_domain_catalog(agent_label=agent_label, ctx=uno_ctx)
    return frozenset(entry["domain"] for entry in catalog if entry["domain"] != _PYTHON_DOMAIN)


def normalize_domain_list(raw: Any) -> tuple[list[str] | None, str | None]:
    """Return ``(names, None)`` or ``(None, error)``.

    A JSON array string is accepted because some models stringify tool arguments
    before the host parses them. A bare domain name is not: the parameter is a list.
    """
    if isinstance(raw, str):
        text = raw.strip()
        try:
            raw = json.loads(text) if text.startswith("[") else None
        except json.JSONDecodeError:
            raw = None
    if not isinstance(raw, (list, tuple)):
        return None, _DOMAINS_ERROR
    names: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            return None, "domains must be a list of non-empty domain name strings."
        name = item.strip()
        if name not in seen:
            seen.add(name)
            names.append(name)
    if not names:
        return None, "domains must contain at least one specialized domain name."
    return names, None


def validate_requested_domains(names: list[str], agent_label: str, uno_ctx: Any = None) -> tuple[str | None, str | None]:
    """Return ``(message, code)`` when *names* are not valid for *agent_label*."""
    allowed = allowed_specialized_domains(agent_label, uno_ctx)
    rejected_python = [name for name in names if name == _PYTHON_DOMAIN]
    unknown = [name for name in names if name not in allowed and name != _PYTHON_DOMAIN]
    known = ", ".join(sorted(allowed))
    if unknown:
        extra = ""
        if rejected_python:
            extra = " The python domain cannot be nested (this agent already has venv and symbolic tools)."
        return (
            f"Unknown specialized domain(s) for {agent_label}: {', '.join(unknown)}.{extra} Known domains: {known}.",
            "UNKNOWN_SPECIALIZED_DOMAIN",
        )
    if rejected_python:
        return (
            "Cannot nest the python domain. This agent already has venv, symbolic math, and python helpers. "
            "Pass other specialized domains in domains (for example shapes, footnotes, tables, sheets).",
            "PYTHON_DOMAIN_NOT_NESTED",
        )
    return None, None


def _filter_document_research_tools(tools: list[ToolBase], parent_ctx: ToolContext) -> list[ToolBase]:
    """Same discovery/peer filters as the document_research specialized loop."""
    from plugin.doc.document_research import filter_document_research_discovery_tools
    from plugin.doc.peer_message import filter_peer_tools_for_specialized

    filtered = filter_document_research_discovery_tools(tools)
    return filter_peer_tools_for_specialized(filtered, parent_ctx.ctx, parent_ctx.doc)


def script_only_llm_tool_names(domain: str) -> frozenset[str]:
    """Shape-domain proxy names the inner LLM must not see.

    Scripts still call them: ``python_tool_domain`` allowlists the same
    ``DOMAIN_TOOLS`` entry. ``shape_summary`` stays so the agent can verify.
    Footnotes and other domains return an empty set and keep their tools.
    """
    if domain not in _SCRIPT_PLACEMENT_DOMAINS:
        return frozenset()
    from plugin.scripting.host_rpc import domain_proxy_tool_names

    names = domain_proxy_tool_names(domain)
    if not names:
        return frozenset()
    return frozenset(name for name in names if name not in _SCRIPT_PLACEMENT_LLM_KEEP)


def gather_domain_tools(parent_ctx: ToolContext, domains: list[str]) -> list[ToolBase]:
    """Union of registered tools for *domains*, plus venv script and finish.

    Fetched on the main thread. ``delegate_tool_domains`` is dropped so the inner
    agent cannot start another outer→inner hop. ``run_venv_python_script`` is
    ``specialized_cross_cutting`` on the python domain, so a shapes/footnotes
    lookup does not return it; it is fetched by name. Shapes mutators are
    omitted (see ``script_only_llm_tool_names``).
    """
    registry = parent_ctx.services.get("tools") if getattr(parent_ctx, "services", None) is not None else None
    if registry is None:
        return []

    def _fetch() -> list[ToolBase]:
        body: list[ToolBase] = []
        venv: list[ToolBase] = []
        finish: list[ToolBase] = []
        seen: set[str] = set()
        for domain in domains:
            found = registry.get_tools(
                doc=parent_ctx.doc,
                doc_type=parent_ctx.doc_type,
                active_domain=domain,
                exclude_tiers=(),
                ctx=parent_ctx.ctx,
                uno_services_supported=getattr(parent_ctx, "uno_services_supported", None),
            )
            if domain == "document_research":
                try:
                    found = _filter_document_research_tools(list(found), parent_ctx)
                except Exception:
                    log.exception("document_research filter failed for domain tool agent")
            hidden = script_only_llm_tool_names(domain)
            added = False
            hid_script_tools = False
            for tool in found:
                name = tool.name or ""
                if not name or name in seen or name == DELEGATE_TOOL_DOMAINS:
                    continue
                if name in hidden:
                    # Still mark seen so a later domain cannot put the mutator back.
                    seen.add(name)
                    hid_script_tools = True
                    continue
                seen.add(name)
                if name == _FINISH_TOOL:
                    finish.append(tool)
                else:
                    body.append(tool)
                    added = True
            if not added and not hid_script_tools:
                log.warning("Domain tool agent: no tools for domain %s", domain)
        # Not returned by a non-python domain lookup. The inner script is how
        # bulk shape placement reaches writeragent_api.
        missing = [name for name in (_VENV_SCRIPT_TOOL, _FINISH_TOOL) if name not in seen]
        if missing:
            for tool in registry.get_tools(names=missing, exclude_tiers=(), filter_doc_type=False):
                if tool.name == _VENV_SCRIPT_TOOL:
                    venv.append(tool)
                elif tool.name == _FINISH_TOOL:
                    finish.append(tool)
        return body + venv + finish

    return run_on_main(_fetch)


def _inner_context(parent_ctx: ToolContext, domains: list[str]) -> ToolContext:
    """Same document and callbacks as the outer python agent.

    ``set_active_domain_callback`` is omitted: the outer chat loop owns the
    callback, and the inner context omits it so finishing the inner task does
    not clear the outer python session.

    ``python_tool_domain`` is the delegated names plus ``core`` (comma-separated).
    ``RunVenvPythonScript`` forwards it to the worker so script RPC is allowlisted
    to those ``writeragent_api`` domains (``shapes`` → ``shape``) and
    ``DOMAIN_TOOLS['core']``. ``run_venv_python_script`` stays off that set.

    ``active_domain`` stays ``None`` and ``read_only_target`` stays ``False`` so
    the inner loop does not inherit the outer python domain or a read-only parent.
    """
    from plugin.scripting.host_rpc import inner_script_tool_domain

    return parent_ctx.derive(
        set_active_domain_callback=None,
        active_domain=None,
        read_only_target=False,
        python_tool_domain=inner_script_tool_domain(domains),
    )


def _domain_loop_hints(parent_ctx: ToolContext, domains: list[str], agent_label: str) -> str:
    """Small suffixes the single-domain specialized loop adds, when those domains are requested.

    Shapes canvas is a UNO read, so it is fetched on the main thread. Footnotes
    and charts lines match ``DelegateToSpecializedBase.execute`` (static text).
    """
    parts: list[str] = []
    if "footnotes" in domains:
        # plugin/doc/specialized_base.py — domain == "footnotes"
        parts.append(
            " For footnotes_insert: if the task quotes or names the document anchor (e.g. a sentence),"
            " pass that exact string as insert_after so the note is placed after that text;"
            " the task executor cannot move the view cursor."
        )
    if "charts" in domains:
        # plugin/doc/specialized_base.py — domain == "charts"
        if agent_label == "Calc":
            parts.append(" When creating a chart in Calc, you MUST specify the data range explicitly (e.g. data_range='A1:B10').")
        elif agent_label in ("Writer", "Draw"):
            parts.append(" When creating or editing a chart in Writer or Draw/Impress, you MUST specify both the `headers` and `rows` parameters.")
    if "shapes" in domains:
        from plugin.doc.specialized_shapes_context import format_shapes_canvas_context

        canvas = run_on_main(lambda: format_shapes_canvas_context(getattr(parent_ctx, "doc", None)))
        if canvas:
            parts.append(canvas if canvas.startswith(" ") else " " + canvas)
    return "".join(parts)


class _InnerToolErrorCapture(SmolToolAdapter):
    _errors: list[dict[str, Any]]

    def __init__(self, tool: ToolBase, tctx: ToolContext, errors: list[dict[str, Any]]) -> None:
        super().__init__(tool, tctx, safe=True, inputs_style="specialized")
        self._errors = errors

    def forward(self, *args: Any, **kwargs: Any) -> Any:
        result = super().forward(*args, **kwargs)
        if isinstance(result, dict) and result.get("status") == "error":
            self._errors.append(result)
        return result


# Bugfix: the inner loop returned status ok for inner tool errors and for
# max-step prose, because _compact_result never inspected either. Record
# inner error payloads and treat a run that did not call the finish tool as
# incomplete (same contract as DelegateToSpecializedBase).
def _normalize_inner_result(final_ans: Any, domains: list[str], tool_errors: list[dict[str, Any]]) -> dict[str, Any]:
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


def run_inner_domain_tool_agent(parent_ctx: ToolContext, domains: list[str], task: str) -> dict[str, Any]:
    """Run a focused smol agent with the union of *domains*' production tools."""
    from plugin.framework.errors import make_tool_error

    label = agent_label_for_context(parent_ctx)
    ordered = gather_domain_tools(parent_ctx, domains)
    domain_tools = [tool for tool in ordered if tool.name != _FINISH_TOOL]
    if not domain_tools:
        return make_tool_error(
            f"No specialized tools found for domains: {', '.join(domains)}.",
            code="NO_DOMAIN_TOOLS",
        )

    inner_ctx = _inner_context(parent_ctx, domains)
    inner_tool_errors: list[dict[str, Any]] = []
    # inputs_style="specialized" keeps enum / items / descriptions, same as other
    # specialized loops — not the slim librarian input shape.
    smol_tools = [_InnerToolErrorCapture(tool, inner_ctx, inner_tool_errors) for tool in ordered]
    domain_list = ", ".join(domains)
    hints = _domain_loop_hints(parent_ctx, domains, label)
    # Shapes mutators are not on this tool list. One script loops the API.
    # The catalog (below) is what every delegated domain may call from that script.
    script_hint = ""
    if "shapes" in domains:
        script_hint = (
            " For create/draw/diagram/composite tasks (including when the task says to write a"
            " script that uses shapes) you MUST write one run_venv_python_script with a Python"
            " for-loop that calls wa.shape.upsert(...) before finishing — actually place shapes"
            " on the page. Those shape mutators are not LLM tools. Do not invent JSON geometry,"
            " return a pasteable script/macro listing, or finish with prose alone. Use page-scale"
            " HMM sizes: a full-page composite is typically 10000–20000 HMM wide. Width near"
            " 1900 HMM (~19mm) is a tiny speck and is wrong. shape_summary can verify."
        )
    instructions = (
        f"You are an inner {label} agent with tools for these specialized domains: {domain_list}. "
        "Use those tools to accomplish the task. Do not invent tools outside this list."
        f"{script_hint}"
        " Call specialized_workflow_finished with a compact summary when done."
        f"{hints}"
    )
    # Full Args from writeragent_api, not a summary. Sandbox blocks reflection.
    from plugin.scripting.host_rpc import format_script_api_catalog

    catalog = format_script_api_catalog(domains)
    if catalog:
        instructions = f"{instructions}\n\n{catalog}"
    # Not the outer ``*:python`` block: that few-shot calls delegate_tool_domains,
    # which this inner list does not include. Shapes uses the venv for-loop block.
    if "shapes" in domains:
        examples_key = "domain_tools:shapes"
    else:
        examples_key = f"domain_tools:{parent_ctx.doc_type or label.lower()}"

    def tool_call_handler(step: Any) -> None:
        cb = parent_ctx.append_thinking_callback
        if cb:
            cb(f"Domain tool: {step.name}\n")
        sc = parent_ctx.status_callback
        if sc:
            sc(f"Domain tool: {step.name}...")

    final_ans = run_inner_tool_agent(
        inner_ctx,
        smol_tools,
        instructions=instructions,
        examples_key=examples_key,
        final_answer_tool_name=_FINISH_TOOL,
        task=task,
        stop_message="Domain tool agent stopped by user.",
        error_prefix="Domain tool agent failed",
        tool_call_handler=tool_call_handler,
    )
    return _normalize_inner_result(final_ans, domains, inner_tool_errors)


class DelegateToolDomains(ToolBase):
    """Outer python-domain tool: spin an inner agent for one or more specialized domains."""

    name: str | None = DELEGATE_TOOL_DOMAINS
    description: str = (
        "Run an inner agent for one or more specialized domains "
        "(shapes, footnotes, tables, sheets, and the other domains for this document). "
        "ALWAYS call this for create/draw/diagram/composite/layout tasks that need shapes "
        "(or other listed domains), including when the task says 'write a script' or "
        "'using the shapes domain tools' — never invent JSON geometry, coordinate lists, "
        "ASCII art, or a pasteable Python/macro listing as a substitute for drawing on the page. "
        "For draw tasks the inner agent MUST use one run_venv_python_script with a Python "
        "for-loop over the allowed domain APIs (import writeragent as wa, for example "
        "wa.shape.upsert); finishing without that script is wrong when the user asked to "
        "create or draw. Do venv scripts, symbolic math, and python helpers yourself when "
        "the task does not need those domain tools. Do not pass python in domains — this "
        "agent already has that toolset. Pass domains (list of domain names) and task "
        "(what the inner agent should accomplish, including that bulk shapes go through "
        "the script loop at page-scale HMM — typically 10000–20000 HMM wide, never ~1900 HMM / ~19mm speck)."
    )
    tier: str = "specialized"
    specialized_domain: ClassVar[str | None] = _PYTHON_DOMAIN
    specialized_cross_cutting: ClassVar[bool] = True
    is_mutation: bool | None = True
    long_running: bool = True
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "domains": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "string"},
                "description": (
                    "Specialized domain names to inject (at least one). "
                    "Examples: shapes, footnotes, tables, sheets, ranges. Not python."
                ),
            },
            "task": {
                "type": "string",
                "description": "What the inner agent should accomplish with those domain tools.",
            },
        },
        "required": ["domains", "task"],
    }

    def is_async(self) -> bool:
        return True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        from plugin.framework.send_cancellation import SendCancelled

        names, names_err = normalize_domain_list(kwargs.get("domains"))
        if names_err or not names:
            return self._tool_error(names_err or _DOMAINS_ERROR, code="DOMAINS_REQUIRED")
        raw_task = kwargs.get("task")
        task = raw_task.strip() if isinstance(raw_task, str) else None
        if not task:
            return self._tool_error("task is required.", code="TASK_REQUIRED")

        label = agent_label_for_context(ctx)
        message, code = validate_requested_domains(names, label, getattr(ctx, "ctx", None))
        if message:
            return self._tool_error(message, code=code or "UNKNOWN_SPECIALIZED_DOMAIN")

        stop_checker = getattr(ctx, "stop_checker", None)
        if stop_checker is not None and stop_checker():
            return user_stopped_payload("Domain tool agent stopped by user.")

        try:
            return run_inner_domain_tool_agent(ctx, names, task)
        except SendCancelled:
            return user_stopped_payload("Domain tool agent stopped by user.")
