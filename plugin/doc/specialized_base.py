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
from dataclasses import dataclass, field
import logging
from typing import Any, cast, Type, ClassVar

from plugin.framework.constants import ALL_DOCUMENT_SERVICES, DELEGATE_SUBAGENT_TOOL
from plugin.framework.errors import append_warnings, is_disposal
from plugin.framework.tool import ToolBase, ToolContext, field_from_tool_arguments
from plugin.framework.prompts import (
    attach_sheets_create_completion_instruction,
    build_specialized_sub_agent_instructions,
    DELEGATE_SPECIALIZED_TASK_PARAM_HINT,
    images_specialized_sub_agent_hint,
    PYTHON_ROUTING_HINT,
    python_specialized_sub_agent_hint,
    get_document_research_workflow_hint,
)
from plugin.doc.peer_prompt import (
    annotate_outer_peer_delivery_pending,
    annotate_outer_peer_wait,
    looks_like_peer_work_envelope,
)
from plugin.doc.specialized_catalog import (
    FINISH_TOOL_NAME,
    InnerToolCall,
    READ_ONLY_DOMAINS,
    apply_inner_outcome,
    catalog_entries_from_base,
    chat_tier_domains,
    expects_edits,
    format_specialized_domains_description,
    loop_hint,
    start_inner_payload,
)
from plugin.framework.i18n import _
from plugin.chatbot.smol_agent import InnerCallRecorder, run_inner_tool_agent
from plugin.chatbot.web_research_chat import document_open_step_chat_text
# Module imports, not name imports: tests patch plugin.doc.document_research.*
# and plugin.doc.peer_message.* at the owner, and peer_message.list_v1_peers
# calls document_research too, so one owner-path patch covers this gateway and
# peer_message in the same delegated call.
from plugin.doc import document_research, peer_message
from plugin.doc.live_panels import get_live_panel
from plugin.doc.specialized_shapes_context import format_shapes_canvas_context, summarize_shape_calls
from plugin.framework.queue_executor import main
from plugin.framework.uno_context import get_runtime_uid

log = logging.getLogger("writeragent.specialized")


def _outer_turn_is_peer_work(ctx: ToolContext) -> bool:
    """True when this sidebar's current outer turn is a [Peer work from:…] envelope.

    Nested specializes (ranges/sheets/…) otherwise look "done" to the outer and it
    Ready-s without ``document_research`` → ``peer_send_result``. Probe the live
    panel listener's active turn ``active_query_text`` and last user ``session.messages``
    entry; RuntimeUID lookup is UNO so marshal to the main thread.
    """
    def _probe() -> bool:
        doc = ctx.doc
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
        # Lazy: tool_loop_actions loads the chat send state machine (chat_session,
        # compaction, memory, model_fetcher), which only this probe needs.
        from plugin.chatbot.tool_loop_actions import current_turn

        turn = current_turn(listener)
        if turn is not None and looks_like_peer_work_envelope(getattr(turn, "active_query_text", None)):
            return True
        messages = getattr(getattr(listener, "session", None), "messages", None)
        if not messages:
            return False
        last_user = next((m for m in reversed(list(messages)) if isinstance(m, dict) and m.get("role") == "user"), None)
        return last_user is not None and looks_like_peer_work_envelope(last_user.get("content"))

    try:
        return bool(main.call(_probe))
    except Exception as e:
        # CancelledError stringifies to ""; %r keeps the class in the warning.
        log.warning("peer-work turn probe failed: %r", e)
        return False


def _main_thread_or_warn(fn: Any, what: str) -> Any:
    """Marshal fn to the main thread. Re-raise real document disposal.

    Re-raise document disposal when building specialized context; other failures keep the empty fallback.
    """
    try:
        return main.call(fn)
    except Exception as e:
        if is_disposal(e):
            raise
        log.warning("Failed to get %s for sub-agent: %s", what, e)
        return ""


@dataclass
class _DelegationCapture:
    """State gathered during the inner agent's execution loop.

    Inner tool results that carry a web-research-style instruction (such as sheet_create)
    are captured so specialized finish can forward them to the outer agent. When an accepted
    inner peer send runs, the outer agent must go Ready rather than continuing with tools.
    """

    calls: list[InnerToolCall] = field(default_factory=list)
    # Forwarded to the outer agent as an instruction payload (e.g. sheet_create).
    captured_tool_results: list[dict[str, Any]] = field(default_factory=list)
    create_sheet_ran: bool = False
    # Outer agent must transition to Ready instead of continuing with tools after peer delivery.
    peer_send_invoked: bool = False
    peer_result_send_invoked: bool = False


