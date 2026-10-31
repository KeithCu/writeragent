# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""Shared base class for gateway tools that delegate to specialized toolsets."""

from __future__ import annotations

import copy
import logging
from types import SimpleNamespace
from typing import Any, cast, Type, ClassVar

from plugin.framework.errors import is_real_disposal
from plugin.framework.tool import ToolBase, ToolContext, field_from_tool_arguments, tool_supports_document
from plugin.framework.prompts import (
    CALC_HIDDEN_SPECIALIZED_DOMAINS,
    WRITER_SIDEBAR_ONLY_DOMAINS,
    IMPRESS_DRAW_SIDEBAR_ONLY_DOMAINS,
    attach_sheets_create_completion_instruction,
    DELEGATE_SPECIALIZED_TASK_PARAM_HINT,
    images_specialized_sub_agent_hint,
    python_specialized_sub_agent_hint,
    looks_like_peer_work_envelope,
    get_document_research_workflow_hint,
    annotate_outer_peer_delivery_pending,
    annotate_outer_peer_wait,
)
from plugin.framework.i18n import _
from plugin.chatbot.smol_agent import SmolToolAdapter, run_inner_tool_agent
from plugin.doc.specialized_shapes_context import format_shapes_canvas_context
from plugin.framework import queue_executor

log = logging.getLogger("writeragent.specialized")


def _outer_turn_is_peer_work(ctx: Any) -> bool:
    """True when this sidebar's current outer turn is a [Peer work from:…] envelope.

    Nested specializes (ranges/sheets/…) otherwise look "done" to the outer and it
    Ready-s without ``document_research`` → ``send_peer_result``. Probe the live
    panel listener's active turn ``active_query_text`` and last user ``session.messages``
    entry; RuntimeUID lookup is UNO so marshal to the main thread.
    """
    def _probe() -> bool:
        from plugin.doc.live_panels import get_live_panel
        from plugin.framework.uno_context import get_runtime_uid

        doc = getattr(ctx, "doc", None)
        if doc is None:
            return False
        uid = get_runtime_uid(doc) or ""
        if not uid:
            return False
        panel = get_live_panel(uid)
        if panel is None:
            return False
        listener = getattr(panel, "send_listener", None)
        if listener is None:
            return False
        from plugin.chatbot.tool_loop_actions import current_turn

        turn = current_turn(listener)
        if turn is not None and looks_like_peer_work_envelope(getattr(turn, "active_query_text", None)):
            return True
        session = getattr(listener, "session", None)
        messages = getattr(session, "messages", None) if session is not None else None
        if not messages:
            return False
        for msg in reversed(list(messages)):
            if not isinstance(msg, dict) or msg.get("role") != "user":
                continue
            return looks_like_peer_work_envelope(msg.get("content"))
        return False

    try:
        return bool(queue_executor.execute_on_main_thread(_probe))
    except Exception as e:
        # Cancelling peer-work must not log a warning with an empty
        # message. str(CancelledError()) is "", and %s interpolates that
        # to nothing. %r prints the exception class and repr.
        log.warning("peer-work turn probe failed: %r", e)
        return False


def _main_thread_or_warn(fn: Any, what: str) -> Any:
    """Marshal fn to the main thread. Re-raise real document disposal.

    Bugfix: a closed document raises DisposedException while reading the shapes
    canvas, Calc context, or open-document list. Those except blocks turned
    disposal into a missing page or a partial prompt. Re-raise real disposal so
    execute_safe reports DOCUMENT_DISPOSED. Any other failure keeps the empty
    context.
    """
    try:
        return queue_executor.execute_on_main_thread(fn)
    except Exception as e:
        if is_real_disposal(e):
            raise
        log.warning("Failed to get %s for sub-agent: %s", what, e)
        return ""


