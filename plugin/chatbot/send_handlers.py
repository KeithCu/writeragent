"""SendHandlersMixin: specialized send paths for the chat sidebar.

Composed onto SendButtonListener in panel.py and contains
alternate send flows that would otherwise bloat that class:

- Audio transcription fallback
- Direct image generation / img2img (sidebar Image mode)
- External agent backends
- Web research sub-agent
"""

from __future__ import annotations

import threading
import logging
from typing import Any, Callable

from plugin.framework.i18n import _
from plugin.framework.stream_batch import StreamQueueKind
from plugin.framework.async_stream import StreamQueue, defer_until_drain_done, run_async_worker_with_drain
from plugin.framework.errors import (
    AgentParsingError,
    BlockingWaitStopped,
    best_effort,
    format_error_for_display,
    format_error_payload,
    is_disposal,
    suppress_disposed,
)
from plugin.framework.config import get_config
from plugin.framework.config_schema import DEFAULT_IMAGE_BASE_SIZE, as_bool
from plugin.framework.prompts import get_core_directives_for_type
from plugin.chatbot.agent_manual import full_manual
from plugin.framework.llm_lanes import llm_request_lane
from plugin.framework.send_cancellation import capture_send_stop, is_cancelled
from plugin.acp import get_backend
from plugin.acp.registry import normalize_backend_id
from plugin.chatbot.tool_loop import _PanelDescriptor
from plugin.chatbot.tool_loop_actions import (
    TurnController,
    turn_session_or_stop,
    abort_turn,
    current_turn,
    persist_assistant_on_turn,
)
from plugin.chatbot.chat_sidebar_mode import (
    CHAT_MODE_WEB_RESEARCH,
    require_sidebar_mode,
    sidebar_mode,
)
from plugin.chatbot.dialogs import get_control_text, show_approval_dialog
from plugin.framework.model_lru import update_lru_history
from plugin.framework.tool import ToolContext
from plugin.doc.visual_helpers import selected_graphic_object

log = logging.getLogger(__name__)


def _direct_image_source_arg(model: Any) -> str | None:
    """Return ``'selection'`` when a document graphic is selected, else ``None``.

    Sidebar Image mode calls ``image_generate`` with no LLM. The tool only
    runs img2img and replaces in place when ``source_image='selection'``.
    The old worker always omitted that arg, so an edit prompt with a
    selected image still text-to-image inserted a new graphic.
    """
    return "selection" if selected_graphic_object(model) is not None else None


def _tool_result_dict(res: Any, tool_label: str = "web search", doc_type: str = "unknown") -> dict[str, Any]:
    from plugin.chatbot.tool_loop_state import as_dict

    data = as_dict(res)
    if data or (isinstance(res, (dict, str, bytes)) and res in ({}, "{}", b"{}")):
        return data
    log.error("Failed to parse %s result [doc: %s]", tool_label, doc_type)
    parsed_err = AgentParsingError(f"Invalid JSON from {tool_label} tool.", details={"raw_result": str(res)})
    return format_error_payload(parsed_err)


def dispatch_sidebar_send(
    panel: Any,
    sidebar_mode_key: str,
    query_text: str,
    model: Any,
    doc_type_label: str,
) -> bool:
    mode = sidebar_mode(sidebar_mode_key)
    if mode is None or mode.kind == "chat":
        return False
    if mode.kind == "image":
        log.debug("_do_send: using image model (direct, level=logging.INFO) — skip chat model")
        panel._send_handlers._do_send_direct_image(query_text, model)
        return True

    if mode.allowed_docs is not None and doc_type_label not in mode.allowed_docs:
        return False
    if mode.topic_policy == "switch":
        if getattr(panel, "_active_mode", "") != sidebar_mode_key or not getattr(panel, "_mode_topic", ""):
            panel._mode_topic = query_text
    elif mode.topic_policy == "fill":
        if not getattr(panel, "_mode_topic", ""):
            panel._mode_topic = query_text
    if mode.dispatch_log:
        log.info(mode.dispatch_log)
    panel._send_handlers._run_specialized(query_text, model, mode=sidebar_mode_key)
    return True