class _CaptureInstructionAdapter(InnerCallRecorder):
    """Adapter logging calls and capturing peer send and instruction results."""

    _capture: _DelegationCapture
    _domain: str

    def __init__(
        self,
        tool: ToolBase,
        ctx: ToolContext,
        capture: _DelegationCapture,
        domain: str,
    ) -> None:
        super().__init__(tool, ctx, capture.calls)
        self._capture = capture
        self._domain = domain

    def forward(self, *args: Any, **kwargs: Any) -> Any:
        result = super().forward(*args, **kwargs)
        if not isinstance(result, dict):
            return result
        if result.get("instruction"):
            self._capture.captured_tool_results.append(result)
        # Accepted delivery only. PEER_NOT_FOUND, PEER_SIDEBAR_NOT_OPEN,
        # PEER_QUEUE_FULL, and PEER_NOT_CHAT_MODE leave both flags false.
        if (
            self._domain == "document_research"
            and self.name in peer_message.PEER_TOOL_NAMES
            and result.get("status") == "ok"
            and result.get("accepted") is True
        ):
            self._capture.peer_send_invoked = True
            if self.name == peer_message.PEER_RESULT_TOOL_NAME:
                self._capture.peer_result_send_invoked = True
        return result


def _gather_domain_tools(ctx: ToolContext, domain: str) -> tuple[list[ToolBase], str, str]:
    """Gather tools for the requested domain (same rules as main chat ``active_domain``).

    Must use ``ToolRegistry.get_tools(..., active_domain=...)`` so cross-app tools
    (e.g. ``RunVenvPythonScript`` with ``specialized_cross_cutting``) are included;
    ``isinstance(..., _special_base_class)`` alone misses Calc-registered tools on Writer delegate.
    """
    registry = ctx.services.get("tools")
    tools = registry.get_tools(
        doc=ctx.doc,
        active_domain=domain,
        exclude_tiers=(),
        ctx=ctx.ctx,
    )
    peer_catalog = ""
    document_research_hint = ""
    if domain == "document_research":
        tools = document_research.filter_document_research_discovery_tools(tools)
        tools = peer_message.filter_peer_tools_for_specialized(tools, ctx.ctx, ctx.doc)
        if any(t.name in peer_message.PEER_TOOL_NAMES for t in tools):
            peer_catalog = peer_message.format_peer_catalog(peer_message.list_v1_peers(ctx.ctx, ctx.doc))
        # The hint reads list_v1_peers / getRuntimeUID (UNO calls). Specialized
        # execute is async, so gather it here next to get_tools, not on
        # the tool-async worker.
        document_research_hint = get_document_research_workflow_hint(
            ctx.ctx, ctx.doc
        )
    return tools, peer_catalog, document_research_hint


def _sub_agent_hints(ctx: ToolContext, domain: str, agent_label: str, document_research_hint: str) -> str:
    """Build supplemental prompt hints for the sub-agent based on domain and context."""
    hints: list[str] = []
    hint = loop_hint(domain, agent_label)
    if hint:
        hints.append(" " + hint)

    # Page size stays on the shapes loop (and the inner domain agent when
    # shapes is requested). The outer python agent passes layout in task.
    if domain == "shapes":
        hints.append(
            _main_thread_or_warn(
                lambda: format_shapes_canvas_context(ctx.doc),
                "shapes canvas",
            )
        )

    # Identity only: truthiness on a guard-proxied doc trips UNO bool on the MCP/long-running
    # worker. The spreadsheet read stays inside the lambda, on the main thread.
    if agent_label == "Calc" and ctx.doc is not None and domain != "mcp":
        # Cross-app import: only a Calc delegate loads the calc package.
        from plugin.calc.analyzer import get_calc_context_for_chat

        hints.append(
            _main_thread_or_warn(
                lambda: "\n\n[SPREADSHEET CONTEXT]\n" + get_calc_context_for_chat(ctx.doc, ctx=ctx.ctx),
                "Calc context",
            )
        )

    hints.append(document_research_hint)
    if domain == "document_research":
        open_docs = _main_thread_or_warn(
            lambda: document_research.get_open_documents(ctx.ctx, ctx.doc),
            "open documents",
        )
        if open_docs:
            hints.append(
                "\n\n[OPEN DOCUMENTS CONTEXT]\n"
                "Note: These are the currently open files in LibreOffice. "
                "Some of these files may be completely unrelated to the task at hand:\n"
                + "\n".join(
                    f"- {d['path'] or d['url'] or 'Untitled'} [{d['doc_type']}]"
                    f"{' (Active)' if d['is_active'] else ''}"
                    for d in open_docs
                )
            )

    if domain == "images":
        hints.append(images_specialized_sub_agent_hint())
    if domain == "python":
        hints.append(python_specialized_sub_agent_hint(agent_label))

    return "".join(hints)