class DelegateToSpecializedBase(ToolBase):
    """Shared base for tools that delegate tasks to specialized sub-agents."""

    # Subclasses MUST override these
    _special_base_class: ClassVar[Type[ToolBase]]
    _agent_label: ClassVar[str]  # e.g., "Writer", "Calc", "Draw"

    tier: str = "core"  # Available to the main agent
    is_mutation: bool | None = True
    long_running: bool = True
    parameters: dict[str, Any] | None

    def __init__(self) -> None:
        super().__init__()
        domains: set[str] = set()
        # Find all domains by scanning subclasses of the specialized base
        for cls in self._special_base_class.__subclasses__():
            domain = getattr(cls, "specialized_domain", None)
            if domain:
                if self._agent_label == "Calc" and domain in CALC_HIDDEN_SPECIALIZED_DOMAINS:
                    continue
                if self._agent_label == "Writer" and domain in WRITER_SIDEBAR_ONLY_DOMAINS:
                    continue
                if self._agent_label == "Draw" and domain in IMPRESS_DRAW_SIDEBAR_ONLY_DOMAINS:
                    continue
                domains.add(str(domain))

        self.parameters = {
            "type": "object",
            "properties": {
                "domain": {"type": "string", "enum": sorted(domains), "description": "The specialized domain to activate."},
                # python_tool_domain stays off this schema on purpose. delegate_tool_domains
                # (plugin/doc/python_domain_specialized.py) sets ToolContext.python_tool_domain
                # (delegated domains plus core, including "" during =PY()). A property here would be a second API.
                "task": {"type": "string", "description": DELEGATE_SPECIALIZED_TASK_PARAM_HINT},
            },
            "required": ["domain", "task"],
        }

    def domains_for_services(self, uno_services: frozenset[str]) -> list[str]:
        """Sorted enum domains with a base class that runs on a document supporting *uno_services*.

        Bugfix: a Draw (DrawingDocument only) document was offered Impress-only domains
        (headers_footers, speaker_notes, slide_*) whose bases declare PresentationDocument,
        and picking one returned "No specialized tools found". Keyed on probed services,
        not the doc_type label: Impress's label is the family "draw" too (K082 regression).
        """
        assert uno_services, "caller must skip narrowing when services are unknown"
        assert self.parameters is not None
        enum = self.parameters["properties"]["domain"]["enum"]
        supported = {
            str(domain)
            for cls in self._special_base_class.__subclasses__()
            if (domain := getattr(cls, "specialized_domain", None))
            and tool_supports_document(cast("ToolBase", cls), doc_type=None, uno_services_supported=uno_services)
        }
        return [d for d in enum if d in supported]

    def is_async(self) -> bool:
        """Run in a background thread so the main-thread queue/drain loop isn't blocked."""
        return True

    # Domains whose work is read-only -> a long-running delegation to them must NOT
    # take the per-document mutation lock (it would needlessly serialize research on
    # the same doc). The gateway itself is is_mutation=True for the mutating domains.
    _READ_ONLY_DOMAINS: ClassVar[frozenset[str]] = frozenset({"document_research", "web_research"})

    def requires_document_lock(self, arguments: Any = None) -> bool:
        domain = field_from_tool_arguments(arguments, "domain")
        # Bugfix: Model or client passed a non-string domain (e.g. a nested dict or list) in tool
        # arguments. _prepare_mcp_execution calls requires_document_lock before schema validation, so
        # `domain in self._READ_ONLY_DOMAINS`
        # raised TypeError: unhashable type: 'dict'.
        # Checking isinstance(domain, str) ensures non-string domains safely fall back to requiring
        # the document lock (safe default), allowing execute() to subsequently return the standard
        # invalid-domain error.
        if isinstance(domain, str) and domain in self._READ_ONLY_DOMAINS:
            return False
        return super().requires_document_lock(arguments)

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        domain = kwargs.get("domain")
        task = kwargs.get("task")

        # Bugfix: Non-string domain (e.g. unhashable dict or None) cannot match any registered
        # domain and would fail inside ToolRegistry.get_tools frozenset lookups. Return the
        # standard invalid-domain error directly.
        if not isinstance(domain, str):
            return self._tool_error(
                f"No specialized tools found for domain '{domain}'. Ensure the tools are implemented and registered."
            )

        status_callback = getattr(ctx, "status_callback", None)
        append_thinking_callback = getattr(ctx, "append_thinking_callback", None)
        chat_append_callback = getattr(ctx, "chat_append_callback", None)

        if domain == "web_research":
            from plugin.chatbot.web_research import WebResearchTool

            return WebResearchTool().execute(ctx, query=task)

        if domain == "vision":
            from plugin.vision.vision_availability import vision_venv_configured
            from plugin.vision.vision_tools import ExtractStructureFromImage

            if not vision_venv_configured(ctx.ctx):
                return self._tool_error(
                    _(
                        "Local OCR requires Settings → Python venv with Docling or PaddleOCR "
                        "(Settings → Python → Test for install hints)."
                    ),
                    code="VISION_UNAVAILABLE",
                )

            if status_callback:
                status_callback(_("Running local OCR on selected image(s)..."))
            # Gateway shortcut: no sub-agent parses task — always insert after graphic(s).
            return ExtractStructureFromImage().execute(ctx, insert_into_document=True)

        if domain == "document_research":
            try:
                from plugin.embeddings.embeddings_indexer import enqueue_folder_index

                # Sub-agent runs on a worker thread; resolve_index_context reads the active doc path via UNO.
                queue_executor.execute_on_main_thread(lambda: enqueue_folder_index(ctx.ctx, ctx.services, ctx.doc))
            except Exception:
                log.debug("embeddings index wakeup failed", exc_info=True)

        if status_callback:
            status_callback(f"Delegating to specialized agent ({domain})...")

        # Gather tools for the requested domain (same rules as main chat ``active_domain``).
        # Must use ``ToolRegistry.get_tools(..., active_domain=...)`` so cross-app tools
        # (e.g. ``RunVenvPythonScript`` with ``specialized_cross_cutting``) are included;
        # ``isinstance(..., _special_base_class)`` alone misses Calc-registered tools on Writer delegate.
        registry = ctx.services.get("tools")

        def _fetch_domain_tools() -> tuple[list[ToolBase], str, str]:
            tools = registry.get_tools(
                doc=getattr(ctx, "doc", None),
                active_domain=domain,
                exclude_tiers=(),
                ctx=ctx.ctx,
            )
            peer_catalog = ""
            document_research_hint = ""
            if domain == "document_research":
                from plugin.doc.document_research import filter_document_research_discovery_tools
                from plugin.doc.peer_message import (
                    PEER_TOOL_NAMES,
                    filter_peer_tools_for_specialized,
                    format_peer_catalog,
                    list_v1_peers,
                )

                tools = filter_document_research_discovery_tools(tools)
                tools = filter_peer_tools_for_specialized(tools, ctx.ctx, ctx.doc)
                if any(getattr(t, "name", None) in PEER_TOOL_NAMES for t in tools):
                    peer_catalog = format_peer_catalog(list_v1_peers(ctx.ctx, ctx.doc))
                # #673 added list_v1_peers / getRuntimeUID to this hint. Specialized
                # execute is async: gather the catalog here with get_tools, not on
                # the tool-async worker.
                document_research_hint = get_document_research_workflow_hint(
                    ctx.ctx, getattr(ctx, "doc", None)
                )
            return tools, peer_catalog, document_research_hint

        # get_tools(doc=...) calls doc.supportsService — must not run on the sub-agent worker.
        domain_tools, peer_catalog, document_research_hint = queue_executor.execute_on_main_thread(
            _fetch_domain_tools
        )

        if not domain_tools:
            return self._tool_error(f"No specialized tools found for domain '{domain}'. Ensure the tools are implemented and registered.")

        # Specialized execute must see document_research so peer send tools'
        # caller guard allows this loop (not only ctx.caller == "chat").
        prev_active_domain = getattr(ctx, "active_domain", None)
        ctx.active_domain = domain
        # When an accepted inner peer send ran, the outer must Ready (not keep tooling).
        # Inner tool results that already carry web-research-style `instruction`
        # (create_sheet, etc.) so specialized finish can forward them to the outer.
        cap = SimpleNamespace(
            peer_send_invoked=False,
            peer_result_send_invoked=False,
            captured_tool_results=[],
            create_sheet_ran=False,
            workflow_finished=False,
            inner_tool_errors=[],
        )

        class _CaptureInstructionAdapter(SmolToolAdapter):
            def forward(self, *args: Any, **kwargs: Any) -> Any:
                result = super().forward(*args, **kwargs)
                if self.name == "specialized_workflow_finished":
                    cap.workflow_finished = True
                if isinstance(result, dict) and result.get("status") == "error":
                    cap.inner_tool_errors.append(result)
                if isinstance(result, dict) and result.get("instruction"):
                    cap.captured_tool_results.append(result)
                if domain != "document_research":
                    return result
                from plugin.doc.peer_message import PEER_RESULT_TOOL_NAME, PEER_TOOL_NAMES

                if self.name not in PEER_TOOL_NAMES:
                    return result
                # Delivery is recorded only after status == "ok" and accepted is True,
                # not when the model calls the tool. PEER_NOT_FOUND, PEER_SIDEBAR_NOT_OPEN,
                # PEER_QUEUE_FULL, and PEER_NOT_CHAT_MODE must leave both flags false.
                if (
                    isinstance(result, dict)
                    and result.get("status") == "ok"
                    and result.get("accepted") is True
                ):
                    cap.peer_send_invoked = True
                    if self.name == PEER_RESULT_TOOL_NAME:
                        cap.peer_result_send_invoked = True
                return result

        try:
            smol_tools = [_CaptureInstructionAdapter(t, ctx, safe=True, inputs_style="specialized") for t in domain_tools]
            if peer_catalog:
                from plugin.doc.peer_message import PEER_TOOL_NAMES

                for adapter in smol_tools:
                    if getattr(adapter, "name", None) in PEER_TOOL_NAMES:
                        adapter.description = (str(adapter.description or "") + " " + peer_catalog).strip()

            footnotes_hint = ""
            if domain == "footnotes":
                footnotes_hint = " For footnotes_insert: if the task quotes or names the document anchor (e.g. a sentence), pass that exact string as insert_after so the note is placed after that text; the task executor cannot move the view cursor."
            shapes_canvas = ""
            # Page size stays on the shapes loop (and the inner domain agent when
            # shapes is requested). The outer python agent passes layout in task.
            if domain == "shapes":
                shapes_canvas = _main_thread_or_warn(
                    lambda: format_shapes_canvas_context(getattr(ctx, "doc", None)),
                    "shapes canvas",
                )

            charts_hint = ""
            if domain == "charts":
                if self._agent_label == "Calc":
                    charts_hint = " When creating a chart in Calc, you MUST specify the data range explicitly (e.g. data_range='A1:B10')."
                elif self._agent_label in ("Writer", "Draw"):
                    charts_hint = " When creating or editing a chart in Writer or Draw/Impress, you MUST specify both the `headers` and `rows` parameters."

            calc_ctx = ""
            # Identity only: truthiness on a guard-proxied doc trips UNO bool on the MCP/long-running
            # worker. UNO reads stay inside _fetch_calc_context on the main thread.
            if self._agent_label == "Calc" and getattr(ctx, "doc", None) is not None:
                from plugin.calc.analyzer import get_calc_context_for_chat

                def _fetch_calc_context() -> str:
                    return "\n\n[SPREADSHEET CONTEXT]\n" + get_calc_context_for_chat(ctx.doc, ctx=ctx.ctx)

                calc_ctx = _main_thread_or_warn(_fetch_calc_context, "Calc context")

            open_docs_context = ""
            if domain == "document_research":
                from plugin.doc.document_research import get_open_documents

                open_docs = _main_thread_or_warn(
                    lambda: get_open_documents(ctx.ctx, ctx.doc),
                    "open documents",
                )
                if open_docs:
                    lines = []
                    for d in open_docs:
                        path_or_url = d["path"] or d["url"] or "Untitled"
                        doc_type = d["doc_type"]
                        active_str = " (Active)" if d["is_active"] else ""
                        lines.append(f"- {path_or_url} [{doc_type}]{active_str}")
                    open_docs_context = (
                        "\n\n[OPEN DOCUMENTS CONTEXT]\n"
                        "Note: These are the currently open files in LibreOffice. "
                        "Some of these files may be completely unrelated to the task at hand:\n"
                        + "\n".join(lines)
                    )

            images_hint = images_specialized_sub_agent_hint() if domain == "images" else ""
            python_hint = python_specialized_sub_agent_hint(self._agent_label) if domain == "python" else ""
            instructions = (
                f"You are a specialized {self._agent_label} task executor focused on the '{domain}' domain. "
                f"You have a focused set of tools to accomplish your task. Use them to fulfill the user's request."
                f"{footnotes_hint}{shapes_canvas}{charts_hint}{calc_ctx}{document_research_hint}{open_docs_context}{images_hint}{python_hint}"
            )

            examples_key = f"{self._agent_label.lower()}:{domain}"
            document_open_step_index = 0

            def tool_call_handler(step: Any) -> None:
                nonlocal document_open_step_index
                if step.name == "create_sheet":
                    cap.create_sheet_ran = True
                # Peer delivery is recorded in _CaptureInstructionAdapter.forward from
                # the tool return, not from this pre-execution call.
                if domain == "document_research" and step.name == "delegate_read_document" and chat_append_callback:
                    from plugin.chatbot.web_research_chat import document_open_step_chat_text

                    val = field_from_tool_arguments(step.arguments, "path_or_name")
                    path_or_name = str(val) if val is not None else ""
                    chat_append_callback(document_open_step_chat_text(path_or_name, document_open_step_index))
                    document_open_step_index += 1
                if append_thinking_callback:
                    append_thinking_callback(f"Running specialized tool: {step.name} with {step.arguments}\n")
                if status_callback:
                    status_callback(f"Tool: {step.name}...")

            final_ans = run_inner_tool_agent(
                ctx,
                smol_tools,
                instructions=instructions,
                examples_key=examples_key,
                final_answer_tool_name="specialized_workflow_finished",
                task=cast("str", task),
                stop_message="Specialized task stopped by user.",
                error_prefix="Specialized agent failed",
                tool_call_handler=tool_call_handler,
            )
        finally:
            ctx.active_domain = prev_active_domain

        # Discussion #2029: a user-stop or crash dict that is already status: "error"
        # is left unchanged.
        if isinstance(final_ans, dict) and final_ans.get("status") == "error":
            return final_ans

        if isinstance(final_ans, dict) and "status" in final_ans:
            payload = dict(final_ans)
        else:
            payload = {"status": "ok", "message": _("Specialized task ({domain}) completed.").format(domain=domain), "result": str(final_ans)}
        # Outer never sees create_sheet's inner-only ok string; forward the same
        # `instruction` field web research uses when empty tabs were created.
        if domain == "sheets" or cap.create_sheet_ran or cap.captured_tool_results:
            payload = attach_sheets_create_completion_instruction(
                payload,
                create_sheet_ran=cap.create_sheet_ran,
                tool_results=cap.captured_tool_results,
            )

        # Peer-work receiving turn: nested specialize (or research without an
        # accepted send_peer_result) is not delivery — stamp still-required
        # before Ready. A failed send_peer_result leaves this flag false so
        # the inner agent can retry. send_peer_work alone is not delivery.
        if not cap.peer_result_send_invoked and _outer_turn_is_peer_work(ctx):
            payload = annotate_outer_peer_delivery_pending(payload)

        elif domain == "document_research":
            # Idle-after-send for ask / accepted delivery sends.
            payload = annotate_outer_peer_wait(payload, peer_send_invoked=cap.peer_send_invoked)

        # Discussion #2029:
        # What was wrong: non-dict final answers (free text from subagents) and runs
        # where inner tools raised errors were reported as status: "ok", masking errors
        # and making unfinished subagent runs appear successful to the caller.
        # How it happened: execute wrapped any non-status return in a provisional ok payload
        # without inspecting inner tool execution results or requiring specialized_workflow_finished.
        # Why this change fixes it: inner tool errors are converted to status: "error" with code
        # SPECIALIZED_TOOL_ERROR and their messages. Discussion #2268: a finish answer stays
        # in answer only; message and error carry just the tool-error text. Runs without
        # specialized_workflow_finished returning and without tool errors become status: "incomplete"
        # with code SPECIALIZED_WORKFLOW_INCOMPLETE and the model text on message.
        is_finished = cap.workflow_finished or (isinstance(final_ans, dict) and final_ans.get("finished") is True)
        if cap.inner_tool_errors:
            combined_message = "; ".join(
                str(e.get("message") or e.get("error") or e) for e in cap.inner_tool_errors
            )
            payload["status"] = "error"
            payload["code"] = "SPECIALIZED_TOOL_ERROR"
            payload["message"] = combined_message
            payload["error"] = combined_message
        elif not is_finished:
            # Discussion #2262: a plain-text return sits in result (plus any peer note); move it to
            # message instead of copying it, so the text is sent once.
            if isinstance(final_ans, dict):
                model_text = str(final_ans.get("message") or final_ans.get("answer") or final_ans.get("result") or final_ans)
            elif final_ans is None:
                model_text = ""
            else:
                model_text = payload.pop("result")
            payload["status"] = "incomplete"
            payload["code"] = "SPECIALIZED_WORKFLOW_INCOMPLETE"
            payload["message"] = model_text

        return payload


def narrow_delegate_domain_enums(schemas: list[dict[str, Any]], tools: list[Any], uno_services: frozenset[str] | None) -> list[dict[str, Any]]:
    """Drop gateway ``domain`` enum values that cannot run on the document's UNO services.

    *schemas* are ``tools`` converted 1:1 (OpenAI ``function.parameters`` or MCP ``inputSchema``).
    Unknown services (no document / MCP without probe) keep every domain.
    """
    if not uno_services:
        return schemas
    out: list[dict[str, Any]] = []
    for schema, tool in zip(schemas, tools, strict=True):
        if not isinstance(tool, DelegateToSpecializedBase):
            out.append(schema)
            continue
        patched = copy.deepcopy(schema)
        params = patched["function"]["parameters"] if "function" in patched else patched["inputSchema"]
        domain_prop = params["properties"]["domain"]
        domain_prop["enum"] = tool.domains_for_services(uno_services)
        out.append(patched)
    return out
