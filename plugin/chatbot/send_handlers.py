"""SendHandlersMixin: specialized send paths for the chat sidebar.

This mixin is used by SendButtonListener in panel.py and contains
alternate send flows that would otherwise bloat that class:

- Audio transcription fallback
- Direct image generation (Use Image model)
- External agent backends (Aider, Hermes)
- Web research sub-agent
"""

from __future__ import annotations

import os
import json
import threading
import queue
import logging
from typing import TYPE_CHECKING, Protocol, Any, Callable, TypeVar, cast

try:
    from com.sun.star.lang import DisposedException
    from com.sun.star.uno import RuntimeException, Exception as UnoException
    # Common exceptions for UI components that may be disposed during layout/refresh
    UNO_DISPOSED_EXCEPTIONS = (DisposedException, RuntimeException, UnoException)
except ImportError:
    # Fallback for tests without PyUNO
    UNO_DISPOSED_EXCEPTIONS = cast("Any", (Exception,))

from plugin.framework.i18n import _
from plugin.framework.async_stream import StreamQueueKind, run_blocking_in_thread, run_async_worker_with_drain
from plugin.framework.errors import safe_json_loads, format_error_payload, AgentParsingError, ConfigError, NetworkError
from plugin.framework.config import get_api_config, get_config, get_config_int_safe, as_bool
from plugin.framework.client.llm_client import LlmClient
from plugin.framework.constants import get_core_directives, CHAT_DOCUMENT_CONTEXT_MAX_CHARS
from plugin.framework.queue_executor import llm_request_lane
from plugin.doc.document_helpers import get_document_context_for_chat, is_calc, is_draw
from plugin.agent_backend import get_backend
from plugin.agent_backend.registry import normalize_backend_id
from plugin.chatbot.state_machine import SendHandlerState, StartEvent, StreamChunkEvent, StreamDoneEvent, ErrorEvent, StopRequestedEvent, next_state, EffectInterpreter
from plugin.chatbot.dialogs import get_control_text, show_approval_dialog
from plugin.chatbot.config_ui_helpers import update_lru_history
from plugin.framework.tool import ToolContext

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from plugin.chatbot.panel import ChatSession


def _agent_backend_label(adapter: Any, backend_id: str) -> str:
    """Human-readable backend name for errors (ACP backends implement get_display_name())."""
    getter = getattr(adapter, "get_display_name", None)
    if callable(getter):
        try:
            return str(getter())
        except NotImplementedError:
            pass
    return getattr(adapter, "display_name", backend_id)


class SendHandlerHost(Protocol):
    ctx: Any
    client: "LlmClient | None"

    @property
    def stop_requested(self) -> bool: ...

    def resolve_stop_checker(self) -> Callable[[], bool]: ...
    _in_librarian_mode: bool
    _in_brainstorming_mode: bool
    _brainstorming_topic: str
    session: "ChatSession"
    response_control: Any
    status_control: Any
    image_model_selector: Any
    aspect_ratio_selector: Any
    base_size_input: Any
    frame: Any
    audio_wav_path: str | None
    _terminal_status: str
    _current_agent_backend: Any

    def _set_status(self, text: str) -> None: ...
    def _append_response(self, text: str, is_thinking: bool = False, role: str = "assistant") -> None: ...
    def _get_doc_type_str(self, model: Any) -> str: ...
    def begin_inline_web_approval(self, query: str, tool: str, event: Any) -> None: ...
    def rerender_rich_text_session(self) -> None: ...
    _record_assistant_start: bool
    def _run_unified_worker_drain_loop(
        self, q: "queue.Queue[Any]", worker_fn: Callable[[], None], current_state: "SendHandlerState", interpreter: "EffectInterpreter", show_thinking: bool = True, on_stopped_callback: Callable[[], None] | None = None, on_approval_callback: Callable[[Any], None] | None = None
    ) -> None: ...
    def _get_mcp_url(self) -> str | None: ...
    def _do_send_direct_image(self, query_text: str, model: Any) -> None: ...
    def _do_send_via_agent_backend(self, query_text: str, model: Any, doc_type_str: str) -> None: ...
    def _run_librarian(self, query_text: str, model: Any) -> None: ...
    def _run_brainstorming(self, query_text: str, model: Any) -> None: ...
    def _run_web_research(self, query_text: str, model: Any) -> None: ...


