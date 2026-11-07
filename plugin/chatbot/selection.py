# WriterAgent - AI Writing Assistant for LibreOffice
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
"""Active-document dispatch for Extend/Edit Selection actions."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import Any, Callable

from plugin.framework.model_lru import update_lru_history
from plugin.framework.doc_type import DocumentType, get_document_type
from plugin.framework.async_drain_guard import add_drain_idle_callback, get_drain_owner, remove_drain_idle_callback
from plugin.framework.queue_executor import main
from plugin.framework.async_stream import run_stream_completion_async
from plugin.framework.errors import format_error_message
from plugin.framework.client.llm_client import LlmClient

from plugin.framework.send_cancellation import SendCancellation
from plugin.framework.uno_listeners import BaseListener
from plugin.chatbot.send_state import SendEvent, SendEventKind, spawn_scope
from plugin.framework.config import get_api_config, save, validate_api_config
from plugin.framework.i18n import _
from plugin.framework.uno_context import get_ctx, get_document_from_frame
from .dialogs import msgbox
from .dialog_views import input_box


@dataclass
class _SelectionArm:
    scope: SendCancellation
    host: Any | None
    armed: bool
    detach: Callable[[], None]
    ended: bool = False


class _SelectionDocListener(BaseListener):
    _scope: SendCancellation

    def __init__(self, scope: SendCancellation) -> None:
        super().__init__()
        self._scope = scope

    def on_disposing(self, Source: Any) -> None:
        self._scope.cancel()


def bind_selection_cancel(
    host: Any | None,
    model: Any,
    cancel: SendCancellation | None = None,
) -> _SelectionArm:
    scope: SendCancellation
    armed = False

    if cancel is not None:
        scope = cancel
    elif host is not None and hasattr(host, "_send_cancellation"):
        existing_scope = spawn_scope(host)
        if existing_scope is not None:
            scope = existing_scope
        else:
            send_state = getattr(getattr(host, "sidebar_state", None), "send", None)
            if send_state is not None and not send_state.is_busy and not send_state.is_recording:
                scope = SendCancellation()
                host._send_cancellation = scope
                if hasattr(host, "_stop_requested_fallback"):
                    host._stop_requested_fallback = False
                host.dispatch(SendEvent(SendEventKind.EXTRACTED_SEND))
                armed = True
            else:
                scope = SendCancellation()
    else:
        scope = SendCancellation()

    listener: _SelectionDocListener | None = None
    if model is not None and hasattr(model, "addEventListener"):
        listener = _SelectionDocListener(scope)
        with contextlib.suppress(Exception):
            model.addEventListener(listener)

    def detach() -> None:
        if listener is not None and model is not None and hasattr(model, "removeEventListener"):
            with contextlib.suppress(Exception):
                model.removeEventListener(listener)

    return _SelectionArm(scope=scope, host=host, armed=armed, detach=detach)


def end_selection_cancel(arm: _SelectionArm) -> None:
    """Tear down a selection cancel arm; idempotent."""
    if arm.ended:
        return
    arm.ended = True
    with contextlib.suppress(Exception):
        arm.detach()

    if not arm.armed:
        return
    host = arm.host
    if host is None or getattr(host, "_panel_teardown", False):
        return
    if getattr(host, "_send_cancellation", None) is arm.scope:
        host._send_cancellation = None
    if hasattr(host, "_stop_requested_fallback"):
        host._stop_requested_fallback = False

    send_state = getattr(getattr(host, "sidebar_state", None), "send", None)
    if send_state is not None and getattr(send_state, "is_busy", False):
        host.dispatch(SendEvent(SendEventKind.SEND_COMPLETED))
        sync_fn = getattr(host, "sync_has_text_from_query", None)
        if callable(sync_fn):
            sync_fn()


@dataclass
class StreamCompletionTask:
    prompt: str
    system_prompt: str
    max_tokens: int
    payload: Any = None


ApplyChunkFn = Callable[[str, bool], None]
ErrorFn = Callable[[Exception], None]
PrepareTaskFn = Callable[[StreamCompletionTask], tuple[ApplyChunkFn, ErrorFn]]


def create_validated_client(ctx: Any, title: str) -> LlmClient | None:
    """Return an LLM client for selection actions, or show the config error."""
    api_config = get_api_config()
    ok, err_msg = validate_api_config(api_config)
    if not ok:
        msgbox(ctx, title, _(err_msg))
        return None
    return LlmClient(api_config, ctx)


def prompt_for_edit_instructions(ctx: Any, title: str) -> tuple[str, str] | None:
    """Show the edit dialog and persist shared prompt history when supplied."""
    try:
        user_input, extra_instructions = input_box(ctx, _("Please enter edit instructions!"), _("Input"), "")
    except Exception as e:
        msgbox(ctx, title, format_error_message(e))
        return None

    if not user_input:
        return None
    if extra_instructions:
        save({"additional_instructions": extra_instructions})
        update_lru_history(extra_instructions, "prompt_lru", "")
    return user_input, extra_instructions


def stream_completion(
    ctx: Any,
    client: LlmClient,
    prompt: str,
    system_prompt: str,
    max_tokens: int,
    apply_chunk_fn: ApplyChunkFn,
    on_done_fn: Callable[..., None],
    on_error_fn: ErrorFn,
    *,
    cancel: SendCancellation | None = None,
    on_stopped_fn: Callable[[], None] | None = None,
    completion_out: dict[str, Any] | None = None,
) -> None:
    """Start a simple completion stream and route startup failures like stream errors."""
    try:
        run_stream_completion_async(
            ctx,
            client,
            prompt,
            system_prompt,
            max_tokens,
            apply_chunk_fn,
            on_done_fn,
            on_error_fn,
            cancel=cancel,
            on_stopped_fn=on_stopped_fn,
            completion_out=completion_out,
        )
    except Exception as e:
        on_error_fn(e)


def stream_completion_tasks(
    ctx: Any,
    client: LlmClient,
    tasks: list[StreamCompletionTask],
    prepare_task_fn: PrepareTaskFn,
    on_finished: Callable[[], None] | None = None,
    *,
    cancel: SendCancellation | None = None,
    completion_out: dict[str, Any] | None = None,
) -> None:
    """Run simple completion streams sequentially, advancing from each done callback.

    ``on_finished`` runs once when every task is done or the first task fails.
    This returns before that when the next cell is waiting on drain idle.
    """
    task_index = 0
    finished = False

    def finish_once() -> None:
        nonlocal finished
        if finished:
            return
        finished = True
        if on_finished is not None:
            on_finished()

    # schedule_next_when_idle registers a one-shot callback that removes
    # itself. run_next_task defers itself with that callback if the pump is
    # already owned. A fresh lambda per task fires while the previous task
    # still owns the pump, and async_stream then rejects the later task
    # (NestedDrainOwnerError).

    def schedule_next_when_idle() -> None:
        fired = False

        def _once() -> None:
            nonlocal fired
            if not fired:
                fired = True
                # It is possible this is running from _notify_drain_idle traversing the list,
                # meaning it's still in the list when it runs. We remove it so it's gone for good.
                remove_drain_idle_callback(_once)
                main.post(run_next_task)

        add_drain_idle_callback(_once)

    def run_next_task() -> None:
        nonlocal task_index
        if task_index > 0 and get_drain_owner() is not None:
            schedule_next_when_idle()
            return
        if task_index >= len(tasks):
            finish_once()
            return
        task = tasks[task_index]
        task_index += 1
        try:
            apply_chunk_fn, on_error_fn = prepare_task_fn(task)
        except BaseException:
            finish_once()
            raise

        def on_error(exc: Exception) -> None:
            try:
                on_error_fn(exc)
            finally:
                finish_once()

        stream_completion(
            ctx,
            client,
            task.prompt,
            task.system_prompt,
            task.max_tokens,
            apply_chunk_fn,
            schedule_next_when_idle,
            on_error,
            cancel=cancel,
            on_stopped_fn=finish_once,
            completion_out=completion_out,
        )

    try:
        run_next_task()
    except BaseException:
        finish_once()
        raise


def do_selection_action_for_document(
    ctx: Any,
    model: Any,
    is_edit: bool,
    *,
    preset_instruction: str | None = None,
    allow_whole_document: bool = False,
    sidebar_notify: Callable[[str], None] | None = None,
    cancel: SendCancellation | None = None,
    host: Any | None = None,
) -> None:
    """Dispatch Extend/Edit Selection to the document-specific implementation."""
    doc_type = get_document_type(model)
    if doc_type == DocumentType.WRITER:
        from plugin.writer.editselection import do_edit_selection, do_extend_selection

        if is_edit:
            do_edit_selection(
                ctx,
                model,
                preset_instruction=preset_instruction,
                allow_whole_document=allow_whole_document,
                sidebar_notify=sidebar_notify,
                cancel=cancel,
                host=host,
            )
        else:
            do_extend_selection(ctx, model, cancel=cancel, host=host)
        return

    if doc_type == DocumentType.CALC:
        from plugin.calc.editselection import do_calc_extend_edit

        do_calc_extend_edit(
            ctx,
            model,
            is_edit,
            preset_instruction=preset_instruction,
            sidebar_notify=sidebar_notify,
            allow_whole_document=allow_whole_document,
            cancel=cancel,
            host=host,
        )
        return

    if doc_type in (DocumentType.DRAW, DocumentType.IMPRESS):
        from plugin.draw.editselection import do_draw_extend_edit

        do_draw_extend_edit(
            ctx,
            model,
            is_edit,
            preset_instruction=preset_instruction,
            sidebar_notify=sidebar_notify,
            allow_whole_document=allow_whole_document,
            cancel=cancel,
            host=host,
        )
        return

    action = _("Edit") if is_edit else _("Extend")
    message = _("{0} selection not supported for this document type").format(action)
    if sidebar_notify is not None:
        sidebar_notify(message)
        return
    msgbox(ctx, "WriterAgent", message)


def _action_selection(services: Any, is_edit: bool, frame: Any = None) -> None:
    """Extend or Edit the sidebar frame's document, or the focused one.

    A passed frame resolves with ``get_document_from_frame``. The hamburger
    already has the sidebar frame; ``get_active_document()`` edits whichever
    document has focus. The menubar calls this with no frame, so it still
    uses the focused document.
    """
    ctx = get_ctx()
    host = None
    if frame is not None:
        from plugin.framework.frame_session import session_for_frame

        session = session_for_frame(frame)
        if session is not None and getattr(session, "panel", None) is not None:
            host = getattr(session.panel, "send_listener", None)
        doc = get_document_from_frame(frame)
    else:
        doc = services.document.get_active_document()
    if not doc:
        msgbox(ctx, "WriterAgent", _("No document open"))
        return

    do_selection_action_for_document(ctx, doc, is_edit, host=host)


def action_extend_selection(services: Any, frame: Any = None) -> None:
    """Get document selection -> stream AI completion -> append to text."""
    _action_selection(services, is_edit=False, frame=frame)


def action_edit_selection(services: Any, frame: Any = None) -> None:
    """Get selection -> input instructions -> stream AI -> replace text."""
    _action_selection(services, is_edit=True, frame=frame)
