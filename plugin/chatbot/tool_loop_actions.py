"""Effect interpreter for the sidebar tool-calling loop.

``tool_loop_state.next_state`` stays pure: it returns effect descriptions.
This module is the command boundary where those descriptions touch UI,
session history, workers, tools, and document context.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import traceback
from typing import Any, Callable, Protocol

from plugin.chatbot.tool_loop_state import (
    DELEGATE_GATEWAY_TOOL_NAMES,
    AddMessageEffect,
    ExitLoopEffect,
    LogAgentEffect,
    SpawnFinalStreamEffect,
    SpawnLLMWorkerEffect,
    SpawnToolWorkerEffect,
    ToolLoopUIEffect,
    TriggerNextToolEffect,
    UpdateActivityStateEffect,
    UpdateDocumentContextEffect,
)
from plugin.framework.async_stream import StreamQueueKind
from plugin.framework.client.model_fetcher import get_text_model, set_native_audio_support
from plugin.framework.config import get_config_bool, get_config_str, get_current_endpoint
from plugin.framework.constants import CHAT_DOCUMENT_CONTEXT_MAX_CHARS, get_chat_system_prompt_for_document
from plugin.framework.errors import ToolExecutionError, UnoObjectError, format_error_payload
from plugin.framework.logging import agent_log, update_activity_state
from plugin.framework.tool import ToolContext
from plugin.framework.worker_pool import run_in_background
from plugin.doc.document_helpers import get_document_context_for_chat

log = logging.getLogger(__name__)


class ToolLoopActionHost(Protocol):
    ctx: Any
    session: Any
    image_model_selector: Any
    audio_wav_path: str | None
    _active_q: Any
    _active_batched_q: Any
    _active_client: Any
    _active_max_tokens: int
    _active_tools: list[dict[str, Any]]
    _active_execute_tool_fn: Callable[..., Any]
    _active_model: Any
    _active_query_text: str | None
    _active_supports_status: bool
    _current_tool_call_id: str | None
    _terminal_status: str

    def _append_response(self, text: str, is_thinking: bool = False, role: str = "assistant") -> None: ...
    def _set_status(self, text: str) -> None: ...
    def _get_document_model(self) -> Any: ...
    def _refresh_active_tools_for_session(self) -> None: ...
    def _spawn_llm_worker(self, q: Any, client: Any, max_tokens: int, tools: list[dict[str, Any]], round_num: int, query_text: str | None = None) -> None: ...
    def _spawn_final_stream(self, q: Any, client: Any, max_tokens: int) -> None: ...
    def resolve_stop_checker(self) -> Callable[[], bool]: ...


def build_tool_execute_fn(
    host: Any,
    doc_type_str: str,
    active_domain: Any,
    python_tool_domain: Any,
    set_active_domain: Callable[..., None],
) -> Callable[..., str]:
    """Build the tool executor used by SpawnToolWorkerEffect.

    The returned callable is intentionally independent from the send setup code
    so tests can verify ToolContext wiring and error serialization without
    starting a full sidebar send.
    """

    def execute_fn(name, args, doc, ctx, status_callback=None, append_thinking_callback=None, stop_checker=None):
        from plugin.main import get_tools as _get_tools

        # NOTE: Experimental planning/TodoStore wiring is intentionally
        # commented out. When enabling the hermes-style todo tool,
        # you can attach a session-scoped TodoStore here and expose it
        # via ToolContext.services, e.g.:
        #
        # from plugin.contrib.todo_store import TodoStore
        # if not hasattr(host, "_todo_store"):
        #     host._todo_store = TodoStore()
        # services = dict(_get_tools()._services)
        # services["todo_store"] = host._todo_store
        #
        # and then pass `services=services` into ToolContext below.

        approval_cb: Any = None
        chat_append_cb: Any = None
        safe_args = args if isinstance(args, dict) else {}

        delegate_domain = str(safe_args.get("domain") or "") if name in DELEGATE_GATEWAY_TOOL_NAMES else ""
        # Delegate gateways forward domain=web_research to WebResearchTool with the same ctx;
        # they must receive the same HITL wiring as the outer web_research tool.
        needs_web_research_ui = name == "web_research" or delegate_domain == "web_research"
        needs_document_research_ui = delegate_domain == "document_research"
        if needs_web_research_ui or needs_document_research_ui:

            def _sub_agent_chat_append(text):
                aq = getattr(host, "_active_q", None)
                if aq is not None:
                    aq.put((StreamQueueKind.CHUNK, text))
                cid = getattr(host, "_current_tool_call_id", None)
                if cid and hasattr(host, "session") and host.session:
                    if not hasattr(host.session, "tool_streamed_texts"):
                        host.session.tool_streamed_texts = {}
                    if cid not in host.session.tool_streamed_texts:
                        host.session.tool_streamed_texts[cid] = []
                    host.session.tool_streamed_texts[cid].append(text)

            chat_append_cb = _sub_agent_chat_append

            try:
                if needs_web_research_ui and get_config_bool("chatbot.prompt_for_web_research"):

                    def _web_approval(query_for_engine, tool_name, args):
                        q = getattr(host, "_active_q", None)
                        if q is None:
                            log.warning("tool_loop: web_research approval skipped (_active_q missing)")
                            return True
                        event = threading.Event()
                        # Use setattr/getattr to avoid static attribute errors on Event.
                        setattr(event, "approved", False)
                        setattr(event, "query_override", None)
                        q.put((StreamQueueKind.APPROVAL_REQUIRED, query_for_engine, tool_name, event))
                        event.wait()
                        if not getattr(event, "approved", False):
                            q.put((StreamQueueKind.STOPPED,))
                        return (bool(getattr(event, "approved", False)), getattr(event, "query_override", None))

                    approval_cb = _web_approval
            except Exception as ex:
                log.warning("tool_loop: web_research approval setup failed: %s", ex)

        active_page_idx = None
        if doc_type_str in ("draw", "impress"):
            try:
                from plugin.draw.bridge import DrawBridge

                active_page_idx = DrawBridge(doc).get_active_page_index()
            except Exception:
                log.debug("execute_fn: failed to get active page index for %s", doc_type_str)

        cancel_scope = getattr(host, "_send_cancellation", None)

        tctx = ToolContext(
            doc=doc,
            ctx=ctx,
            doc_type=doc_type_str,
            services=_get_tools()._services,
            caller="chat",
            active_page_index=active_page_idx,
            status_callback=status_callback,
            append_thinking_callback=append_thinking_callback,
            stop_checker=stop_checker if stop_checker is not None else host.resolve_stop_checker(),
            approval_callback=approval_cb,
            chat_append_callback=chat_append_cb if (needs_web_research_ui or needs_document_research_ui) else None,
            set_active_domain_callback=set_active_domain,
            active_domain=active_domain,
            python_tool_domain=python_tool_domain,
            send_cancellation=cancel_scope,
            uno_services_supported=getattr(host, "cached_uno_services", None),
        )
        try:
            res = _get_tools().execute(name, tctx, **safe_args)
            return json.dumps(res) if isinstance(res, dict) else str(res)
        except (ToolExecutionError, UnoObjectError) as e:
            tb = traceback.format_exc()
            log.exception("Tool execution failed")
            agent_log("tool_loop.py:execute_fn", "Tool execution failed", data={"type": type(e).__name__, "message": str(e)})
            err_payload = format_error_payload(e)
            if "details" not in err_payload:
                err_payload["details"] = {}
            err_payload["details"]["traceback"] = tb
            return json.dumps(err_payload)
        except Exception as e:
            log.exception("Unexpected tool error")
            tb = traceback.format_exc()
            wrapped_error = ToolExecutionError("Unexpected error executing tool '%s'" % name, code="TOOL_UNEXPECTED_ERROR", details={"tool_name": name, "original_error": str(e), "type": type(e).__name__, "traceback": tb})
            return json.dumps(format_error_payload(wrapped_error))

    return execute_fn


class ToolLoopEffectInterpreter:
    """Execute tool-loop effects against a concrete sidebar host."""

    def __init__(self, host: ToolLoopActionHost):
        self.host = host

    def execute(self, effect: Any) -> bool:
        """Run one effect and return True when the drain loop should exit."""

        host = self.host
        if isinstance(effect, ExitLoopEffect):
            return True
        if isinstance(effect, TriggerNextToolEffect):
            host._active_q.put((StreamQueueKind.NEXT_TOOL,))
        elif isinstance(effect, SpawnFinalStreamEffect):
            host._spawn_final_stream(host._active_batched_q or host._active_q, host._active_client, host._active_max_tokens)
        elif isinstance(effect, UpdateDocumentContextEffect):
            self._refresh_document_context()
        elif isinstance(effect, ToolLoopUIEffect):
            self._execute_ui_effect(effect)
        elif isinstance(effect, LogAgentEffect):
            agent_log(effect.location, effect.message, data=effect.data, hypothesis_id=effect.hypothesis_id)
        elif isinstance(effect, AddMessageEffect):
            self._add_message(effect)
        elif isinstance(effect, SpawnLLMWorkerEffect):
            host._refresh_active_tools_for_session()
            host._spawn_llm_worker(host._active_batched_q or host._active_q, host._active_client, host._active_max_tokens, host._active_tools, effect.round_num, query_text=host._active_query_text)
        elif isinstance(effect, UpdateActivityStateEffect):
            self._update_activity_state(effect)
        elif effect.__class__.__name__ == "CleanupAudioEffect":
            self._cleanup_audio()
        elif isinstance(effect, SpawnToolWorkerEffect):
            self._spawn_tool_worker(effect)
        return False

    def _refresh_document_context(self) -> None:
        host = self.host
        try:
            doc = host._get_document_model() if hasattr(host, "_get_document_model") else None
            if doc:
                max_ctx = CHAT_DOCUMENT_CONTEXT_MAX_CHARS
                doc_text = get_document_context_for_chat(doc, max_ctx, include_end=True, include_selection=True, ctx=host.ctx)
                extra_instructions = get_config_str("additional_instructions")
                base_prompt = get_chat_system_prompt_for_document(doc, extra_instructions, ctx=host.ctx)
                host.session.set_system_context(base_prompt, doc_text)
        except Exception:
            log.debug("Tool loop: failed to refresh document context after mutating tool", exc_info=True)

    def _execute_ui_effect(self, effect: ToolLoopUIEffect) -> None:
        host = self.host
        if effect.kind == "append":
            host._append_response(effect.text)
            if effect.text.startswith("\n[Debug: round="):
                log.warning("Tool loop: no assistant text from model: %s", effect.text.strip())
        elif effect.kind == "status":
            host._set_status(effect.text)
            if effect.text in ("Stopped", "Ready", "Error"):
                host._terminal_status = effect.text
        elif effect.kind == "debug":
            log.debug(effect.text)
        elif effect.kind == "info":
            log.info(effect.text)

    def _add_message(self, effect: AddMessageEffect) -> None:
        if effect.role == "assistant":
            self.host.session.add_assistant_message(content=effect.content, tool_calls=effect.tool_calls, reasoning_replay=effect.reasoning_replay)
        elif effect.role == "tool":
            self.host.session.add_tool_result(effect.call_id, effect.content)

    def _update_activity_state(self, effect: UpdateActivityStateEffect) -> None:
        if effect.action == "tool_execute":
            update_activity_state("tool_execute", round_num=effect.round_num, tool_name=effect.tool_name)
        elif effect.action == "exhausted_rounds":
            update_activity_state("exhausted_rounds")

    def _cleanup_audio(self) -> None:
        host = self.host
        current_model = get_text_model()
        current_endpoint = get_current_endpoint()
        set_native_audio_support(current_model, current_endpoint, supported=True)

        try:
            if host.audio_wav_path:
                os.remove(host.audio_wav_path)
        except Exception:
            pass
        host.audio_wav_path = None

    def _spawn_tool_worker(self, effect: SpawnToolWorkerEffect) -> None:
        host = self.host
        func_name = effect.func_name
        func_args_str = effect.func_args_str
        func_args = effect.func_args
        call_id = effect.call_id
        host._current_tool_call_id = call_id

        image_model_override = host.image_model_selector.getText() if host.image_model_selector else None
        if image_model_override and func_name == "generate_image":
            func_args["image_model"] = image_model_override

        def tool_status_callback(msg):
            host._active_q.put((StreamQueueKind.STATUS, msg))

        if effect.is_async:

            def run_async():
                try:

                    def tool_thinking_callback(msg):
                        host._active_q.put((StreamQueueKind.TOOL_THINKING, msg))

                    if host._active_supports_status:
                        res = host._active_execute_tool_fn(func_name, func_args, host._active_model, host.ctx, status_callback=tool_status_callback, append_thinking_callback=tool_thinking_callback, stop_checker=host.resolve_stop_checker())
                    else:
                        res = host._active_execute_tool_fn(func_name, func_args, host._active_model, host.ctx, stop_checker=host.resolve_stop_checker())
                    host._active_q.put((StreamQueueKind.TOOL_DONE, call_id, func_name, func_args_str, res))
                except Exception as e:
                    host._active_q.put((StreamQueueKind.TOOL_DONE, call_id, func_name, func_args_str, json.dumps(format_error_payload(e))))

            run_in_background(run_async, name=f"tool-async-{func_name}")
        else:
            try:
                if host._active_supports_status:
                    res = host._active_execute_tool_fn(func_name, func_args, host._active_model, host.ctx, status_callback=tool_status_callback)
                else:
                    res = host._active_execute_tool_fn(func_name, func_args, host._active_model, host.ctx)
                host._active_q.put((StreamQueueKind.TOOL_DONE, call_id, func_name, func_args_str, res))
            except Exception as e:
                host._active_q.put((StreamQueueKind.TOOL_DONE, call_id, func_name, func_args_str, json.dumps(format_error_payload(e))))