class TypedEvent(Protocol):
    approved: bool
    query_override: str | None

    def wait(self, timeout: float | None = None) -> bool: ...
    def set(self) -> None: ...
    def is_set(self) -> bool: ...


T = TypeVar("T", bound="SendHandlersMixin")


class SendHandlersMixin:
    client: LlmClient | None
    audio_wav_path: str | None

    def _transcribe_audio(self: SendHandlerHost, wav_path: str, stt_model: str) -> str:
        """Transcribe audio synchronously using event pumping on the main thread."""


        if not self.client:


            api_config = get_api_config(self.ctx)
            self.client = LlmClient(api_config, self.ctx)

        cl = self.client
        assert cl is not None

        transcribing = _("Transcribing audio...")
        self._set_status(transcribing)
        self._append_response("\n[" + transcribing + "]\n")

        try:
            transcript_text = run_blocking_in_thread(self.ctx, cl.transcribe_audio, wav_path, model=stt_model)
            return transcript_text

        except Exception as e:
            log.exception("Transcription error in _transcribe_audio")
            self._append_response("\n" + _("[Transcription error: {0}]").format(str(e)) + "\n")
            raise e
        finally:


            try:
                os.remove(wav_path)
            except Exception:
                pass
            self.audio_wav_path = None

    def _run_unified_worker_drain_loop(
        self: SendHandlerHost, q: "queue.Queue[Any]", worker_fn: Callable[[], None], current_state: "SendHandlerState", interpreter: "EffectInterpreter", show_thinking: bool = True, on_stopped_callback: Callable[[], None] | None = None, on_approval_callback: Callable[[Any], None] | None = None
    ) -> None:


        def dispatch_event(event):
            nonlocal current_state
            step = next_state(current_state, event)
            current_state = step.state
            interpreter.current_state = current_state
            for eff in step.effects:
                interpreter.interpret(eff)

        def apply_chunk(chunk_text, is_thinking=False):
            if is_thinking and not show_thinking:
                return
            dispatch_event(StreamChunkEvent(chunk_text, is_thinking))

        def on_stream_done(item):
            payload = item[1] if isinstance(item, tuple) and len(item) > 1 else item
            dispatch_event(StreamDoneEvent(payload))

        def on_stopped():
            if on_stopped_callback:
                on_stopped_callback()
            dispatch_event(StopRequestedEvent())

        def on_error(e):
            dispatch_event(ErrorEvent(e))

        def worker_wrapper(worker_q):
            # The worker_fn in this mixin expects to put things directly into q.
            # We already have q, so we just run worker_fn.
            # However, run_async_worker_with_drain creates its own q.
            # We can just ignore the worker_q and use the outer q.
            # BUT, it's better to refactor the workers to use the passed queue.
            worker_fn()

        run_async_worker_with_drain(self.ctx, worker_wrapper, apply_chunk, on_stream_done, on_error, on_status_fn=self._set_status, stop_checker=self.resolve_stop_checker(), on_stopped_fn=on_stopped, name="chatbot-send-handler", q=q)

    def _do_send_direct_image(self: SendHandlerHost, query_text: str, model: Any) -> None:
        interpreter = EffectInterpreter(self)
        current_state = SendHandlerState(handler_type="image", status="ready")

        # 1. State machine transition: start
        step = next_state(current_state, StartEvent(query_text, model, "image"))
        current_state = step.state
        interpreter.current_state = step.state
        for effect in step.effects:
            interpreter.interpret(effect)

    def _execute_direct_image_effect(self: SendHandlerHost, query_text: str, model: Any, current_state: "SendHandlerState", interpreter: "EffectInterpreter") -> None:


        q: queue.Queue[Any] = queue.Queue()

        def run_direct_image():
            try:
                aspect_ratio_str = "Square"
                if self.aspect_ratio_selector and hasattr(self.aspect_ratio_selector, "getText"):
                    aspect_ratio_str = self.aspect_ratio_selector.getText()

                aspect_map = {"Square": "square", "Landscape (16:9)": "landscape_16_9", "Portrait (9:16)": "portrait_9_16", "Landscape (3:2)": "landscape_3_2", "Portrait (2:3)": "portrait_2_3"}
                mapped_aspect = aspect_map.get(aspect_ratio_str, "square")

                image_model_text = ""
                if self.image_model_selector and hasattr(self.image_model_selector, "getText"):
                    image_model_text = self.image_model_selector.getText()

                base_size_val = 512
                if self.base_size_input:
                    if hasattr(self.base_size_input, "getText"):
                        base_size_val = self.base_size_input.getText()
                    elif hasattr(self.base_size_input.getModel(), "Text"):
                        base_size_val = get_control_text(self.base_size_input)
                try:
                    base_size_val = int(base_size_val)
                except (ValueError, TypeError):
                    base_size_val = 512

                from plugin.main import get_tools

                cancel_scope = getattr(self, "_send_cancellation", None)
                tctx = ToolContext(doc=model, ctx=self.ctx, stop_checker=self.resolve_stop_checker(), doc_type="writer", services=get_tools()._services, caller="chat", status_callback=lambda t: q.put((StreamQueueKind.STATUS, t)), send_cancellation=cancel_scope)
                try:


                    update_lru_history(self.ctx, base_size_val, "image_base_size_lru", "")
                except Exception as elru:


                    if isinstance(elru, ConfigError):
                        log.error("LRU update ConfigError: %s" % elru)
                    else:
                        if isinstance(elru, UNO_DISPOSED_EXCEPTIONS):
                            log.debug("LRU update error (likely disposed): %s" % elru)
                        else:
                            log.error("LRU update error: %s" % elru)



                # generate_image is async; UNO is marshalled inside the tool (worker runs HTTP).
                res = get_tools().execute("generate_image", tctx, bypass_thread_guard=False, **{"prompt": query_text, "aspect_ratio": mapped_aspect, "base_size": base_size_val, "image_model": image_model_text})
                if isinstance(res, dict) and res.get("status") == "error":
                    log.error("generate_image (direct) failed: %s details=%s", res.get("message"), res.get("details"))
                result = json.dumps(res) if isinstance(res, dict) else str(res)
                data = safe_json_loads(result, default={})
                if isinstance(data, dict):
                    note = data.get("message", data.get("status", "done"))
                else:
                    log.error("Failed to parse generate_image result in _do_send_direct_image")
                    note = "done"
                q.put((StreamQueueKind.CHUNK, "[generate_image: %s]\n" % note))
                q.put((StreamQueueKind.STREAM_DONE, {}))
            except Exception as e:
                doc_type = self._get_doc_type_str(model).lower() if model else "unknown"
                log.error("Direct image path ERROR in _do_send_direct_image [doc: %s]: %s", doc_type, e)


                q.put((StreamQueueKind.ERROR, format_error_payload(e)))

        self._run_unified_worker_drain_loop(q, run_direct_image, current_state, interpreter)
        if self._terminal_status != "Error":
            self._terminal_status = "Ready"

    def _do_send_via_agent_backend(self: SendHandlerHost, query_text: str, model: Any, doc_type_str: str) -> None:
        """Send via external agent backend (Aider, Hermes). No fallback to built-in on failure."""
        interpreter = EffectInterpreter(self)
        current_state = SendHandlerState(handler_type="agent", status="ready")

        self.session.add_user_message(query_text)

        # 1. State machine transition: start
        step = next_state(current_state, StartEvent(query_text, model, doc_type_str))
        current_state = step.state
        interpreter.current_state = current_state
        for effect in step.effects:
            interpreter.interpret(effect)

    def _execute_agent_backend_effect(self: SendHandlerHost, query_text: str, model: Any, doc_type_str: str, current_state: "SendHandlerState", interpreter: "EffectInterpreter") -> None:


        document_url = ""
        try:
            if model and hasattr(model, "getURL"):
                document_url = str(model.getURL() or "")
        except Exception as e:
            if isinstance(e, UNO_DISPOSED_EXCEPTIONS):
                log.debug("Failed to get document URL for agent backend (likely disposed): %s", e)

        try:
            doc_context = get_document_context_for_chat(model, CHAT_DOCUMENT_CONTEXT_MAX_CHARS, include_end=True, include_selection=True, ctx=self.ctx)
        except Exception as e:
            if isinstance(e, UNO_DISPOSED_EXCEPTIONS):
                log.debug("Failed to build document context for agent backend (likely disposed): %s", e)
            else:
                log.exception("Failed to build document context for agent backend")
            self._append_response("\n" + _("[Document context error: {0}]").format(str(e)) + "\n")
            self._terminal_status = "Error"
            self._set_status(_("Error"))
            return



        backend_id = normalize_backend_id(get_config(self.ctx, "agent_backend.backend_id"))
        adapter = get_backend(backend_id, ctx=self.ctx)
        if not adapter:


            self._append_response("\n" + _("[Agent backend '{0}' not found.]").format(backend_id) + "\n")
            self._terminal_status = "Error"
            self._set_status(_("Error"))
            return
        if not adapter.is_available(self.ctx):


            self._append_response("\n" + _("[Agent backend '{0}' is not available. Check Settings (path, install).]").format(_agent_backend_label(adapter, backend_id)) + "\n")
            self._terminal_status = "Error"
            self._set_status(_("Error"))
            return

        q: queue.Queue[Any] = queue.Queue()
        self._current_agent_backend = adapter
        cancel_scope = getattr(self, "_send_cancellation", None)
        if cancel_scope is not None and hasattr(adapter, "stop"):
            cancel_scope.register_on_cancel(adapter.stop)

        def run_agent():
            try:


                # Lean system prompt for external agents: instructions + MCP connection info
                mcp_url = self._get_mcp_url()

                # Check if MCP is enabled; if so, tell the agent about it.
                mcp_instructions = ""
                if mcp_url and as_bool(get_config(self.ctx, "mcp.mcp_enabled")):
                    mcp_instructions = (
                        f"\n\n[MCP SERVER AVAILABLE]\nA Model Context Protocol (MCP) server is running at: {mcp_url}\nYou can discover and use all LibreOffice tools (Writer, Calc, Draw) via this server.\nTarget the current document by passing the 'X-Document-URL' header: {document_url}\n"
                    )

                core_dirs = get_core_directives(model)
                lean_system_prompt = f"{core_dirs}\n\nYou are currently interacting with a LibreOffice document.\n{mcp_instructions}\nPlease proceed with the user's request."

                # Add optional instructions from settings
                extra = str(get_config(self.ctx, "additional_instructions") or "").strip()
                if extra:
                    lean_system_prompt += "\n\n" + extra

                with llm_request_lane():
                    adapter.send(queue=q, user_message=query_text, document_context=doc_context, document_url=document_url, system_prompt=lean_system_prompt, mcp_url=mcp_url, stop_checker=self.resolve_stop_checker())
            except Exception as e:
                log.exception("Agent backend ERROR in _do_send_via_agent_backend [backend: %s, doc: %s]", backend_id, doc_type_str)


                q.put((StreamQueueKind.ERROR, format_error_payload(e)))
            finally:
                self._current_agent_backend = None

        def on_stopped():
            # Ensure conversation roles alternate user/assistant when stopping an
            # external agent backend mid-response.
            self.session.add_assistant_message(content="No response.")

        def on_approval_required(item):
            # item = ("approval_required", description, tool_name, args, request_id)


            description = item[1] if len(item) > 1 else ""
            tool_name = item[2] if len(item) > 2 else ""
            request_id = item[4] if len(item) > 4 else None

            # Option to auto-approve web research or other tools from external agents
            try:
                prompt_for_research = as_bool(get_config(self.ctx, "chatbot.prompt_for_web_research"))
            except Exception:
                prompt_for_research = True

            if not prompt_for_research:
                approved = True
            else:
                approved = show_approval_dialog(self.ctx, description, tool_name, parent_frame=getattr(self, "frame", None))

            if request_id is not None and hasattr(adapter, "submit_approval"):
                try:
                    adapter.submit_approval(request_id, approved)
                except Exception as e:
                    if isinstance(e, NetworkError):
                        log.debug("NetworkError submitting agent backend approval: %s", e)
                    else:
                        log.debug("Error submitting agent backend approval: %s", e)

        self._run_unified_worker_drain_loop(q, run_agent, current_state, interpreter, on_stopped_callback=on_stopped, on_approval_callback=on_approval_required)
        if self._terminal_status not in ("Error", "Stopped"):
            self._terminal_status = "Ready"
        self._current_agent_backend = None

    def _run_librarian(self: SendHandlerHost, query_text: str, model: Any) -> None:
        """Run the librarian onboarding tool via the sub-agent and stream its result into the response area."""
        interpreter = EffectInterpreter(self)
        current_state = SendHandlerState(handler_type="web", status="ready")  # We can reuse 'web' handler_type or create a new one, but for simplicity, 'web' will dispatch StartEvent

        self._in_librarian_mode = True
        self.session.add_user_message(query_text)

        # 1. State machine transition: start
        step = next_state(current_state, StartEvent(query_text, model, "web"))
        current_state = step.state
        interpreter.current_state = current_state

        # Manually set the run_librarian flag to distinguish from web research in effect execution
        setattr(self, "_active_run_librarian", True)

        for effect in step.effects:
            interpreter.interpret(effect)

    def _run_brainstorming(self: SendHandlerHost, query_text: str, model: Any) -> None:
        """Run the brainstorming sub-agent and stream its result into the response area."""
        interpreter = EffectInterpreter(self)
        current_state = SendHandlerState(handler_type="web", status="ready")

        self._in_brainstorming_mode = True
        self.session.add_user_message(query_text)

        step = next_state(current_state, StartEvent(query_text, model, "web"))
        current_state = step.state
        interpreter.current_state = current_state

        setattr(self, "_active_run_brainstorming", True)

        for effect in step.effects:
            interpreter.interpret(effect)

    def _run_web_research(self: SendHandlerHost, query_text: str, model: Any) -> None:
        """Run the web_research tool via the sub-agent and stream its result into the response area."""
        interpreter = EffectInterpreter(self)
        current_state = SendHandlerState(handler_type="web", status="ready")

        self.session.add_user_message(query_text)

        # 1. State machine transition: start
        step = next_state(current_state, StartEvent(query_text, model, "web"))
        current_state = step.state
        interpreter.current_state = current_state
        for effect in step.effects:
            interpreter.interpret(effect)

    def _execute_web_research_effect(self: SendHandlerHost, query_text: str, model: Any, current_state: "SendHandlerState", interpreter: "EffectInterpreter") -> None:
        from plugin.main import get_tools
        is_librarian = getattr(self, "_active_run_librarian", False)
        if hasattr(self, "_active_run_librarian"):
            delattr(self, "_active_run_librarian")
        is_brainstorming = getattr(self, "_active_run_brainstorming", False)
        if hasattr(self, "_active_run_brainstorming"):
            delattr(self, "_active_run_brainstorming")



        q: queue.Queue[Any] = queue.Queue()
        # Read show_thinking before spawning the thread so apply_chunk can use it
        try:


            show_thinking = as_bool(get_config(self.ctx, "chatbot.show_search_thinking"))
        except (ValueError, TypeError) as e:
            log.debug("Failed to read 'chatbot.show_search_thinking' from config: %s", e)
            show_thinking = False



        from plugin.chatbot.web_research_chat import format_sub_agent_conversation_history

        history_text = format_sub_agent_conversation_history(self.session, current_query=query_text)

        def run_search():
            doc_type = "calc" if is_calc(model) else "draw" if is_draw(model) else "writer"
            cancel_scope = getattr(self, "_send_cancellation", None)
            stop_checker = self.resolve_stop_checker()
            try:
                # If librarian mode, clear active_run_librarian and run librarian

                def status_cb(msg):
                    q.put((StreamQueueKind.STATUS, msg))

                # Always push thinking to the queue so the drain loop stays active
                # (processEventsToIdle fires each iteration). Display is controlled
                # by show_thinking in apply_chunk below.
                def thinking_cb(msg):
                    q.put((StreamQueueKind.THINKING, msg))

                def chat_append_cb(text):
                    q.put((StreamQueueKind.CHUNK, text))

                def approval_cb(query_for_engine, tool_name, args):


                    event = threading.Event()
                    # Use setattr/getattr to avoid static attribute errors on Event
                    setattr(event, "approved", False)
                    setattr(event, "query_override", None)
                    q.put((StreamQueueKind.APPROVAL_REQUIRED, query_for_engine, tool_name, event))
                    event.wait()
                    if not getattr(event, "approved", False):
                        # If the user rejects the search query, do not let the LLM
                        # keep going without the data it requested. Instead, immediately
                        # halt the entire tool call loop, acting exactly as if the
                        # user clicked the explicit 'Stop' button in the UI.
                        q.put((StreamQueueKind.STOPPED,))
                    return (bool(getattr(event, "approved", False)), getattr(event, "query_override", None))

                tctx = ToolContext(doc=model, ctx=self.ctx, stop_checker=stop_checker, doc_type=doc_type, services=get_tools()._services, caller="chat", status_callback=status_cb, append_thinking_callback=thinking_cb, approval_callback=approval_cb, chat_append_callback=chat_append_cb, send_cancellation=cancel_scope)

                if is_librarian:
                    res = get_tools().execute("librarian_onboarding", tctx, bypass_thread_guard=False, **{"query": query_text, "history_text": history_text})
                    result = json.dumps(res) if isinstance(res, dict) else str(res)

                    data = safe_json_loads(result)
                    if not isinstance(data, dict):
                        log.error("Failed to parse librarian result in _run_librarian [doc: %s]", doc_type)
                        parsed_err = AgentParsingError("Invalid JSON from librarian tool.", details={"raw_result": result})
                        data = format_error_payload(parsed_err)

                    if data.get("status") == "ok":
                        answer = data.get("result", "")
                        if not isinstance(answer, str):
                            answer = str(answer)
                        self._record_assistant_start = True
                        q.put((StreamQueueKind.CHUNK, answer + "\n"))
                        self.session.add_assistant_message(content=answer)
                    elif data.get("status") == "switch_mode":
                        # We want to exit librarian flow on the next turn.
                        self._in_librarian_mode = False

                        answer = data.get("result", _("Perfect! I'm switching you to the main assistant now."))
                        self._record_assistant_start = True
                        q.put((StreamQueueKind.CHUNK, answer + "\n"))
                        self.session.add_assistant_message(content=answer)
                    else:
                        self._in_librarian_mode = False

                        msg = data.get("message", _("Unknown librarian error."))
                        q.put((StreamQueueKind.CHUNK, "\n" + _("[Librarian error: {0}]").format(msg) + "\n"))

                    q.put((StreamQueueKind.STREAM_DONE, {}))
                elif is_brainstorming:
                    topic = getattr(self, "_brainstorming_topic", "") or ""
                    res = get_tools().execute(
                        "brainstorming_session",
                        tctx,
                        bypass_thread_guard=False,
                        **{"query": query_text, "history_text": history_text, "topic": topic},
                    )
                    result = json.dumps(res) if isinstance(res, dict) else str(res)

                    data = safe_json_loads(result)
                    if not isinstance(data, dict):
                        log.error("Failed to parse brainstorming result [doc: %s]", doc_type)
                        parsed_err = AgentParsingError("Invalid JSON from brainstorming tool.", details={"raw_result": result})
                        data = format_error_payload(parsed_err)

                    if data.get("status") == "ok":
                        answer = data.get("result", "")
                        if not isinstance(answer, str):
                            answer = str(answer)
                        self._record_assistant_start = True
                        q.put((StreamQueueKind.CHUNK, answer + "\n"))
                        self.session.add_assistant_message(content=answer)
                    elif data.get("status") == "finished":
                        finished_cb = getattr(self, "on_brainstorming_session_finished", None)
                        if callable(finished_cb):
                            finished_cb()
                        else:
                            self._in_brainstorming_mode = False
                        answer = data.get("result", _("Brainstorming complete."))
                        if not isinstance(answer, str):
                            answer = str(answer)
                        self._record_assistant_start = True
                        q.put((StreamQueueKind.CHUNK, answer + "\n"))
                        self.session.add_assistant_message(content=answer)
                    else:
                        self._in_brainstorming_mode = False
                        msg = data.get("message", _("Unknown brainstorming error."))
                        q.put((StreamQueueKind.CHUNK, "\n" + _("[Brainstorming error: {0}]").format(msg) + "\n"))

                    q.put((StreamQueueKind.STREAM_DONE, {}))
                else:
                    res = get_tools().execute("web_research", tctx, bypass_thread_guard=False, **{"query": query_text, "history_text": history_text})
                    result = json.dumps(res) if isinstance(res, dict) else str(res)

                    data = safe_json_loads(result)
                    if not isinstance(data, dict):
                        log.error("Failed to parse web_research result in _run_web_research [doc: %s]", doc_type)
                        parsed_err = AgentParsingError("Invalid JSON from web search tool.", details={"raw_result": result})
                        data = format_error_payload(parsed_err)

                    if data.get("status") == "ok":
                        from plugin.chatbot.web_research_chat import format_research_cache_result_chat

                        answer = data.get("result", "")
                        if not isinstance(answer, str):
                            answer = str(answer)
                        cache_block = format_research_cache_result_chat(data)
                        self._record_assistant_start = True
                        q.put((StreamQueueKind.CHUNK, cache_block + answer + "\n"))
                        self.session.add_assistant_message(content=cache_block + answer)
                    else:
                        msg = data.get("message", _("Unknown research error."))
                        q.put((StreamQueueKind.CHUNK, "\n" + _("[Research error: {0}]").format(msg) + "\n"))

                    q.put((StreamQueueKind.STREAM_DONE, {}))
            except Exception as e:
                log.exception("Web/Librarian path ERROR in _run_web_research [doc: %s]", doc_type)

                q.put((StreamQueueKind.ERROR, format_error_payload(e)))

        def on_approval_required(item):
            # item = ("approval_required", query_for_engine, tool_name, event_obj)
            query_for_engine = item[1] if len(item) > 1 else ""
            tool_name = item[2] if len(item) > 2 else ""
            event_obj = item[3] if len(item) > 3 else None
            if event_obj is not None:
                self.begin_inline_web_approval(query_for_engine, tool_name, event_obj)
            log.info("web_research on_approval_required: tool=%s (inline Accept/Change/Reject)", tool_name)

        self._run_unified_worker_drain_loop(q, run_search, current_state, interpreter, show_thinking=show_thinking, on_approval_callback=on_approval_required)

        from plugin.chatbot.rich_text import finalize_sidebar_assistant_response

        finalize_sidebar_assistant_response(self)

    def _get_mcp_url(self: SendHandlerHost) -> str | None:
        """Construct the local MCP streamable-HTTP endpoint URL from config."""
        try:
            from plugin.mcp.server import mcp_endpoint_url

            port = get_config_int_safe(self.ctx, "mcp.mcp_port")
            # MCP binds localhost only (mcp/module.yaml); host/ssl are not user settings.
            return mcp_endpoint_url("localhost", port, False)
        except (ValueError, TypeError) as e:
            log.debug("Failed to read MCP config: %s", e)
            return None