class DelegateToSpecializedBase(ToolBase):
    """Shared base for tools that delegate tasks to specialized sub-agents."""

    # Subclasses MUST override these
    _special_base_class: ClassVar[Type[ToolBase]]
    _agent_label: ClassVar[str]  # e.g., "Writer", "Calc", "Draw"

    is_mutation: bool | None = True
    long_running: bool = True

    def __init__(self) -> None:
        super().__init__()
        # ctx=None and for_discovery=False: same exclusions as the chat enum.
        # for_discovery=True would put Calc-hidden domains back into the schema.
        self._domains: list[str] = sorted({
            e["domain"]
            for e in catalog_entries_from_base(
                agent_label=self._agent_label,
                ctx=None,
                for_discovery=False,
            )
        })

        self.parameters: dict[str, Any] | None = {
            "type": "object",
            "properties": {
                "domain": {"type": "string", "enum": self._domains, "description": "The specialized domain to activate."},
                # python_tool_domain stays off this schema on purpose. python_delegate_domains
                # (plugin/doc/python_domain_specialized.py) sets ToolContext.python_tool_domain
                # (delegated domains plus core, including "" during =PY()). A property here would be a second API.
                "task": {"type": "string", "description": DELEGATE_SPECIALIZED_TASK_PARAM_HINT},
            },
            "required": ["domain", "task"],
        }

    def agent_label_for(
        self,
        *,
        doc_type: str | None = None,
        uno_services: frozenset[str] | None = None,
        doc: Any = None,
    ) -> str:
        """Return the sub-agent label ('Writer', 'Calc', or 'Draw') for this target."""
        return self._agent_label

    def domains_for_services(self, uno_services: frozenset[str]) -> list[str]:
        """Sorted enum domains with a base class that runs on a document supporting *uno_services*.

        Narrow domains by probed UNO services, not doc_type—Draw-only docs must not see Impress-only toolsets.
        """
        assert uno_services, "caller must skip narrowing when services are unknown"
        agent_label = self.agent_label_for(uno_services=uno_services)
        bound = {e["domain"] for e in catalog_entries_from_base(agent_label=agent_label, uno_services=uno_services)}
        return [d for d in self._domains if d in bound]

    def description_for_services(self, uno_services: frozenset[str], *, for_mcp: bool = False) -> str | None:
        """Optional narrowed tool description for *uno_services* (None keeps the class description)."""
        return None

    def is_async(self) -> bool:
        """Run in a background thread so the main-thread queue/drain loop isn't blocked."""
        return True

    # A delegation to a read-only domain never changes the active document.
    # It skips the MCP per-document gate, and in the sidebar it does not
    # re-read the document context.
    # Called before schema validation (MCP prepare, sidebar TOOL_DONE), so a
    # non-string domain must not reach the frozenset membership test (unhashable
    # values raise TypeError); it falls through to the mutating default.
    def detects_mutation(self, arguments: Any = None) -> bool:
        domain = field_from_tool_arguments(arguments, "domain")
        if isinstance(domain, str) and domain in READ_ONLY_DOMAINS:
            return False
        return super().detects_mutation(arguments)

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        domain = kwargs.get("domain")
        task = kwargs.get("task")

        # Non-string domain cannot enter ToolRegistry frozenset lookups—return invalid-domain error directly.
        no_tools = f"No specialized tools found for domain '{domain}'. Ensure the tools are implemented and registered."
        if not isinstance(domain, str):
            return self._tool_error(no_tools)

        # The web_research, vision and embeddings imports below stay lazy: each pulls a
        # heavy or optional stack (web cache, the OCR venv probe, the indexer's
        # subprocess and IPC) that only its own domain uses.
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

            if ctx.status_callback:
                ctx.status_callback(_("Running local OCR on selected image(s)..."))
            insert = ctx.doc_type not in ("draw", "impress")
            return ExtractStructureFromImage().execute(ctx, insert_into_document=insert)

        if domain == "document_research":
            try:
                from plugin.embeddings.embeddings_indexer import enqueue_folder_index

                # Sub-agent runs on a worker thread; this main.call is the UNO listing inside enqueue_folder_index. Stale-schema deletion runs on the corpus-index-* thread, not inside the marshal.
                main.call(lambda: enqueue_folder_index(ctx.ctx, ctx.doc))
            except Exception:
                log.debug("embeddings index wakeup failed", exc_info=True)

        if ctx.status_callback:
            ctx.status_callback(f"Delegating to specialized agent ({domain})...")

        mcp_tools: list[ToolBase] = []
        mcp_hint = ""
        if domain == "mcp":
            # Lazy: only this domain needs the MCP client. Listing is network I/O, so it runs on this delegate worker, never in main.call.
            from plugin.mcp_client.client import McpClientError, mcp_domain_tools

            try:
                mcp_tools, mcp_hint = mcp_domain_tools(ctx)
            except McpClientError as e:
                return self._tool_error(str(e), code=e.code)

        # get_tools(doc=...) calls doc.supportsService — must not run on the sub-agent worker.
        domain_tools, peer_catalog, document_research_hint = main.call(
            lambda: _gather_domain_tools(ctx, domain)
        )
        domain_tools = [*mcp_tools, *domain_tools]

        if not any(t.name != FINISH_TOOL_NAME for t in domain_tools):
            # The registry has already filtered this domain's tools by the document's services,
            # so having no domain tool beyond workflow_finished for an enum domain
            # means it is bound to another document type (for example, Impress-only slide domains
            # in a Draw document).
            services = ctx.uno_services_supported
            if services and domain in self._domains:
                available = self.domains_for_services(services)
                if domain not in available:
                    return self._tool_error(
                        f"Specialized domain '{domain}' does not support the current document. Use one of details.available.",
                        code="UNSUPPORTED_DOCUMENT",
                        available=available,
                    )
            return self._tool_error(no_tools)

        # Specialized execute must see document_research so peer send tools'
        # caller guard allows this loop (not only ctx.caller == "chat").
        prev_active_domain = ctx.active_domain
        ctx.active_domain = domain
        cap = _DelegationCapture()

        try:
            smol_tools = [_CaptureInstructionAdapter(t, ctx, cap, domain) for t in domain_tools]
            if peer_catalog:
                for adapter in smol_tools:
                    if adapter.name in peer_message.PEER_TOOL_NAMES:
                        adapter.description = (str(adapter.description or "") + " " + peer_catalog).strip()

            agent_label = self.agent_label_for(
                doc_type=ctx.doc_type,
                uno_services=ctx.uno_services_supported,
                doc=ctx.doc,
            )
            instructions = build_specialized_sub_agent_instructions(
                agent_label,
                domain,
                extra=_sub_agent_hints(ctx, domain, agent_label, document_research_hint) + mcp_hint,
            )

            examples_key = f"{agent_label.lower()}:{domain}"

            def tool_call_handler(step: Any) -> None:
                if step.name == "sheet_create":
                    cap.create_sheet_ran = True
                # Peer delivery is recorded in _CaptureInstructionAdapter.forward from
                # the tool return, not from this pre-execution call.
                if domain == "document_research" and step.name == "research_delegate_read" and ctx.chat_append_callback:
                    val = field_from_tool_arguments(step.arguments, "path_or_name")
                    path_or_name = str(val) if val is not None else ""
                    ctx.chat_append_callback(document_open_step_chat_text(path_or_name))
                if ctx.append_thinking_callback:
                    ctx.append_thinking_callback(f"Running specialized tool: {step.name} with {step.arguments}\n")
                if ctx.status_callback:
                    ctx.status_callback(f"Tool: {step.name}...")

            final_ans = run_inner_tool_agent(
                ctx,
                smol_tools,
                instructions=instructions,
                examples_key=examples_key,
                final_answer_tool_name=FINISH_TOOL_NAME,
                task=cast("str", task),
                stop_message="Specialized task stopped by user.",
                error_prefix="Specialized agent failed",
                tool_call_handler=tool_call_handler,
            )
        finally:
            ctx.active_domain = prev_active_domain

        payload = start_inner_payload(
            final_ans,
            [domain],
            text_message=_("Specialized task ({domain}) completed.").format(domain=domain),
        )
        # Discussion #2029: a user-stop or crash dict that is already status: "error" is left unchanged.
        if payload.get("status") == "error":
            return payload
        payload = self._annotate_payload(payload, ctx, domain, cap)
        return apply_inner_outcome(
            payload,
            final_ans,
            cap.calls,
            expect_edits=expects_edits(domain),
        )

    def _annotate_payload(
        self,
        payload: dict[str, Any],
        ctx: ToolContext,
        domain: str,
        cap: _DelegationCapture,
    ) -> dict[str, Any]:
        """Attach shapes outcome counts, completion instructions, and peer annotations to the payload.

        The annotation helpers return early unless the payload status is ok. Running
        them on the initial ok shell before apply_inner_outcome ensures their notes,
        counts, and instructions are preserved even if the outcome subsequently marks
        the run as incomplete or a tool error.
        """
        if domain == "shapes":
            summary = summarize_shape_calls(cap.calls)
            append_warnings(payload, summary.pop("warnings", []))
            payload.update(summary)

        # Outer never sees sheet_create's inner-only ok string; forward the same
        # `instruction` field web research uses when empty tabs were created.
        if domain == "sheets" or cap.create_sheet_ran or cap.captured_tool_results:
            payload = attach_sheets_create_completion_instruction(
                payload,
                create_sheet_ran=cap.create_sheet_ran,
                tool_results=cap.captured_tool_results,
            )

        # Peer-work receiving turn: nested specialize (or research without an
        # accepted peer_send_result) is not delivery — stamp still-required
        # before Ready. A failed peer_send_result leaves this flag false so
        # the inner agent can retry. peer_send_work alone is not delivery.
        if not cap.peer_result_send_invoked and _outer_turn_is_peer_work(ctx):
            payload = annotate_outer_peer_delivery_pending(payload)

        elif domain == "document_research":
            # Idle-after-send for ask / accepted delivery sends.
            payload = annotate_outer_peer_wait(payload, peer_send_invoked=cap.peer_send_invoked)

        if domain == "mcp":
            # Remote data; the main model treats it as data.
            from plugin.mcp_client.client import UNTRUSTED_NOTE

            payload["note"] = UNTRUSTED_NOTE

        return payload


