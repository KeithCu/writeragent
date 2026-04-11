"""SendHandlersMixin: specialized send paths for the chat sidebar.

This mixin is used by SendButtonListener in panel.py and contains
alternate send flows that would otherwise bloat that class:

- Audio transcription fallback
- Direct image generation (Use Image model)
- External agent backends (Aider, Hermes)
- Web research sub-agent
"""

from __future__ import annotations

import queue
import logging
import json
from typing import TYPE_CHECKING, Protocol, Any, Callable, TypeVar

if TYPE_CHECKING:
    import threading
    from plugin.modules.http.client import LlmClient
    from plugin.modules.chatbot.panel import ChatSession
    from plugin.modules.chatbot.state_machine import SendHandlerState, EffectInterpreter


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
    stop_requested: bool
    _in_librarian_mode: bool
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
    def _append_response(self, text: str, is_thinking: bool = False) -> None: ...
    def _get_doc_type_str(self, model: Any) -> str: ...
    def begin_inline_web_approval(self, query: str, tool: str, event: Any) -> None: ...
    def _run_unified_worker_drain_loop(
        self,
        q: "queue.Queue[Any]",
        worker_fn: Callable[[], None],
        current_state: "SendHandlerState",
        interpreter: "EffectInterpreter",
        show_thinking: bool = True,
        on_stopped_callback: Callable[[], None] | None = None,
        on_approval_callback: Callable[[Any], None] | None = None
    ) -> None: ...
    def _get_mcp_url(self) -> str | None: ...
    def _do_send_direct_image(self, query_text: str, model: Any) -> None: ...
    def _do_send_via_agent_backend(self, query_text: str, model: Any, doc_type_str: str) -> None: ...
    def _run_librarian(self, query_text: str, model: Any) -> None: ...
    def _run_web_research(self, query_text: str, model: Any) -> None: ...

class TypedEvent(Protocol):
    approved: bool
    query_override: str | None
    def wait(self, timeout: float | None = None) -> bool: ...
    def set(self) -> None: ...
    def is_set(self) -> bool: ...

T = TypeVar("T", bound="SendHandlersMixin")

from plugin.framework.async_stream import StreamQueueKind
from plugin.framework.errors import safe_json_loads
from plugin.modules.chatbot.state_machine import (
    SendHandlerState, StartEvent, StreamChunkEvent, StreamDoneEvent,
    ErrorEvent, StopRequestedEvent, next_state, EffectInterpreter,
    SpawnAudioWorkerEffect, SpawnDirectImageEffect, SpawnAgentWorkerEffect,
    SpawnWebWorkerEffect, ProceedToChatEffect
)

log = logging.getLogger(__name__)


