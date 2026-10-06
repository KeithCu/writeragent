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
"""Operations for Writer (Extend/Edit Selection)."""

from typing import Any

from plugin.framework.config import get_config_int, get_config_str, get_current_endpoint
from plugin.framework.client.model_fetcher import get_text_model
from plugin.chatbot.config_ui_helpers import update_lru_history
from plugin.framework.errors import format_error_message
from plugin.chatbot.dialogs import msgbox
from plugin.framework.i18n import _
from plugin.chatbot.selection import create_validated_client, prompt_for_edit_instructions, stream_completion
from plugin.doc.text_helpers import get_string_without_tracked_deletions
from plugin.writer.edit_review import (
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
        # Literal must match TRACKED_SELECTION_MESSAGE so xgettext extracts it.
        msgbox(ctx, title, _("This selection contains tracked insertions or deletions. Accept or reject those changes first. The selection was not modified."))
        return True
    return False


def do_extend_selection(ctx: Any, model: Any, input_box_fn: Any) -> None:
    from plugin.writer.selection import selected_text_range

    text_range = selected_text_range(model.CurrentController)
    title = _("WriterAgent: Extend Selection")
    if _stop_for_tracked_changes(ctx, title, text_range):
        return
    original_text = get_string_without_tracked_deletions(text_range)
    if len(original_text) == 0:
        return

    extra_instructions = get_config_str("additional_instructions")
    system_prompt = extra_instructions
    current_endpoint = get_current_endpoint()
    update_lru_history(system_prompt, "prompt_lru", "")
    prompt = original_text
    max_tokens = get_config_int("extend_selection_max_tokens")
    model_val = get_text_model()
    update_lru_history(model_val, "model_lru", current_endpoint)

    client = create_validated_client(ctx, title)
    if client is None:
        return

    session = WriterStreamedAppendSession(
        model, text_range, original_text,
        track_reviewable=review_recording_enabled(ctx),
    )

    # A chunk already in hand is a document write and lands. Stop aborts the
    # network read inside stream_completion, not this append.
    def apply_chunk(chunk_text: str, is_thinking: bool = False) -> None:
        if not is_thinking:
            session.append_chunk(chunk_text)

    def on_done() -> None:
        warning = session.finish()
        if warning:
            msgbox(ctx, title, warning)

    def on_error(e: BaseException) -> None:
        session.abort_and_restore()
        msgbox(ctx, title, _(format_error_message(e)))

    # Stop is read from ctx inside stream_completion. Do not pass a separate checker.
    stream_completion(ctx, client, prompt, system_prompt, max_tokens, apply_chunk, on_done, on_error)


def do_edit_selection(ctx: Any, model: Any, input_box_fn: Any) -> None:
    from plugin.writer.selection import selected_text_range

    text_range = selected_text_range(model.CurrentController)
    title = _("WriterAgent: Edit Selection")
    if _stop_for_tracked_changes(ctx, title, text_range):
        return
    original_text = get_string_without_tracked_deletions(text_range)

    edit_request = prompt_for_edit_instructions(ctx, input_box_fn, title)
    if edit_request is None:
        return
    user_input, extra_instructions = edit_request

    prompt = build_writer_rewrite_prompt(original_text, user_input)
    system_prompt = extra_instructions or ""
    max_tokens = len(original_text) + get_config_int("edit_selection_max_new_tokens")

    client = create_validated_client(ctx, title)
    if client is None:
        return

    session = WriterStreamedRewriteSession(
        model, text_range, original_text,
        track_reviewable=review_recording_enabled(ctx),
    )

    # A chunk already in hand is a document write and lands. Stop aborts the
    # network read inside stream_completion, not this append.
    def apply_chunk(chunk_text: str, is_thinking: bool = False) -> None:
        if not is_thinking:
            session.append_chunk(chunk_text)

    def on_done() -> None:
        warning = session.finish()
        if warning:
            msgbox(ctx, title, warning)

    def on_error(e: BaseException) -> None:
        session.abort_and_restore()
        msgbox(ctx, title, _(format_error_message(e)))

    # Stop is read from ctx inside stream_completion. Do not pass a separate checker.
    stream_completion(ctx, client, prompt, system_prompt, max_tokens, apply_chunk, on_done, on_error)
