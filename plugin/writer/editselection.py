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
"""Operations for Writer (Extend/Edit Selection).

Extend sends the selection text with ``extend_selection_system_prompt``. Edit sends
``build_writer_rewrite_prompt`` with the dialog's extra instructions, or
``edit_selection_system_prompt`` when that field is empty. ``additional_instructions``
is the chat system prompt and is not read here.
"""

from typing import Any

from plugin.framework.config import get_config_int, get_config_str, get_current_endpoint
from plugin.framework.client.model_fetcher import get_text_model
from plugin.framework.model_lru import update_lru_history
from plugin.framework.errors import format_error_message
from plugin.chatbot.dialogs import msgbox
from plugin.framework.i18n import _
from plugin.chatbot.selection import create_validated_client, prompt_for_edit_instructions, stream_completion
from plugin.doc.text_helpers import get_string_without_tracked_deletions, selected_text_range
from plugin.writer.edit_review import (
    TRACKED_SELECTION_MESSAGE,
    TrackedChangesInSelection,
    WriterStreamedAppendSession,
    WriterStreamedRewriteSession,
    build_writer_rewrite_prompt,
    refuse_tracked_insert_or_delete,
    review_recording_enabled,
)


def _stop_for_tracked_changes(ctx: Any, title: str, text_range: Any) -> bool:
    """Stop Extend/Edit Selection when the range already has an Insert or Delete.

    The message box is the user-visible path. The streamed session raises the same
    way before ``setString``, so skipping this wrapper still cannot wipe the redlines.
    A deletion-only selection reads as empty after skipping deletes; that used to
    return from Extend Selection with no message and, on Edit Selection, still
    reached ``setString``. Check before that empty return and before the prompt.
    """
    try:
        refuse_tracked_insert_or_delete(text_range)
    except TrackedChangesInSelection:
        msgbox(ctx, title, _(TRACKED_SELECTION_MESSAGE))
        return True
    return False


def _selection_target(ctx: Any, model: Any, title: str) -> tuple[Any, str] | None:
    """Return (range, text without tracked deletions), or None after telling the user why not."""
    text_range = selected_text_range(model.CurrentController)
    if text_range is None:
        msgbox(ctx, title, _("Select text first"))
        return None
    if _stop_for_tracked_changes(ctx, title, text_range):
        return None
    original_text = get_string_without_tracked_deletions(text_range)
    if not original_text:
        msgbox(ctx, title, _("Select text first"))
        return None
    return text_range, original_text


def _stream_into_session(
    ctx: Any,
    title: str,
    client: Any,
    prompt: str,
    system_prompt: str,
    max_tokens: int,
    session: Any,
) -> None:
    # Extend/Edit has no Stop control; a chunk already in hand is a document write
    # and lands (session.append_chunk).
    def apply_chunk(chunk_text: str, is_thinking: bool = False) -> None:
        if not is_thinking:
            session.append_chunk(chunk_text)

    def on_done() -> None:
        warning = session.finish()
        if warning:
            msgbox(ctx, title, warning)

    def on_error(e: BaseException) -> None:
        session.abort_and_restore()
        msgbox(ctx, title, format_error_message(e))

    stream_completion(ctx, client, prompt, system_prompt, max_tokens, apply_chunk, on_done, on_error)


def do_extend_selection(ctx: Any, model: Any, input_box_fn: Any) -> None:
    title = _("WriterAgent: Extend Selection")
    target = _selection_target(ctx, model, title)
    if target is None:
        return
    text_range, original_text = target

    client = create_validated_client(ctx, title)
    if client is None:
        return

    update_lru_history(get_text_model(), "model_lru", get_current_endpoint())
    prompt = original_text
    system_prompt = get_config_str("extend_selection_system_prompt")
    max_tokens = get_config_int("extend_selection_max_tokens")

    session = WriterStreamedAppendSession(
        model, text_range, original_text,
        track_reviewable=review_recording_enabled(ctx),
    )
    _stream_into_session(ctx, title, client, prompt, system_prompt, max_tokens, session)


def do_edit_selection(ctx: Any, model: Any, input_box_fn: Any) -> None:
    title = _("WriterAgent: Edit Selection")
    target = _selection_target(ctx, model, title)
    if target is None:
        return
    text_range, original_text = target

    client = create_validated_client(ctx, title)
    if client is None:
        return

    edit_request = prompt_for_edit_instructions(ctx, input_box_fn, title)
    if edit_request is None:
        return
    user_input, extra_instructions = edit_request

    update_lru_history(get_text_model(), "model_lru", get_current_endpoint())
    prompt = build_writer_rewrite_prompt(original_text, user_input)
    system_prompt = extra_instructions or get_config_str("edit_selection_system_prompt")
    max_tokens = len(original_text) + get_config_int("edit_selection_max_new_tokens")

    session = WriterStreamedRewriteSession(
        model, text_range, original_text,
        track_reviewable=review_recording_enabled(ctx),
    )
    _stream_into_session(ctx, title, client, prompt, system_prompt, max_tokens, session)