class SendHandlersMixin:
    client: LlmClient | None
    audio_wav_path: str | None

    def _transcribe_audio(self: SendHandlerHost, wav_path: str, stt_model: str) -> str:
        """Transcribe audio synchronously using event pumping on the main thread."""
        from plugin.framework.async_stream import run_blocking_in_thread
        from plugin.framework.i18n import _

        if not self.client:
            from plugin.framework.config import get_api_config
            from plugin.modules.http.client import LlmClient

            api_config = get_api_config(self.ctx)
            self.client = LlmClient(api_config, self.ctx)

        cl = self.client
        assert cl is not None

        self._set_status(_("Transcribing audio..."))
        self._append_response("\n" + _("[Transcribing audio...]") + "\n")

        try:
            transcript_text = run_blocking_in_thread(
                self.ctx, cl.transcribe_audio, wav_path, model=stt_model
            )
            return transcript_text

        except Exception as e:
            log.error("Transcription error in _transcribe_audio: %s", e)
            self._append_response("\n" + _("[Transcription error: {0}]").format(str(e)) + "\n")
            raise e
        finally:
            import os
            try:
                os.remove(wav_path)
            except Exception:
                pass
            self.audio_wav_path = None

    def _run_unified_worker_drain_loop(
        self: SendHandlerHost,
        q: "queue.Queue[Any]",
        worker_fn: Callable[[], None],
        current_state: "SendHandlerState",
        interpreter: "EffectInterpreter",
        show_thinking: bool = True,
        on_stopped_callback: Callable[[], None] | None = None,
        on_approval_callback: Callable[[Any], None] | None = None
    ) -> None:
        from plugin.framework.worker_pool import run_in_background
        from plugin.framework.async_stream import run_stream_drain_loop
        from plugin.framework.i18n import _

        job_done = [False]
        run_in_background(worker_fn)

        try:
            toolkit = self.ctx.getServiceManager().createInstanceWithContext(
                "com.sun.star.awt.Toolkit", self.ctx
            )
        except Exception as e:
            from com.sun.star.lang import DisposedException
            from com.sun.star.uno import RuntimeException, Exception as UnoException
            if isinstance(e, (DisposedException, RuntimeException, UnoException)):
                log.debug("Failed to create Toolkit for stream drain loop (likely disposed): %s", e)
            else:
                log.error("Failed to create Toolkit for stream drain loop: %s", e)
            self._append_response("\n" + _("[Error: {0}]").format(str(e)) + "\n")
            self._terminal_status = "Error"
            if hasattr(self, "_current_agent_backend"):
                self._current_agent_backend = None
            return

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

        def on_stream_done(response):
            job_done[0] = True
            dispatch_event(StreamDoneEvent(response))
            return True

        def on_stopped():
            if on_stopped_callback:
                on_stopped_callback()
            dispatch_event(StopRequestedEvent())
            job_done[0] = True

        def on_error(e):
            dispatch_event(ErrorEvent(e))

        run_stream_drain_loop(
            q,
            toolkit,
            job_done,
            apply_chunk,
            on_stream_done=on_stream_done,
            on_stopped=on_stopped,
            on_error=on_error,
            on_status_fn=self._set_status,
            ctx=self.ctx,
            stop_checker=lambda: self.stop_requested,
            on_approval_required=on_approval_callback,
        )

    def _do_send_direct_image(self: SendHandlerHost, query_text: str, model: Any) -> None:
        interpreter = EffectInterpreter(self)
        current_state = SendHandlerState(handler_type="image", status="ready")

        # 1. State machine transition: start
        step = next_state(current_state, StartEvent(query_text, model, "image"))
        current_state = step.state
        interpreter.current_state = step.state
        for effect in step.effects:
            interpreter.interpret(effect)

    def _execute_direct_image_effect(
        self: SendHandlerHost, query_text: str, model: Any, current_state: "SendHandlerState", interpreter: "EffectInterpreter"
    ) -> None:
        from plugin.framework.dialogs import get_control_text
        q: queue.Queue[Any] = queue.Queue()
        job_done = [False]

        def run_direct_image():
            try:
                aspect_ratio_str = "Square"
                if self.aspect_ratio_selector and hasattr(
                    self.aspect_ratio_selector, "getText"
                ):
                    aspect_ratio_str = self.aspect_ratio_selector.getText()

                aspect_map = {
                    "Square": "square",
                    "Landscape (16:9)": "landscape_16_9",
                    "Portrait (9:16)": "portrait_9_16",
                    "Landscape (3:2)": "landscape_3_2",
                    "Portrait (2:3)": "portrait_2_3",
                }
                mapped_aspect = aspect_map.get(aspect_ratio_str, "square")

                image_model_text = ""
                if self.image_model_selector and hasattr(
                    self.image_model_selector, "getText"
                ):
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
                from plugin.framework.tool_context import ToolContext

                tctx = ToolContext(
                    doc=model,
                    ctx=self.ctx,
                    stop_checker=lambda: self.stop_requested,
                    doc_type="writer",
                    services=get_tools()._services,
                    caller="chat",
                    status_callback=lambda t: q.put((StreamQueueKind.STATUS, t)),
                )
                try:
                    from plugin.framework.config import update_lru_history

                    update_lru_history(
                        self.ctx, base_size_val, "image_base_size_lru", ""
                    )
                except Exception as elru:
                    from plugin.framework.errors import ConfigError
                    if isinstance(elru, ConfigError):
                        log.error("LRU update ConfigError: %s" % elru)
                    else:
                        from com.sun.star.lang import DisposedException
                        from com.sun.star.uno import RuntimeException, Exception as UnoException
                        if isinstance(elru, (DisposedException, RuntimeException, UnoException)):
                            log.debug("LRU update error (likely disposed): %s" % elru)
                        else:
                            log.error("LRU update error: %s" % elru)

                import json

                # generate_image is async; UNO is marshalled inside the tool (worker runs HTTP).
                res = get_tools().execute(
                    "generate_image",
                    tctx,
                    bypass_thread_guard=False,
                    **{
                        "prompt": query_text,
                        "aspect_ratio": mapped_aspect,
                        "base_size": base_size_val,
                        "image_model": image_model_text,
                    },
                )
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
                log.error("Direct image path ERROR in _do_send_direct_image [doc: %s]: %s",
                          doc_type, e)
                from plugin.framework.errors import format_error_payload
                q.put((StreamQueueKind.ERROR, format_error_payload(e)))

        self._run_unified_worker_drain_loop(
            q,
            run_direct_image,
            current_state,
            interpreter
        )
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

    def _execute_agent_backend_effect(
        self: SendHandlerHost, query_text: str, model: Any, doc_type_str: str, current_state: "SendHandlerState", interpreter: "EffectInterpreter"
    ) -> None:
        from plugin.framework.config import get_config, get_config_int
        from plugin.framework.document import get_document_context_for_chat
        from plugin.modules.agent_backend import get_backend

        document_url = ""
        try:
            if model and hasattr(model, "getURL"):
                document_url = str(model.getURL() or "")
        except Exception as e:
            from com.sun.star.lang import DisposedException
            from com.sun.star.uno import RuntimeException, Exception as UnoException
            if isinstance(e, (DisposedException, RuntimeException, UnoException)):
                log.debug("Failed to get document URL for agent backend (likely disposed): %s", e)

        max_context = get_config_int(self.ctx, "chat_context_length")
        try:
            doc_context = get_document_context_for_chat(
                model,
                max_context,
                include_end=True,
                include_selection=True,
                ctx=self.ctx,
            )
        except Exception as e:
            from plugin.framework.i18n import _
            from com.sun.star.lang import DisposedException
            from com.sun.star.uno import RuntimeException, Exception as UnoException
            if isinstance(e, (DisposedException, RuntimeException, UnoException)):
                log.debug("Failed to build document context for agent backend (likely disposed): %s", e)
            else:
                log.error("Failed to build document context for agent backend: %s", e)
            self._append_response("\n" + _("[Document context error: {0}]").format(str(e)) + "\n")
            self._terminal_status = "Error"
            self._set_status(_("Error"))
            return

        from plugin.modules.agent_backend.registry import normalize_backend_id
        backend_id = normalize_backend_id(get_config(self.ctx, "agent_backend.backend_id"))
        adapter = get_backend(backend_id, ctx=self.ctx)
        if not adapter:
            from plugin.framework.i18n import _
            self._append_response("\n" + _("[Agent backend '{0}' not found.]").format(backend_id) + "\n")
            self._terminal_status = "Error"
            self._set_status(_("Error"))
            return
        if not adapter.is_available(self.ctx):
            from plugin.framework.i18n import _
            self._append_response(
                "\n" + _("[Agent backend '{0}' is not available. Check Settings (path, install).]")
                .format(_agent_backend_label(adapter, backend_id)) + "\n"
            )
            self._terminal_status = "Error"
            self._set_status(_("Error"))
            return

        q: queue.Queue[Any] = queue.Queue()
        job_done = [False]
        self._current_agent_backend = adapter

        def run_agent():
            try:
                from plugin.framework.constants import CORE_DIRECTIVES
                from plugin.framework.config import as_bool

                # Lean system prompt for external agents: instructions + MCP connection info
                mcp_url = self._get_mcp_url()
                
                # Check if MCP is enabled; if so, tell the agent about it.
                mcp_instructions = ""
                if mcp_url and as_bool(get_config(self.ctx, "http.mcp_enabled")):
                    mcp_instructions = (
                        f"\n\n[MCP SERVER AVAILABLE]\n"
                        f"A Model Context Protocol (MCP) server is running at: {mcp_url}\n"
                        f"You can discover and use all LibreOffice tools (Writer, Calc, Draw) via this server.\n"
                        f"Target the current document by passing the 'X-Document-URL' header: {document_url}\n"
                    )

                lean_system_prompt = (
                    f"{CORE_DIRECTIVES}\n\n"
                    f"You are currently interacting with a LibreOffice document.\n"
                    f"{mcp_instructions}\n"
                    f"Please proceed with the user's request."
                )
                
                # Add optional instructions from settings
                extra = str(get_config(self.ctx, "additional_instructions") or "").strip()
                if extra:
                    lean_system_prompt += "\n\n" + extra

                adapter.send(
                    queue=q,
                    user_message=query_text,
                    document_context=doc_context,
                    document_url=document_url,
                    system_prompt=lean_system_prompt,
                    mcp_url=mcp_url,
                    stop_checker=lambda: self.stop_requested,
                )
            except Exception as e:
                log.error("Agent backend ERROR in _do_send_via_agent_backend [backend: %s, doc: %s]: %s",
                          backend_id, doc_type_str, e)
                from plugin.framework.errors import format_error_payload
                q.put((StreamQueueKind.ERROR, format_error_payload(e)))
            finally:
                self._current_agent_backend = None

        def on_stopped():
            # Ensure conversation roles alternate user/assistant when stopping an
            # external agent backend mid-response.
            self.session.add_assistant_message(content="No response.")

        def on_approval_required(item):
            # item = ("approval_required", description, tool_name, args, request_id)
            from plugin.framework.dialogs import show_approval_dialog
            from plugin.framework.config import get_config, as_bool

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
                approved = show_approval_dialog(
                    self.ctx, description, tool_name, parent_frame=getattr(self, "frame", None)
                )

            if request_id is not None and hasattr(adapter, "submit_approval"):
                try:
                    adapter.submit_approval(request_id, approved)
                except Exception as e:
                    from plugin.framework.errors import NetworkError
                    if isinstance(e, NetworkError):
                        log.debug("NetworkError submitting agent backend approval: %s", e)
                    else:
                        log.debug("Error submitting agent backend approval: %s", e)

        self._run_unified_worker_drain_loop(
            q,
            run_agent,
            current_state,
            interpreter,
            on_stopped_callback=on_stopped,
            on_approval_callback=on_approval_required
        )
        if self._terminal_status not in ("Error", "Stopped"):
            self._terminal_status = "Ready"
        self._current_agent_backend = None

    def _run_librarian(self: SendHandlerHost, query_text: str, model: Any) -> None:
        """Run the librarian onboarding tool via the sub-agent and stream its result into the response area."""
        interpreter = EffectInterpreter(self)
        current_state = SendHandlerState(handler_type="web", status="ready") # We can reuse 'web' handler_type or create a new one, but for simplicity, 'web' will dispatch StartEvent

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

    def _execute_web_research_effect(
        self: SendHandlerHost, query_text: str, model: Any, current_state: "SendHandlerState", interpreter: "EffectInterpreter"
    ) -> None:
        is_librarian = getattr(self, "_active_run_librarian", False)
        if hasattr(self, "_active_run_librarian"):
            delattr(self, "_active_run_librarian")

        from plugin.modules.http.errors import format_error_message
        from plugin.main import get_tools
        from plugin.framework.document import is_calc, is_draw

        q: queue.Queue[Any] = queue.Queue()
        job_done = [False]
        # Read show_thinking before spawning the thread so apply_chunk can use it
        try:
            from plugin.framework.config import get_config, as_bool

            show_thinking = as_bool(get_config(self.ctx, "chatbot.show_search_thinking"))
        except (ValueError, TypeError) as e:
            log.debug("Failed to read 'chatbot.show_search_thinking' from config: %s", e)
            show_thinking = False

        from plugin.framework.dialogs import get_control_text
        history_text = ""
        if self.response_control and self.response_control.getModel():
            history_text = get_control_text(self.response_control) or ""

        def run_search():
            doc_type = (
                "calc"
                if is_calc(model)
                else "draw"
                if is_draw(model)
                else "writer"
            )
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
                    import threading
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
                    return (
                        bool(getattr(event, "approved", False)),
                        getattr(event, "query_override", None),
                    )

                from plugin.framework.tool_context import ToolContext

                tctx = ToolContext(
                    doc=model,
                    ctx=self.ctx,
                    stop_checker=lambda: self.stop_requested,
                    doc_type=doc_type,
                    services=get_tools()._services,
                    caller="chat",
                    status_callback=status_cb,
                    append_thinking_callback=thinking_cb,
                    approval_callback=approval_cb,
                    chat_append_callback=chat_append_cb,
                )

                import json

                if is_librarian:
                    res = get_tools().execute(
                        "librarian_onboarding",
                        tctx,
                        bypass_thread_guard=False,
                        **{"query": query_text, "history_text": history_text}
                    )
                    result = json.dumps(res) if isinstance(res, dict) else str(res)

                    data = safe_json_loads(result)
                    if not isinstance(data, dict):
                        from plugin.framework.errors import AgentParsingError, format_error_payload
                        log.error("Failed to parse librarian result in _run_librarian [doc: %s]", doc_type)
                        parsed_err = AgentParsingError("Invalid JSON from librarian tool.", details={"raw_result": result})
                        data = format_error_payload(parsed_err)

                    if data.get("status") == "ok":
                        from plugin.framework.i18n import _
                        answer = data.get("result", "")
                        if not isinstance(answer, str):
                            answer = str(answer)
                        msg = _("Librarian: {0}").format(answer) + "\n"
                        q.put((StreamQueueKind.CHUNK, msg))
                        self.session.add_assistant_message(content=msg)
                    elif data.get("status") == "switch_mode":
                        # We want to exit librarian flow on the next turn.
                        self._in_librarian_mode = False
                        from plugin.framework.i18n import _
                        answer = data.get("result", _("Perfect! I'm switching you to the main assistant now."))
                        msg = _("Librarian: {0}").format(answer) + "\n"
                        q.put((StreamQueueKind.CHUNK, msg))
                        self.session.add_assistant_message(content=msg)
                    else:
                        from plugin.framework.i18n import _
                        msg = data.get("message", _("Unknown librarian error."))
                        q.put((StreamQueueKind.CHUNK, "\n" + _("[Librarian error: {0}]").format(msg) + "\n"))

                    q.put((StreamQueueKind.STREAM_DONE, {}))
                else:
                    res = get_tools().execute(
                        "web_research",
                        tctx,
                        bypass_thread_guard=False,
                        **{"query": query_text, "history_text": history_text}
                    )
                    result = json.dumps(res) if isinstance(res, dict) else str(res)

                    data = safe_json_loads(result)
                    if not isinstance(data, dict):
                        from plugin.framework.errors import AgentParsingError, format_error_payload
                        log.error("Failed to parse web_research result in _run_web_research [doc: %s]", doc_type)
                        parsed_err = AgentParsingError("Invalid JSON from web search tool.", details={"raw_result": result})
                        data = format_error_payload(parsed_err)

                    if data.get("status") == "ok":
                        from plugin.framework.i18n import _
                        answer = data.get("result", "")
                        if not isinstance(answer, str):
                            answer = str(answer)
                        msg = _("AI (research): {0}").format(answer) + "\n"
                        q.put((StreamQueueKind.CHUNK, msg))
                        # Persist assistant result to current session
                        self.session.add_assistant_message(content=msg)
                    else:
                        from plugin.framework.i18n import _
                        msg = data.get("message", _("Unknown research error."))
                        q.put((StreamQueueKind.CHUNK, "\n" + _("[Research error: {0}]").format(msg) + "\n"))

                    q.put((StreamQueueKind.STREAM_DONE, {}))
            except Exception as e:
                log.error("Web/Librarian path ERROR in _run_web_research [doc: %s]: %s", doc_type, e)
                from plugin.framework.errors import format_error_payload
                q.put((StreamQueueKind.ERROR, format_error_payload(e)))

        def on_approval_required(item):
            # item = ("approval_required", query_for_engine, tool_name, event_obj)
            query_for_engine = item[1] if len(item) > 1 else ""
            tool_name = item[2] if len(item) > 2 else ""
            event_obj = item[3] if len(item) > 3 else None
            if event_obj is not None:
                self.begin_inline_web_approval(query_for_engine, tool_name, event_obj)
            log.info(
                "web_research on_approval_required: tool=%s (inline Accept/Change/Reject)",
                tool_name,
            )

        self._run_unified_worker_drain_loop(
            q,
            run_search,
            current_state,
            interpreter,
            show_thinking=show_thinking,
            on_approval_callback=on_approval_required
        )

    def _get_mcp_url(self: SendHandlerHost) -> str | None:
        """Construct the local MCP server URL from config."""
        try:
            from plugin.framework.config import get_config
            
            port = get_config(self.ctx, "http.mcp_port") or 8765
            host = get_config(self.ctx, "http.host") or "localhost"
            use_ssl = get_config(self.ctx, "http.use_ssl")
            scheme = "https" if use_ssl else "http"
            return f"{scheme}://{host}:{port}"
        except (ValueError, TypeError) as e:
            log.debug("Failed to read MCP config: %s", e)
            return None
