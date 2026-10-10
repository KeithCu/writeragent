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

from dataclasses import dataclass
from typing import Any, Callable

from plugin.chatbot.config_ui_helpers import update_lru_history
from plugin.doc.doc_type import DocumentType, get_document_type
from plugin.framework.async_drain_guard import add_drain_idle_callback, get_drain_owner, remove_drain_idle_callback
from plugin.framework.queue_executor import post_to_main_thread
from plugin.framework.async_stream import run_stream_completion_async
from plugin.framework.errors import format_error_message
from plugin.framework.client.llm_client import LlmClient
from plugin.framework.config import get_api_config, set_config, validate_api_config
from plugin.framework.i18n import _
from plugin.framework.uno_context import get_ctx, get_document_from_frame
from .dialogs import msgbox
from .dialog_views import input_box


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


def prompt_for_edit_instructions(ctx: Any, input_box_fn: Any, title: str) -> tuple[str, str] | None:
    """Show the edit dialog and persist shared prompt history when supplied."""
    try:
        user_input, extra_instructions = input_box_fn(ctx, _("Please enter edit instructions!"), _("Input"), "")
    except Exception as e:
        msgbox(ctx, title, _(format_error_message(e)))
        return None

    if not user_input:
        return None
    if extra_instructions:
        set_config("additional_instructions", extra_instructions)
        update_lru_history(extra_instructions, "prompt_lru", "")
    return user_input, extra_instructions


def stream_completion(ctx: Any, client: LlmClient, prompt: str, system_prompt: str, max_tokens: int, apply_chunk_fn: ApplyChunkFn, on_done_fn: Callable[[], None], on_error_fn: ErrorFn) -> None:
    """Start a simple completion stream and route startup failures like stream errors."""
    try:
        stop_checker = getattr(ctx, "stop_checker", None)
        run_stream_completion_async(ctx, client, prompt, system_prompt, max_tokens, apply_chunk_fn, on_done_fn, on_error_fn, stop_checker=stop_checker)
    except Exception as e:
        on_error_fn(e)


def stream_completion_tasks(ctx: Any, client: LlmClient, tasks: list[StreamCompletionTask], prepare_task_fn: PrepareTaskFn) -> None:
    """Run simple completion streams sequentially, advancing from each done callback."""
    task_index = [0]

    # schedule_next_when_idle registers a one-shot callback that removes
    # itself. run_next_task defers itself with that callback if the pump is
    # already owned. A fresh lambda per task fires while the previous task
    # still owns the pump, and async_stream then rejects the later task
    # (NestedDrainOwnerError).

    def schedule_next_when_idle() -> None:
        fired = [False]

        def _once() -> None:
            if not fired[0]:
                fired[0] = True
                # It is possible this is running from _notify_drain_idle traversing the list,
                # meaning it's still in the list when it runs. We remove it so it's gone for good.
                remove_drain_idle_callback(_once)
                post_to_main_thread(run_next_task)

        add_drain_idle_callback(_once)

    def run_next_task() -> None:
        if task_index[0] > 0 and get_drain_owner() is not None:
            schedule_next_when_idle()
            return
        if task_index[0] >= len(tasks):
            return
        task = tasks[task_index[0]]
        task_index[0] += 1
        apply_chunk_fn, on_error_fn = prepare_task_fn(task)
        stream_completion(ctx, client, task.prompt, task.system_prompt, task.max_tokens, apply_chunk_fn, schedule_next_when_idle, on_error_fn)

    run_next_task()


def do_selection_action_for_document(ctx: Any, model: Any, input_box_fn: Any, is_edit: bool) -> None:
    """Dispatch Extend/Edit Selection to the document-specific implementation."""
    doc_type = get_document_type(model)
    if doc_type == DocumentType.WRITER:
        if is_edit:
            from plugin.writer.editselection import do_edit_selection

            do_edit_selection(ctx, model, input_box_fn)
        else:
            from plugin.writer.editselection import do_extend_selection

            do_extend_selection(ctx, model, input_box_fn)
        return

    if doc_type == DocumentType.CALC:
        from plugin.calc.editselection import do_calc_extend_edit

        do_calc_extend_edit(ctx, model, input_box_fn, is_edit)
        return

    action = _("Edit") if is_edit else _("Extend")
    msgbox(ctx, "WriterAgent", _("{0} selection not supported for this document type").format(action))


def _action_selection(services: Any, is_edit: bool, frame: Any = None) -> None:
    """Extend or Edit the sidebar frame's document, or the focused one.

    A passed frame resolves with ``get_document_from_frame``. The hamburger
    already has the sidebar frame; ``get_active_document()`` edits whichever
    document has focus. The menubar calls this with no frame, so it still
    uses the focused document.
    """
    ctx = get_ctx()
    if frame is not None:
        doc = get_document_from_frame(frame)
    else:
        doc = services.document.get_active_document()
    if not doc:
        msgbox(ctx, "WriterAgent", _("No document open"))
        return

    do_selection_action_for_document(ctx, doc, input_box, is_edit)


def action_extend_selection(services: Any, frame: Any = None) -> None:
    """Get document selection -> stream AI completion -> append to text."""
    _action_selection(services, is_edit=False, frame=frame)


def action_edit_selection(services: Any, frame: Any = None) -> None:
    """Get selection -> input instructions -> stream AI -> replace text."""
    _action_selection(services, is_edit=True, frame=frame)