def narrow_delegate_domain_enums(schemas: list[dict[str, Any]], tools: list[Any], uno_services: frozenset[str] | None) -> list[dict[str, Any]]:
    """Drop gateway ``domain`` enum values that cannot run on the document's UNO services.

    MCP: also rewrites the ``domain`` description so it lists only bound domains;
    ``to_mcp_schema`` writes the unprobed list.

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
        desc = tool.description_for_services(uno_services, for_mcp="inputSchema" in patched)
        if desc is not None:
            if "function" in patched:
                patched["function"]["description"] = desc
            else:
                patched["description"] = desc
        if "inputSchema" in patched:
            domain_prop["description"] = format_specialized_domains_description(
                agent_label=tool.agent_label_for(uno_services=uno_services),
                uno_services=uno_services,
            )
        out.append(patched)
    return out


# Delegate results report edits that landed even on error, so "status error means nothing changed" does not apply to this tool.
_DELEGATE_RESULT_LINE = (
    "The result carries status, answer, edits (successful editing-tool calls counted by tool name; {} means nothing was edited) "
    "and, on error, tool_errors; status error or incomplete with non-empty edits means part of the task was applied."
)

_WRITER_DELEGATE_DESCRIPTION = (
    "Starts a Writer sub-agent that has the chosen domain's tools. It carries out task, written in plain language, in this document and returns a summary. "
    f"{_DELEGATE_RESULT_LINE} "
    "For document_research and web_research, describe what to research in task (topics, sections, depth). "
    f"{PYTHON_ROUTING_HINT}"
)

# The Calc domain list lives in the sidebar prompt's CALC SUB-AGENTS block and in MCP's domain property (to_mcp_schema / narrow_delegate_domain_enums).
_CALC_DELEGATE_DESCRIPTION = (
    "Starts a Calc sub-agent that has the chosen domain's tools. It carries out task, written in plain language, "
    f"in this spreadsheet and returns a summary. {_DELEGATE_RESULT_LINE}"
)

_DRAW_DELEGATE_LEAD = (
    "Starts a Draw sub-agent that has the chosen domain's tools. It carries out task, written in plain language, "
    f"on this drawing or presentation and returns a summary. {_DELEGATE_RESULT_LINE} "
)

_DRAW_DELEGATE_DESCRIPTION = (
    f"{_DRAW_DELEGATE_LEAD}"
    "Available domains: charts, document_research, forms, images, page_comments, python, shapes, slide_masters, tables, vision, web_research, "
    "and (Impress only) headers_footers, slide_layouts, slide_transitions, speaker_notes."
)

_GENERIC_DELEGATE_DESCRIPTION = (
    "Starts a sub-agent that has the chosen domain's tools. It carries out task, written in plain language, "
    f"in this document and returns a summary. {_DELEGATE_RESULT_LINE}"
)


def _format_draw_delegate_description(domains: list[str]) -> str:
    """Build the Draw/Impress delegate tool description listing the exact bound domains."""
    domain_list = ", ".join(domains)
    return f"{_DRAW_DELEGATE_LEAD}Available domains: {domain_list}."


class DelegateToSubagent(DelegateToSpecializedBase):
    """Unified gateway tool to delegate tasks to specialized Writer, Calc, or Draw sub-agents."""

    name: str | None = DELEGATE_SUBAGENT_TOOL
    description: str = _GENERIC_DELEGATE_DESCRIPTION
    uno_services: list[str] | None = list(ALL_DOCUMENT_SERVICES)
    _agent_label: ClassVar[str] = "Writer"

    def __init__(self) -> None:
        super().__init__()
        self._domains_by_agent: dict[str, list[str]] = {
            label: sorted({
                e["domain"]
                for e in catalog_entries_from_base(agent_label=label, ctx=None, for_discovery=False)
            })
            for label in ("Writer", "Calc", "Draw")
        }
        self._domains: list[str] = sorted({d for domains in self._domains_by_agent.values() for d in domains})
        assert self.parameters is not None
        self.parameters["properties"]["domain"]["enum"] = self._domains

    def agent_label_for(
        self,
        *,
        doc_type: str | None = None,
        uno_services: frozenset[str] | None = None,
        doc: Any = None,
    ) -> str:
        """Resolve the sub-agent label ('Writer', 'Calc', or 'Draw') from doc_type or UNO services."""
        if doc_type == "calc" or (uno_services and "com.sun.star.sheet.SpreadsheetDocument" in uno_services):
            return "Calc"
        if doc_type in ("draw", "impress") or (
            uno_services
            and (
                "com.sun.star.drawing.DrawingDocument" in uno_services
                or "com.sun.star.presentation.PresentationDocument" in uno_services
            )
        ):
            return "Draw"
        if doc_type == "writer" or (uno_services and "com.sun.star.text.TextDocument" in uno_services):
            return "Writer"
        if doc is not None and main.is_current():
            from plugin.framework import doc_type as _doc_type

            dt = _doc_type.doc_type_str_from_doc(doc)
            if dt == "calc":
                return "Calc"
            if dt in ("draw", "impress"):
                return "Draw"
        return "Writer"

    def get_description(self, doc_type: str | None = None) -> str:
        """Return the app-specific sub-agent description for *doc_type*."""
        if doc_type == "calc":
            return _CALC_DELEGATE_DESCRIPTION
        if doc_type in ("draw", "impress"):
            return _DRAW_DELEGATE_DESCRIPTION
        if doc_type == "writer":
            return _WRITER_DELEGATE_DESCRIPTION
        return self.description

    def get_parameters(self, doc_type: str | None = None) -> dict[str, Any] | None:
        """Return parameters with the domain enum narrowed for *doc_type* when known."""
        if self.parameters is None or not doc_type:
            return self.parameters
        label = self.agent_label_for(doc_type=doc_type)
        params = copy.deepcopy(self.parameters)
        params["properties"]["domain"]["enum"] = list(self._domains_by_agent.get(label, self._domains))
        return params

    def domains_for_services(self, uno_services: frozenset[str]) -> list[str]:
        """Sorted enum domains for the app and *uno_services*."""
        assert uno_services, "caller must skip narrowing when services are unknown"
        label = self.agent_label_for(uno_services=uno_services)
        allowed = set(self._domains_by_agent.get(label, self._domains))
        bound = {e["domain"] for e in catalog_entries_from_base(agent_label=label, uno_services=uno_services)}
        return [d for d in self._domains if d in allowed and d in bound]

    def description_for_services(self, uno_services: frozenset[str], *, for_mcp: bool = False) -> str | None:
        """Return the app-specific description narrowed to *uno_services*."""
        label = self.agent_label_for(uno_services=uno_services)
        if label == "Calc":
            return _CALC_DELEGATE_DESCRIPTION
        if label == "Draw":
            domains = self.domains_for_services(uno_services)
            if for_mcp:
                hidden = chat_tier_domains()
                domains = [d for d in domains if d not in hidden]
            return _format_draw_delegate_description(domains)
        return _WRITER_DELEGATE_DESCRIPTION