def _send_worker_queues(host: Any) -> tuple[StreamQueue, Any]:
    from plugin.chatbot.tool_loop_actions import begin_send_turn

    # ``_do_send`` starts the turn. A direct handler call with no turn yet
    # starts one before the worker. An aborted turn is not replaced.
    if current_turn(host) is None:
        begin_send_turn(host)
    raw: StreamQueue = StreamQueue()
    turn = current_turn(host)
    if isinstance(turn, TurnController) and turn.alive:
        turn.queue = raw
        turn.batcher = None
    return raw, turn


def _ui_lines_for_handler_error(handler_type: str, err_msg: str) -> str:
    """Append chat line for a send-handler error (string message, not Exception)."""
    if handler_type == "web":
        return f"\n[Research Chat error: {err_msg}]\n"
    return f"\n[Operation failed: {err_msg}]\n"


class SendHandlersMixin:
    _panel: Any = _PanelDescriptor()

    def __init__(self, panel: Any = None) -> None:
        if panel is not None:
            self._panel = panel

    def _run_unified_worker_drain_loop(
        self,
        q: StreamQueue,
        worker_fn: Callable[[], None],
        handler_type: str,
        status: str = "starting",
        show_thinking: bool = True,
        on_stopped_callback: Callable[[], None] | None = None,
        on_approval_callback: Callable[[Any], None] | None = None,
        on_finished: Callable[[], None] | None = None,
    ) -> None:
        agent_parts: list[str] = []

        def apply_chunk(chunk_text: str, is_thinking: bool = False) -> None:
            if is_thinking and not show_thinking:
                return
            # Bugfix: If the handler is already stopped, late chunks arriving after Stop
            # must not be appended to the UI.
            if status == "stopped":
                return
            # External-agent text is persisted once on STREAM_DONE / stop.
            # Writing each chunk from the worker raced the UI drain.
            if handler_type == "agent" and not is_thinking and chunk_text:
                agent_parts.append(chunk_text)
            self._panel._append_response(chunk_text, is_thinking=is_thinking)

        def _finish_specialized_session(payload: dict[str, Any]) -> None:
            key = payload.get("mode_finished")
            if isinstance(key, str):
                self._panel._sidebar_mode.on_mode_finished(key, payload)

        def on_stream_done(item: Any) -> None:
            nonlocal status
            # Stop or a new send already aborted this turn. The worker
            # wrapper may still post STREAM_DONE so the drain unblocks.
            # That sentinel is not a successful answer.
            live = current_turn(self._panel)
            if isinstance(live, TurnController) and not live.alive:
                return
            payload = item[1] if isinstance(item, tuple) and len(item) > 1 else item
            if isinstance(payload, dict):
                # Store the answer before a mode handoff aborts this turn.
                # Web, librarian, brainstorm, writing, PPT, and deep research
                # used to persist on the worker. That write raced Clear.
                if handler_type != "agent":
                    answer = payload.get("assistant_content")
                    if isinstance(answer, str) and answer:
                        persist_assistant_on_turn(self._panel, content=answer)
                _finish_specialized_session(payload)
            if handler_type == "agent":
                text = "".join(agent_parts).strip()
                if text:
                    persist_assistant_on_turn(self._panel, content=text)
            if status not in ("error", "stopped"):
                # Bugfix: Always emit status "Ready" on normal completion, regardless of
                # handler_type string, so unknown/future handler types don't finish with
                # missing status updates.
                status = "done"
                self._panel._terminal_status = "Ready"
                self._panel._set_status("Ready")

        def on_stopped() -> None:
            nonlocal status
            # The turn commits the open row and writes the stop line.
            # Agent still prefers the non-thinking chunks it accumulated.
            # Only agent Stop used to store a row. Web and image pass no
            # on_stopped_callback, and finalize skips rerender after Stop,
            # so the painted partial never lands in session.messages.
            turn = current_turn(self._panel)
            partial = "".join(agent_parts).strip() if handler_type == "agent" else None
            if isinstance(turn, TurnController):
                turn.close_stopped(self._panel, partial)
            if on_stopped_callback:
                on_stopped_callback()
            # Bugfix: A Stop that arrives after StreamDoneEvent rewrote a finished turn
            # from Ready to Stopped (assigns _terminal_status), and a second Stop re-emitted
            # that pair. "error" was already absorbing. "done" and "stopped" were not,
            # unlike chunks and errors after Stop (#1435).
            if status in ("done", "stopped", "error"):
                return
            status = "stopped"
            self._panel._set_status("Stopped")
            self._panel._terminal_status = "Stopped"

        def on_error(e: Exception) -> None:
            nonlocal status
            live = current_turn(self._panel)
            if isinstance(live, TurnController) and not live.alive:
                return
            # Bugfix: If the handler is already stopped or done, late error events arriving from
            # background threads (or a late error after Ready) must not transition status to
            # "error" or append error lines.
            if status in ("stopped", "done", "error"):
                return
            status = "error"
            self._panel._set_status("Error")
            self._panel._terminal_status = "Error"
            err_msg = format_error_for_display(e)
            append_text = _ui_lines_for_handler_error(handler_type, err_msg)
            self._panel._append_response(append_text)
            # Same write for web, agent, and image, using the lines
            # handle_error already shows. The tool loop stores that banner
            # with persist_assistant_on_turn. Painting the banner and storing
            # the user row without this write leaves the next send a hole
            # where the assistant row should be.
            persist_assistant_on_turn(self._panel, append_text)

        turn = current_turn(self._panel)
        if isinstance(turn, TurnController) and turn.queue is None:
            # The drain owns the queue. The worker wrapper only calls put.
            turn.queue = q
        try:
            # worker_fn closes over StreamQueue. Putting on a different object
            # would miss terminal items and drain would queue a second STREAM_DONE.
            run_async_worker_with_drain(
                self._panel.ctx,
                lambda _q: worker_fn(),
                apply_chunk,
                on_stream_done,
                on_error,
                on_status_fn=self._panel._set_status,
                cancel=self._panel._send_cancellation,
                on_stopped_fn=on_stopped,
                name="chatbot-send-handler",
                q=q,
                on_approval_required=on_approval_callback,
            )
        finally:
            def _drain_epilogue() -> None:
                # Workers that outlive the drain must not enqueue. The send
                # drain drops ``_turn`` after it has read the reply to speak.
                # Event-driven drains return before the stream ends; aborting
                # here on the way out would drop the turn while chunks are
                # still landing.
                abort_turn(self._panel)
                if on_finished is not None:
                    on_finished()

            defer_until_drain_done(_drain_epilogue)

    def _do_send_direct_image(self, query_text: str, model: Any) -> None:
        turn_session = turn_session_or_stop(self._panel)
        # Store the user row once here, before the append, like web
        # research; the fold then sees it and only paints. An aborted
        # turn (None) must not spawn the image worker.
        if turn_session is None:
            return
        turn_session.add_user_message(query_text)
        self._panel._append_response(query_text, role="user")
        self._panel._append_response("\n[Using image model (direct).]\n")
        self._panel._append_response("AI: Creating image...\n")
        self._panel._set_status("Creating image...")
        self._execute_direct_image_effect(query_text, model)

    def _execute_direct_image_effect(self, query_text: str, model: Any) -> None:
        turn_session = turn_session_or_stop(self._panel)
        if turn_session is None:
            return
        # The user row was stored by _do_send_direct_image before execution.
        drain_q, turn = _send_worker_queues(self._panel)
        # Probe on the UI thread. The tool re-reads the selection when it
        # executes; this flag only decides whether to request img2img.
        source_image = _direct_image_source_arg(model)
        # getText on these controls, and update_lru_history, used to run inside
        # the image worker. getText is UNO; the LRU write emits config:changed
        # and refreshes sidebar controls, which must stay on the UI thread.
        aspect_ratio_str = "Square"
        if self._panel.aspect_ratio_selector and hasattr(self._panel.aspect_ratio_selector, "getText"):
            aspect_ratio_str = self._panel.aspect_ratio_selector.getText()
        from plugin.chatbot.settings_dialog import IMAGE_ASPECT_RATIO_ENUMS, canonical_aspect_label

        aspect_ratio_str = canonical_aspect_label(str(aspect_ratio_str or ""))
        mapped_aspect = IMAGE_ASPECT_RATIO_ENUMS.get(aspect_ratio_str, "square")
        image_model_text = ""
        if self._panel.image_model_selector and hasattr(self._panel.image_model_selector, "getText"):
            image_model_text = self._panel.image_model_selector.getText()
        base_size_val: int | str = DEFAULT_IMAGE_BASE_SIZE
        if self._panel.base_size_input:
            if hasattr(self._panel.base_size_input, "getText"):
                base_size_val = self._panel.base_size_input.getText()
            elif hasattr(self._panel.base_size_input.getModel(), "Text"):
                base_size_val = get_control_text(self._panel.base_size_input)
        try:
            base_size_int = int(base_size_val)
        except (ValueError, TypeError):
            base_size_int = DEFAULT_IMAGE_BASE_SIZE
        with suppress_disposed("LRU update", logger=log, exc_info=True):
            update_lru_history(base_size_int, "image_base_size_lru", "")

        # Spawn-time scope. The body must not re-read _send_cancellation:
        # Stop clears the field and the next send replaces it.
        cancel_scope = capture_send_stop(self._panel)

        def run_direct_image() -> None:
            doc_type = getattr(self._panel, "cached_doc_type", None) or "writer"
            try:
                from plugin.main import get_tools

                tctx = ToolContext(doc=model, ctx=self._panel.ctx, doc_type=doc_type, services=get_tools()._services, caller="chat", status_callback=lambda t: turn.put((StreamQueueKind.STATUS, t)), send_cancellation=cancel_scope, uno_services_supported=getattr(self._panel, "cached_uno_services", None))

                # generate_image is async; UNO is marshalled inside the tool (worker runs HTTP).
                image_args: dict[str, Any] = {"prompt": query_text, "aspect_ratio": mapped_aspect, "base_size": base_size_int, "image_model": image_model_text}
                if source_image:
                    image_args["source_image"] = source_image
                res = get_tools().execute("image_generate", tctx, bypass_thread_guard=False, **image_args)
                if isinstance(res, dict) and res.get("status") == "error":
                    # An ordinary user Stop is not a failure; keep it out of ERROR.
                    if is_cancelled(cancel_scope):
                        log.debug("generate_image (direct) stopped by user: %s", res.get("message"))
                    else:
                        log.error("generate_image (direct) failed: %s details=%s", res.get("message"), res.get("details"))
                data = _tool_result_dict(res, "image_generate", doc_type)
                note = data.get("message", data.get("status", "done"))
                # This note is the assistant message. Success (and a tool
                # status=error) that puts STREAM_DONE {} never reaches
                # history: on_stream_done stores a non-agent row only when
                # assistant_content is set. Stop and raised errors already
                # store a row.
                note_line = "[image_generate: %s]\n" % note
                turn.put((StreamQueueKind.CHUNK, note_line))
                turn.put((StreamQueueKind.STREAM_DONE, {"assistant_content": note_line.strip()}))
            except Exception as e:
                doc_type = getattr(self._panel, "cached_doc_type", None) or "unknown"
                if is_cancelled(cancel_scope):
                    log.debug("Direct image path cancelled by Stop [doc: %s]: %s", doc_type, e)
                else:
                    log.exception("Direct image path failed in _do_send_direct_image [doc: %s]", doc_type)

                turn.put((StreamQueueKind.ERROR, format_error_payload(e)))

        def _image_ready() -> None:
            # Runs when the drain is really done. The event-driven drain returns
            # to the VCL loop at once, so code placed after the drain call would
            # run before the queued text and terminal slice are flushed.
            #
            # The stop line must follow the final flush, so finalize runs
            # inside this deferred callback. Chat and web call finalize after
            # their drain, which draws the stop line; the image path has to
            # do the same or Image-mode Stop shows no '[Stopped by user]'
            # until a later repaint.
            if self._panel.stop_requested:
                from plugin.chatbot.tool_loop_actions import finalize_sidebar_assistant_response

                finalize_sidebar_assistant_response(self._panel, allow_rerender=False)
            self._finalize_terminal_ready()

        self._run_unified_worker_drain_loop(drain_q, run_direct_image, "image", on_finished=_image_ready)

    def _finalize_terminal_ready(self) -> None:
        if self._panel._terminal_status not in ("Error", "Stopped"):
            self._panel._terminal_status = "Ready"

    def _do_send_via_agent_backend(self, query_text: str, model: Any, doc_type_str: str) -> None:
        """Send via external agent backend. No fallback to built-in on failure."""
        # User row is stored after the backend exists. Writing it here left
        # a user turn with no assistant row when the adapter was missing.
        self._panel._append_response("\n[Using external agent backend.]\n")
        self._panel._append_response("AI: ")
        self._panel._set_status("Starting agent...")
        self._execute_agent_backend_effect(query_text, model, doc_type_str)

    def _retained_agent_backend(self, backend_id: str) -> Any:
        """Keep one CLI on this sidebar until the backend id changes.

        ``get_backend`` always constructs. The slot is this panel so two
        documents do not share one stdio stream. The rules are the
        process-worker slot policy: one live child, drop a dead child,
        shutdown is explicit. This is not a ``BaseProcessWorker``.
        """
        held = getattr(self._panel, "_acp_backend", None)
        held_id = getattr(self._panel, "_acp_backend_id", None)
        if held is not None and held_id == backend_id:
            return held
        if held is not None:
            best_effort(held.shutdown, "ACP shutdown on backend switch")
            self._panel._acp_backend = None
            self._panel._acp_backend_id = None
        adapter = get_backend(backend_id)
        self._panel._acp_backend = adapter
        self._panel._acp_backend_id = backend_id
        return adapter

    def _execute_agent_backend_effect(self, query_text: str, model: Any, doc_type_str: str) -> None:


        document_url = ""
        with suppress_disposed("get document URL for agent backend", logger=log):
            if model and hasattr(model, "getURL"):
                document_url = str(model.getURL() or "")

        turn_session = turn_session_or_stop(self._panel)
        if turn_session is None:
            return

        def _handle_early_error(exception_instance: Exception) -> None:
            self._panel._set_status("Error")
            self._panel._terminal_status = "Error"
            err_msg = format_error_for_display(exception_instance)
            append_text = _ui_lines_for_handler_error("agent", err_msg)
            self._panel._append_response(append_text)
            # Ensure turn is closed out properly, just as _do_send_direct_image does
            defer_until_drain_done(lambda: abort_turn(self._panel))

        try:
            turn_session.refresh_document_context(model, self._panel.ctx)
            doc_context = turn_session.document_context
        except Exception as e:
            if is_disposal(e):
                log.debug("Failed to build document context for agent backend (likely disposed): %s", e)
            else:
                log.exception("Failed to build document context for agent backend")
            _handle_early_error(Exception(_("[Document context error: {0}]").format(str(e))))
            return



        backend_id = normalize_backend_id(get_config("agent_backend.backend_id"))
        # What was wrong: checked `if not adapter` and raised not-found error.
        # Why: an unknown id is already normalized to builtin by normalize_backend_id,
        # so get_backend / _retained_agent_backend always returns an adapter.
        adapter = self._retained_agent_backend(backend_id)
        if not adapter.is_available(self._panel.ctx):
            name = getattr(adapter, "display_name", None)
            label = name if isinstance(name, str) and name else backend_id
            _handle_early_error(ValueError(_("[Agent backend '{0}' is not available. Check Settings (path, install).]").format(label)))
            return

        turn_session.add_user_message(query_text)
        self._panel._append_response(query_text, role="user")

        drain_q, turn = _send_worker_queues(self._panel)
        # Spawn-time scope. run_agent must not re-read _send_cancellation:
        # Stop clears the field and the next send replaces it.
        # prepare_turn publishes this turn's id before Stop can be registered,
        # so a Stop that lands before send() aborts this turn only.
        cancel_scope = capture_send_stop(self._panel)
        if hasattr(adapter, "prepare_turn"):
            adapter.prepare_turn()
        if cancel_scope is not None and hasattr(adapter, "stop"):
            cancel_scope.register_on_cancel(adapter.stop)

        # String-only: classifying the live model hits get_document_type (UNO).
        # run_agent is chatbot-send-handler; do not classify the document here.
        core_dirs = get_core_directives_for_type(doc_type_str or "writer")

        def run_agent() -> None:
            try:


                from plugin.mcp import is_mcp_server_running

                # What was wrong: session/new was given the URL whenever the flag was
                # set, and the session keeps that mcpServers list for the document URL.
                # Why: check is_mcp_server_running() once; if the server is down,
                # pass mcp_url=None and omit MCP instructions from the prompt.
                mcp_running = is_mcp_server_running()
                mcp_url = self._get_mcp_url() if mcp_running else None

                mcp_instructions = ""
                if mcp_url:
                    mcp_instructions = (
                        f"\n\n[MCP SERVER AVAILABLE]\nA Model Context Protocol (MCP) server is running at: {mcp_url}\nYou can discover and use all LibreOffice tools (Writer, Calc, Draw) via this server.\nTarget the current document by passing the 'X-Document-URL' header: {document_url}\n"
                    )

                # Inject the FULL shared manual: the same prompt pieces (constants) that feed the
                # sidebar's hybrid prompt and get_guidance's topics, concatenated by agent_manual
                # WITH the MCP extras (this backend talks to the HTTP server, so e.g. the 429
                # concurrency contract applies here, unlike the in-process sidebar).
                # full_manual(doc_type_str) is string-only — do not classify the document here.
                lean_system_prompt = f"{core_dirs}\n\n{full_manual(doc_type_str or 'writer')}\n\nYou are currently interacting with a LibreOffice document.\n{mcp_instructions}\nPlease proceed with the user's request."

                # Add optional instructions from settings
                extra = str(get_config("additional_instructions") or "").strip()
                if extra:
                    lean_system_prompt += "\n\n" + extra

                def status_cb(t: str) -> None:
                    turn.put((StreamQueueKind.STATUS, t))
                with llm_request_lane(status_callback=status_cb, resume_status=_("Thinking...")):
                    adapter.send(queue=turn, user_message=query_text, document_context=doc_context, document_url=document_url, system_prompt=lean_system_prompt, mcp_url=mcp_url, stop_checker=cancel_scope.is_cancelled if cancel_scope is not None else None)
            except BlockingWaitStopped:
                turn.put((StreamQueueKind.STOPPED,))
            except Exception as e:
                log.exception("Agent backend ERROR in _do_send_via_agent_backend [backend: %s, doc: %s]", backend_id, doc_type_str)
                turn.put((StreamQueueKind.ERROR, format_error_payload(e)))

        def on_approval_required(item: Any) -> None:
            # item = ("approval_required", description, tool_name, args, request_id)


            description = item[1] if len(item) > 1 else ""
            tool_name = item[2] if len(item) > 2 else ""
            request_id = item[4] if len(item) > 4 else None

            # Option to auto-approve tools from external agents
            prompt_for_permission = as_bool(get_config("agent_backend.prompt_for_permission"))

            if self._panel.stop_requested:
                approved = False
            elif not prompt_for_permission:
                approved = True
            else:
                try:
                    approved = show_approval_dialog(self._panel.ctx, description, tool_name, parent_frame=getattr(self._panel, "frame", None))
                except Exception as e:
                    log.error("Error showing approval dialog: %s", e)
                    approved = False

            if request_id is not None and hasattr(adapter, "submit_approval"):
                best_effort(lambda: adapter.submit_approval(request_id, approved), "submitting agent backend approval")

        self._run_unified_worker_drain_loop(drain_q, run_agent, "agent", on_approval_callback=on_approval_required, on_finished=self._finalize_terminal_ready)

    def _run_specialized(self, query_text: str, model: Any, mode: str = "") -> None:
        """Run a specialized mode (or web research) and stream its result into the response area."""
        spec = sidebar_mode(mode)
        if spec and spec.prefetch_suggested_name:
            if not getattr(self._panel, "_librarian_suggested_user_name_fetched", False):
                from plugin.chatbot.librarian import get_suggested_user_name

                # Resolve on the UI thread so UNO UserProfile reads stay on the main thread.
                self._panel._librarian_suggested_user_name = get_suggested_user_name(self._panel.ctx)
                self._panel._librarian_suggested_user_name_fetched = True

        turn_session = turn_session_or_stop(self._panel)
        if turn_session is None:
            return

        if mode and spec and spec.set_active_mode:
            self._panel._active_mode = mode

        turn_session.add_user_message(query_text)
        self._panel._append_response(query_text, role="user")
        self._panel._set_status("Starting research...")
        self._execute_web_research_effect(query_text, model, mode=mode)

    def _execute_web_research_effect(self, query_text: str, model: Any, mode: str = "") -> None:
        from plugin.main import get_tools

        spec = sidebar_mode(mode) or require_sidebar_mode(CHAT_MODE_WEB_RESEARCH)

        drain_q, turn = _send_worker_queues(self._panel)
        # Read show_thinking before spawning the thread so apply_chunk can use it
        show_thinking = as_bool(get_config("chatbot.show_search_thinking"))

        from plugin.chatbot.web_research_chat import format_sub_agent_conversation_history

        history_text = format_sub_agent_conversation_history(self._panel.session, current_query=query_text)
        # Spawn-time scope. run_search must not re-read _send_cancellation:
        # Stop clears the field and the next send replaces it.
        cancel_scope = capture_send_stop(self._panel)

        doc_type = getattr(self._panel, "cached_doc_type", None) or "writer"

        def run_search() -> None:
            try:
                def status_cb(msg: str) -> None:
                    turn.put((StreamQueueKind.STATUS, msg))

                # Always push thinking to the queue so the drain stays fed.
                # Display is controlled by show_thinking in apply_chunk below.
                def thinking_cb(msg: str) -> None:
                    turn.put((StreamQueueKind.THINKING, msg))

                def chat_append_cb(text: str) -> None:
                    turn.put((StreamQueueKind.CHUNK, text))

                def approval_cb(query_for_engine: str, tool_name: str, args: Any) -> Any:
                    from plugin.framework.send_cancellation import wait_for_approval

                    event = threading.Event()
                    # Use setattr/getattr to avoid static attribute errors on Event
                    setattr(event, "approved", False)
                    setattr(event, "query_override", None)
                    turn.put((StreamQueueKind.APPROVAL_REQUIRED, query_for_engine, tool_name, event))
                    # event.wait() ignored Stop. Sidebar close latches the
                    # checker and never sets the event, so this worker parked.
                    if not wait_for_approval(event, cancel_scope):
                        turn.put((StreamQueueKind.STOPPED,))
                        return (False, None)
                    if not getattr(event, "approved", False):
                        # If the user rejects the search query, do not let the LLM
                        # keep going without the data it requested. Instead, immediately
                        # halt the entire tool call loop, acting exactly as if the
                        # user clicked the explicit 'Stop' button in the UI.
                        turn.put((StreamQueueKind.STOPPED,))
                    return (bool(getattr(event, "approved", False)), getattr(event, "query_override", None))

                # run_search runs on the chatbot-send-handler worker;
                # get_ctx() is main-thread-only. Panel bootstrap ctx matches
                # tool_loop.execute_fn (async tools marshal UNO reads themselves).
                tctx = ToolContext(doc=model, ctx=self._panel.ctx, doc_type=doc_type, services=get_tools()._services, caller="chat", status_callback=status_cb, append_thinking_callback=thinking_cb, approval_callback=approval_cb, chat_append_callback=chat_append_cb, send_cancellation=cancel_scope, uno_services_supported=getattr(self._panel, "cached_uno_services", None))

                extra_args: dict[str, Any] = {}
                if spec.extra_kwarg and spec.extra_attr:
                    val = getattr(self._panel, spec.extra_attr, None)
                    extra_args[spec.extra_kwarg] = (val or "") if spec.extra_attr == "_mode_topic" else val

                res = get_tools().execute(
                    spec.tool_name,
                    tctx,
                    bypass_thread_guard=False,
                    query=query_text,
                    history_text=history_text,
                    **extra_args,
                )

                data = _tool_result_dict(res, spec.tool_label, doc_type)

                if data.get("status") == "ok":
                    answer = data.get("result", "")
                    if not isinstance(answer, str):
                        answer = str(answer)
                    if spec.prepend_web_cache:
                        from plugin.chatbot.web_research_chat import web_research_cache_chat_text

                        cache_block = web_research_cache_chat_text(data)
                        answer = cache_block + answer
                    turn.put((StreamQueueKind.CHUNK, answer + "\n"))
                    turn.put((StreamQueueKind.STREAM_DONE, {"assistant_content": answer}))
                elif data.get("status") in ("finished", "switch_mode") and spec.leave_status is not None:
                    assert spec.finish_msgid is not None
                    raw = data.get("result", _(spec.finish_msgid))
                    if not isinstance(raw, str):
                        raw = str(raw)
                    done_payload: dict[str, Any] = {
                        "mode_finished": spec.key,
                        "spec_saved": bool(data.get("spec_saved", False)) if spec.tracks_spec_saved else False,
                        "assistant_content": raw,
                    }
                    turn.put((StreamQueueKind.CHUNK, raw + "\n"))
                    turn.put((StreamQueueKind.STREAM_DONE, done_payload))
                else:
                    msg = data.get("message") or data.get("result") or spec.default_error
                    note = "\n" + _("[{0} error: {1}]").format(spec.error_tag, msg) + "\n"
                    turn.put((StreamQueueKind.CHUNK, note))
                    # The painted note is the assistant message, same as a stream error.
                    done_payload = {"assistant_content": note.strip()}
                    if spec.leave_status is not None:
                        done_payload["mode_finished"] = spec.key
                        done_payload["spec_saved"] = False
                    turn.put((StreamQueueKind.STREAM_DONE, done_payload))
            except Exception as e:
                log.exception("Web/Librarian path ERROR in _run_web_research [doc: %s]", doc_type)

                turn.put((StreamQueueKind.ERROR, format_error_payload(e)))

        def on_approval_required(item: Any) -> None:
            # item = ("approval_required", query_for_engine, tool_name, event_obj)
            query_for_engine = item[1] if len(item) > 1 else ""
            tool_name = item[2] if len(item) > 2 else ""
            event_obj = item[3] if len(item) > 3 else None
            if event_obj is not None:
                self._panel._events.begin_inline_web_approval(query_for_engine, tool_name, event_obj)
            log.info("web_research on_approval_required: tool=%s (inline Accept/Change/Reject)", tool_name)

        def _finalize_research() -> None:
            from plugin.chatbot.tool_loop_actions import finalize_sidebar_assistant_response

            finalize_sidebar_assistant_response(self._panel, allow_rerender=not self._panel.stop_requested)

        self._run_unified_worker_drain_loop(drain_q, run_search, "web", show_thinking=show_thinking, on_approval_callback=on_approval_required, on_finished=_finalize_research)

    def _get_mcp_url(self) -> str | None:
        """Construct the local MCP streamable-HTTP endpoint URL from config."""
        if not as_bool(get_config("mcp.mcp_enabled")):
            return None
        from plugin.mcp.server import mcp_endpoint_url

        port = get_config("mcp.mcp_port")
        # MCP binds localhost only (mcp/module.yaml); host/ssl are not user settings.
        return mcp_endpoint_url("localhost", port, False)
