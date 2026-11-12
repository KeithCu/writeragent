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

from plugin.framework.errors import is_disposal
from plugin.framework.tool import ToolBase, ToolContext, field_from_tool_arguments, tool_supports_document
from plugin.framework.prompts import (
    attach_sheets_create_completion_instruction,
    build_specialized_sub_agent_instructions,
    DELEGATE_SPECIALIZED_TASK_PARAM_HINT,
    images_specialized_sub_agent_hint,
    python_specialized_sub_agent_hint,
    get_document_research_workflow_hint,
)
from plugin.doc.peer_prompt import (
    annotate_outer_peer_delivery_pending,
    annotate_outer_peer_wait,
    looks_like_peer_work_envelope,
)
from plugin.doc.specialized_catalog import (
    InnerToolCall,
    READ_ONLY_DOMAINS,
    apply_inner_outcome,
    catalog_entries_from_base,
    expects_edits,
    loop_hint,
    start_inner_payload,
)
from plugin.framework.i18n import _
from plugin.chatbot.smol_agent import InnerCallRecorder, run_inner_tool_agent
from plugin.doc.peer_message import PEER_RESULT_TOOL_NAME, PEER_TOOL_NAMES
from plugin.doc.specialized_shapes_context import format_shapes_canvas_context
from plugin.framework.queue_executor import main

log = logging.getLogger("writeragent.specialized")


def _outer_turn_is_peer_work(ctx: ToolContext) -> bool:
    """True when this sidebar's current outer turn is a [Peer work from:…] envelope.

    Nested specializes (ranges/sheets/…) otherwise look "done" to the outer and it
    Ready-s without ``document_research`` → ``send_peer_result``. Probe the live
    panel listener's active turn ``active_query_text`` and last user ``session.messages``
    entry; RuntimeUID lookup is UNO so marshal to the main thread.
    """
    def _probe() -> bool:
        from plugin.doc.live_panels import get_live_panel
        from plugin.framework.uno_context import get_runtime_uid

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
            and self.name in PEER_TOOL_NAMES
            and result.get("status") == "ok"
            and result.get("accepted") is True
        ):
            self._capture.peer_send_invoked = True
            if self.name == PEER_RESULT_TOOL_NAME:
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
        from plugin.doc.document_research import filter_document_research_discovery_tools
        from plugin.doc.peer_message import (
            filter_peer_tools_for_specialized,
            format_peer_catalog,
            list_v1_peers,
        )

        tools = filter_document_research_discovery_tools(tools)
        tools = filter_peer_tools_for_specialized(tools, ctx.ctx, ctx.doc)
        if any(t.name in PEER_TOOL_NAMES for t in tools):
            peer_catalog = format_peer_catalog(list_v1_peers(ctx.ctx, ctx.doc))
        # #673 added list_v1_peers / getRuntimeUID to this hint. Specialized
        # execute is async: gather the catalog here with get_tools, not on
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
    if agent_label == "Calc" and ctx.doc is not None:
        from plugin.calc.analyzer import get_calc_context_for_chat

        hints.append(
            _main_thread_or_warn(
                lambda: "\n\n[SPREADSHEET CONTEXT]\n" + get_calc_context_for_chat(ctx.doc, ctx=ctx.ctx),
                "Calc context",
            )
        )

    hints.append(document_research_hint)
    if domain == "document_research":
        from plugin.doc.document_research import get_open_documents

        open_docs = _main_thread_or_warn(
            lambda: get_open_documents(ctx.ctx, ctx.doc),
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
                # python_tool_domain stays off this schema on purpose. delegate_tool_domains
                # (plugin/doc/python_domain_specialized.py) sets ToolContext.python_tool_domain
                # (delegated domains plus core, including "" during =PY()). A property here would be a second API.
                "task": {"type": "string", "description": DELEGATE_SPECIALIZED_TASK_PARAM_HINT},
            },
            "required": ["domain", "task"],
        }

    def domains_for_services(self, uno_services: frozenset[str]) -> list[str]:
        """Sorted enum domains with a base class that runs on a document supporting *uno_services*.

        Narrow domains by probed UNO services, not doc_type—Draw-only docs must not see Impress-only toolsets.
        """
        assert uno_services, "caller must skip narrowing when services are unknown"
        supported = {
            str(domain)
            for cls in self._special_base_class.__subclasses__()
            if (domain := getattr(cls, "specialized_domain", None))
            and tool_supports_document(cast("ToolBase", cls), doc_type=None, uno_services_supported=uno_services)
        }
        return [d for d in self._domains if d in supported]

    def is_async(self) -> bool:
        """Run in a background thread so the main-thread queue/drain loop isn't blocked."""
        return True

    # Domains whose work is read-only -> a long-running delegation to them must NOT
    # take the per-document mutation lock (it would needlessly serialize research on
    # the same doc). The gateway itself is is_mutation=True for the mutating domains.
    def requires_document_lock(self, arguments: Any = None) -> bool:
        domain = field_from_tool_arguments(arguments, "domain")
        # requires_document_lock runs before schema validation—non-string domain must not hit READ_ONLY_DOMAINS membership.
        if isinstance(domain, str) and domain in READ_ONLY_DOMAINS:
            return False
        return super().requires_document_lock(arguments)

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        domain = kwargs.get("domain")
        task = kwargs.get("task")

        # Non-string domain cannot enter ToolRegistry frozenset lookups—return invalid-domain error directly.
        no_tools = f"No specialized tools found for domain '{domain}'. Ensure the tools are implemented and registered."
        if not isinstance(domain, str):
            return self._tool_error(no_tools)

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

        # get_tools(doc=...) calls doc.supportsService — must not run on the sub-agent worker.
        domain_tools, peer_catalog, document_research_hint = main.call(
            lambda: _gather_domain_tools(ctx, domain)
        )

        if not domain_tools:
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
                    if adapter.name in PEER_TOOL_NAMES:
                        adapter.description = (str(adapter.description or "") + " " + peer_catalog).strip()

            instructions = build_specialized_sub_agent_instructions(
                self._agent_label,
                domain,
                extra=_sub_agent_hints(ctx, domain, self._agent_label, document_research_hint),
            )

            examples_key = f"{self._agent_label.lower()}:{domain}"

            def tool_call_handler(step: Any) -> None:
                if step.name == "sheet_create":
                    cap.create_sheet_ran = True
                # Peer delivery is recorded in _CaptureInstructionAdapter.forward from
                # the tool return, not from this pre-execution call.
                if domain == "document_research" and step.name == "delegate_read_document" and ctx.chat_append_callback:
                    from plugin.chatbot.web_research_chat import document_open_step_chat_text

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
                final_answer_tool_name="specialized_workflow_finished",
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
        """Attach completion instructions and peer communication annotations to the payload.

        The annotation helpers return early unless the payload status is ok. Running
        them on the initial ok shell before apply_inner_outcome ensures their notes
        and instructions are preserved even if the outcome subsequently marks the run
        as incomplete or a tool error.
        """
        # Outer never sees sheet_create's inner-only ok string; forward the same
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
