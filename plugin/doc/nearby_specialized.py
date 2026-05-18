# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Inner read-only sub-agent for workspace cross-document reads."""

from __future__ import annotations

import logging
from typing import Any, ClassVar

from plugin.chatbot.smol_agent import SmolAgentExecutor, SmolToolAdapter, build_toolcalling_agent
from plugin.contrib.smolagents.toolcalling_agent_prompts import SPECIALIZED_EXAMPLES_BLOCK
from plugin.framework.tool import ToolBase, ToolContext
from plugin.doc.nearby import open_document_for_read, resolve_path_or_name

log = logging.getLogger(__name__)

READ_TOOLS_BY_DOC_TYPE: dict[str, frozenset[str]] = {
    "writer": frozenset({"get_document_content", "get_document_tree", "search_in_document"}),
    "calc": frozenset({"get_sheet_summary", "read_cell_range"}),
    "draw": frozenset({"list_pages", "get_draw_tree"}),
}


def run_inner_read_agent(parent_ctx: ToolContext, opened_model: Any, doc_type: str, task: str) -> dict[str, Any] | str:
    """Run a focused read-only smol agent on *opened_model*; not a delegate gateway recurse."""
    registry = parent_ctx.services.get("tools")
    allowlist = READ_TOOLS_BY_DOC_TYPE.get(doc_type)
    if not allowlist:
        return {"status": "error", "message": f"No read tools configured for doc_type {doc_type!r}"}

    inner_ctx = ToolContext(
        doc=opened_model,
        ctx=parent_ctx.ctx,
        doc_type=doc_type,
        services=parent_ctx.services,
        caller=parent_ctx.caller,
        status_callback=parent_ctx.status_callback,
        append_thinking_callback=parent_ctx.append_thinking_callback,
        stop_checker=parent_ctx.stop_checker,
        read_only_target=True,
    )

    domain_tools = registry.get_tools(doc=opened_model, doc_type=doc_type, names=list(allowlist), exclude_tiers=())
    missing = allowlist - {t.name for t in domain_tools if t.name}
    if missing:
        log.warning("Inner workspace agent missing tools: %s", sorted(missing))

    finish_tools = registry.get_tools(names=["specialized_workflow_finished"], exclude_tiers=())
    tools_by_name = {t.name: t for t in domain_tools + finish_tools if t.name}
    ordered = [tools_by_name[n] for n in allowlist if n in tools_by_name]
    for t in finish_tools:
        if t.name == "specialized_workflow_finished" and t not in ordered:
            ordered.append(t)

    if not ordered:
        return {"status": "error", "message": "No read tools available for opened document"}

    smol_tools = [SmolToolAdapter(t, inner_ctx, safe=True, main_thread_sync=True, inputs_style="specialized") for t in ordered]

    instructions = (
        f"You are a read-only assistant for one {doc_type} file. "
        "Extract only the information needed for the task. Do not modify the document. "
        "Call specialized_workflow_finished with a compact summary when done."
    )

    agent = build_toolcalling_agent(
        inner_ctx,
        smol_tools,
        instructions=instructions,
        final_answer_tool_name="specialized_workflow_finished",
        examples_block=SPECIALIZED_EXAMPLES_BLOCK,
        status_callback=parent_ctx.status_callback,
    )
    executor = SmolAgentExecutor(inner_ctx)

    def tool_call_handler(step):
        cb = parent_ctx.append_thinking_callback
        if cb:
            cb(f"Workspace read tool: {step.name}\n")
        sc = parent_ctx.status_callback
        if sc:
            sc(f"Read: {step.name}...")

    final_ans = executor.execute_safe(
        agent,
        task,
        tool_call_handler=tool_call_handler,
        stop_message="Document read stopped by user.",
        error_prefix="Workspace read agent failed",
    )
    if isinstance(final_ans, dict) and final_ans.get("status") == "error":
        return final_ans
    return final_ans


class DelegateReadDocument(ToolBase):
    """Outer workspace tool: open a sibling file and run the inner read-only sub-agent."""

    name = "delegate_read_document"
    description = (
        "Open a nearby file by path or basename (read-only, hidden) and run a read-only sub-agent "
        "with production read tools for that file type. Returns extracted data to the workspace orchestrator."
    )
    tier = "specialized"
    specialized_domain: ClassVar[str | None] = "workspace"
    specialized_cross_cutting: ClassVar[bool] = True
    is_mutation = False
    long_running = True
    parameters = {
        "type": "object",
        "properties": {
            "path_or_name": {"type": "string", "description": "Absolute path, file URL, or basename/substring of a nearby file."},
            "task": {"type": "string", "description": "What to extract from that file (e.g. Q4 revenue figures)."},
        },
        "required": ["path_or_name", "task"],
    }

    def is_async(self) -> bool:
        return True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        from plugin.framework.queue_executor import execute_on_main_thread

        path_or_name = kwargs.get("path_or_name")
        task = kwargs.get("task")
        if not path_or_name or not task:
            return self._tool_error("path_or_name and task are required")

        def _run() -> dict[str, Any]:
            path, url_or_err = resolve_path_or_name(ctx.ctx, ctx.doc, str(path_or_name))
            if path is None:
                return self._tool_error(url_or_err or "Could not resolve file", details={"path_or_name": path_or_name})

            target = url_or_err if url_or_err and url_or_err.startswith("file://") else path
            model, doc_type, err = open_document_for_read(ctx.ctx, target)
            if model is None or doc_type is None:
                return self._tool_error(err or "Open failed", details={"path": path})

            result = run_inner_read_agent(ctx, model, doc_type, str(task))
            if isinstance(result, dict) and result.get("status") == "error":
                return result
            if isinstance(result, dict) and "result" in result:
                payload = result["result"]
            else:
                payload = result
            return {"status": "ok", "path": path, "doc_type": doc_type, "result": str(payload)}

        return execute_on_main_thread(_run)
