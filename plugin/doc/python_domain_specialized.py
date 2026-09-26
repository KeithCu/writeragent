# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Two-level python specialized agent: outer venv loop, inner domain-tool agent.

Mirrors document research (``delegate_read_document`` → ``run_inner_read_agent``).
The outer python agent (``domain="python"``) does venv / symbolic work itself.
When it needs Writer/Calc/Draw specialized tools it calls ``delegate_tool_domains``
with ``domains`` + ``task``. The inner smol agent receives those domains' real
``ToolBase`` instances (full schemas), not ``writeragent_api`` proxy stubs.

The gateway's commented ``python_tool_domain`` string is a different mechanism
(venv → LibreOffice RPC allowlist in ``host_rpc``). This module does not enable it.
"""

from __future__ import annotations

import json
import logging
from typing import Any, ClassVar

from plugin.chatbot.smol_agent import SmolAgentExecutor, SmolToolAdapter, build_toolcalling_agent
from plugin.chatbot.smol_examples import get_examples_block
from plugin.framework.tool import ToolBase, ToolContext

log = logging.getLogger(__name__)

DELEGATE_TOOL_DOMAINS = "delegate_tool_domains"
_PYTHON_DOMAIN = "python"
_FINISH_TOOL = "specialized_workflow_finished"


def _run_on_main(fn: Any) -> Any:
    """Run *fn* on the UI thread. ``get_tools(doc=…)`` may call ``supportsService``."""
    from plugin.framework.thread_guard import on_main_thread
    from plugin.framework import queue_executor

    if on_main_thread():
        return fn()
    return queue_executor.execute_on_main_thread(fn)


def agent_label_for_context(ctx: ToolContext) -> str:
    """Writer / Calc / Draw label used to validate specialized domain names."""
    label = (getattr(ctx, "doc_type", None) or "").strip().lower()
    if label == "calc":
        return "Calc"
    if label in ("draw", "impress"):
        return "Draw"
    if label == "writer":
        return "Writer"
    services = getattr(ctx, "uno_services_supported", None) or ()
    blob = " ".join(str(svc) for svc in services)
    if "SpreadsheetDocument" in blob:
        return "Calc"
    if "PresentationDocument" in blob or "DrawingDocument" in blob:
        return "Draw"
    if "TextDocument" in blob:
        return "Writer"
    return "Writer"


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
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, list):
                raw = parsed
            else:
                return None, "domains must be a non-empty list of specialized domain names."
        else:
            return None, "domains must be a non-empty list of specialized domain names."
    if not isinstance(raw, (list, tuple)):
        return None, "domains must be a non-empty list of specialized domain names."
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

    filtered = filter_document_research_discovery_tools(tools, parent_ctx.ctx)
    return filter_peer_tools_for_specialized(filtered, parent_ctx.ctx, parent_ctx.doc)


def gather_domain_tools(parent_ctx: ToolContext, domains: list[str]) -> list[ToolBase]:
    """Union of registered tools for *domains*, plus ``specialized_workflow_finished``.

    Fetched on the main thread. ``delegate_tool_domains`` is dropped so the inner
    agent cannot start another outer→inner hop.
    """
    registry = parent_ctx.services.get("tools") if getattr(parent_ctx, "services", None) is not None else None
    if registry is None:
        return []

    def _fetch() -> list[ToolBase]:
        body: list[ToolBase] = []
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
            added = False
            for tool in found:
                name = tool.name or ""
                if not name or name in seen or name == DELEGATE_TOOL_DOMAINS:
                    continue
                seen.add(name)
                if name == _FINISH_TOOL:
                    finish.append(tool)
                else:
                    body.append(tool)
                    added = True
            if not added:
                log.warning("Domain tool agent: no tools for domain %s", domain)
        if _FINISH_TOOL not in seen:
            extra = registry.get_tools(names=[_FINISH_TOOL], exclude_tiers=(), filter_doc_type=False)
            for tool in extra:
                if tool.name == _FINISH_TOOL:
                    finish.append(tool)
                    break
        return body + finish

    fetched: list[ToolBase] = _run_on_main(_fetch)
    return fetched


def _inner_context(parent_ctx: ToolContext) -> ToolContext:
    """Same document and callbacks as the outer python agent.

    ``set_active_domain_callback`` is not copied. ``specialized_workflow_finished``
    calls it when ``USE_SUB_AGENT`` is off, which would clear the outer python
    session while the inner agent is only finishing its own task.
    """
    return ToolContext(
        doc=parent_ctx.doc,
        ctx=parent_ctx.ctx,
        doc_type=parent_ctx.doc_type,
        services=parent_ctx.services,
        caller=parent_ctx.caller,
        active_page_index=getattr(parent_ctx, "active_page_index", None),
        status_callback=parent_ctx.status_callback,
        append_thinking_callback=parent_ctx.append_thinking_callback,
        stop_checker=parent_ctx.stop_checker,
        approval_callback=getattr(parent_ctx, "approval_callback", None),
        chat_append_callback=getattr(parent_ctx, "chat_append_callback", None),
        send_cancellation=getattr(parent_ctx, "send_cancellation", None),
        uno_services_supported=getattr(parent_ctx, "uno_services_supported", None),
    )


def _compact_result(final_ans: Any, domains: list[str]) -> dict[str, Any]:
    if isinstance(final_ans, dict) and final_ans.get("status") == "error":
        return final_ans
    if isinstance(final_ans, dict) and "result" in final_ans:
        payload = final_ans["result"]
    elif isinstance(final_ans, dict) and "answer" in final_ans:
        payload = final_ans["answer"]
    else:
        payload = final_ans
    if payload is None:
        payload = ""
    return {"status": "ok", "domains": list(domains), "result": str(payload)}


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

    inner_ctx = _inner_context(parent_ctx)
    # inputs_style="specialized" keeps enum / items / descriptions, same as other
    # specialized loops — not the slim librarian input shape.
    smol_tools = [SmolToolAdapter(tool, inner_ctx, safe=True, inputs_style="specialized") for tool in ordered]
    domain_list = ", ".join(domains)
    instructions = (
        f"You are an inner {label} agent with tools for these specialized domains: {domain_list}. "
        "Use those tools to accomplish the task. Do not invent tools outside this list. "
        "Call specialized_workflow_finished with a compact summary when done."
    )
    # Not a ``*:python`` key: that few-shot teaches run_venv_python_script, which
    # this inner agent does not have.
    examples_key = f"domain_tools:{parent_ctx.doc_type or label.lower()}"
    agent = build_toolcalling_agent(
        inner_ctx,
        smol_tools,
        instructions=instructions,
        final_answer_tool_name=_FINISH_TOOL,
        examples_block=get_examples_block(examples_key),
        status_callback=parent_ctx.status_callback,
    )
    executor = SmolAgentExecutor(inner_ctx)

    def tool_call_handler(step: Any) -> None:
        cb = parent_ctx.append_thinking_callback
        if cb:
            cb(f"Domain tool: {step.name}\n")
        sc = parent_ctx.status_callback
        if sc:
            sc(f"Domain tool: {step.name}...")

    final_ans = executor.execute_safe(
        agent,
        task,
        tool_call_handler=tool_call_handler,
        stop_message="Domain tool agent stopped by user.",
        error_prefix="Domain tool agent failed",
    )
    return _compact_result(final_ans, domains)


class DelegateToolDomains(ToolBase):
    """Outer python-domain tool: spin an inner agent for one or more specialized domains."""

    name: str | None = DELEGATE_TOOL_DOMAINS
    description: str = (
        "Run an inner agent that has the full tool schemas for one or more specialized domains "
        "(shapes, footnotes, tables, sheets, and the other domains for this document). "
        "Call this when the task needs those domain tools. "
        "Do venv scripts, symbolic math, and python helpers yourself when it does not. "
        "Do not pass python in domains — this agent already has that toolset. "
        "Pass domains (list of domain names) and task (what the inner agent should accomplish)."
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
        from plugin.framework.queue_executor import SendCancelled

        names, names_err = normalize_domain_list(kwargs.get("domains"))
        if names_err or not names:
            return self._tool_error(names_err or "domains must be a non-empty list of specialized domain names.", code="DOMAINS_REQUIRED")
        task = kwargs.get("task")
        if not isinstance(task, str) or not task.strip():
            return self._tool_error("task is required.", code="TASK_REQUIRED")

        label = agent_label_for_context(ctx)
        message, code = validate_requested_domains(names, label, getattr(ctx, "ctx", None))
        if message:
            return self._tool_error(message, code=code or "UNKNOWN_SPECIALIZED_DOMAIN")

        stop_checker = ctx.stop_checker if isinstance(ctx, ToolContext) else None
        if stop_checker is not None and stop_checker():
            return self._tool_error("Domain tool agent stopped by user.", code="USER_STOPPED")

        try:
            return run_inner_domain_tool_agent(ctx, names, task.strip())
        except SendCancelled:
            return self._tool_error("Domain tool agent stopped by user.", code="USER_STOPPED")
