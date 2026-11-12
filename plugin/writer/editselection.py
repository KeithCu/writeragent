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

from __future__ import annotations

from typing import Any, Callable

from plugin.framework.config import get_config, get_current_endpoint
from plugin.framework.client.model_fetcher import get_text_model
from plugin.framework.model_lru import update_lru_history
from plugin.framework.errors import format_error_message
from plugin.framework.i18n import _
from plugin.chatbot.dialogs import msgbox
from plugin.chatbot.selection import (
    bind_selection_cancel,
    create_validated_client,
    end_selection_cancel,
    prompt_for_edit_instructions,
    stream_completion,
)
from plugin.doc.agent_undo import record_agent_turn_after, record_agent_turn_before
from plugin.doc.text_helpers import get_string_without_tracked_deletions, normalize_linebreaks, selected_text_range
from plugin.writer.edit_review import (
    TRACKED_SELECTION_MESSAGE,
    TrackedChangesInSelection,
    WriterStreamedAppendSession,
    WriterStreamedRewriteSession,
    build_writer_rewrite_prompt,
    refuse_tracked_insert_or_delete,
    review_recording_enabled,
)


def _selection_target(
    ctx: Any,
    model: Any,
    title: str,
    *,
    silent_empty: bool = False,
) -> tuple[Any, str] | None:
    """Return (range, text without tracked deletions), or None after telling the user why not."""
    text_range = selected_text_range(model.CurrentController)
    if text_range is None:
        if not silent_empty:
            msgbox(ctx, title, _("Select text first"))
        return None
    # Stop Extend/Edit Selection when the range already has an Insert or Delete.
    #
    # The message box is the user-visible path. The streamed session raises the same
    # way before ``setString``, so skipping this wrapper still cannot wipe the redlines.
    # refuse_tracked_insert_or_delete runs before the empty-selection return so
    # deletion-only ranges that read as empty still block Extend/Edit with the
    # tracked-changes message instead of reaching setString silently.
    try:
        refuse_tracked_insert_or_delete(text_range)
    except TrackedChangesInSelection:
        msgbox(ctx, title, _(TRACKED_SELECTION_MESSAGE))
        return None
    original_text = get_string_without_tracked_deletions(text_range)
    if not original_text:
        if not silent_empty:
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
    *,
    preset_instruction: str | None = None,
    sidebar_notify: Callable[[str], None] | None = None,
    host: Any | None = None,
) -> None:
    completion_meta: dict[str, Any] = {}

    # Chunks already in hand are document writes and land (session.append_chunk).
    # When Stopped, on_done runs finish() to preserve text already written,
    # rather than abort_and_restore. Note: on rewrite, stopping retains the
    # rewritten head but the remainder of the selection is replaced by finish()
    # (can be restored via undo or tracked changes).
    def apply_chunk(chunk_text: str, is_thinking: bool = False) -> None:
        if not is_thinking:
            session.append_chunk(chunk_text)

    def on_done(_item: Any = None) -> None:
        try:
            warning = session.finish()
            if warning:
                msgbox(ctx, title, warning)
            elif (
                preset_instruction is not None
                and sidebar_notify is not None
                and completion_meta.get("finish_reason") == "length"
                and not (session.generated_text or "").strip()
            ):
                sidebar_notify(
                    _("The rewrite ran out of tokens before producing text. Try again or shorten the selection.")
                )
        finally:
            try:
                record_agent_turn_after(session.doc)
            finally:
                end_selection_cancel(arm)

    def on_error(e: BaseException) -> None:
        try:
            session.abort_and_restore()
            msgbox(ctx, title, format_error_message(e))
        finally:
            try:
                record_agent_turn_after(session.doc)
            finally:
                end_selection_cancel(arm)

    arm = bind_selection_cancel(host, session.doc)
    try:
        stream_completion(
            ctx,
            client,
            prompt,
            system_prompt,
            max_tokens,
            apply_chunk,
            on_done,
            on_error,
            cancel=arm.scope,
            on_stopped_fn=on_done,
            completion_out=completion_meta,
        )
    except BaseException:
        end_selection_cancel(arm)
        raise


def do_extend_selection(
    ctx: Any,
    model: Any,
    *,
    host: Any | None = None,
) -> None:
    title = _("WriterAgent: Extend Selection")
    target = _selection_target(ctx, model, title)
    if target is None:
        return
    text_range, original_text = target

    client = create_validated_client(ctx, title)
    if client is None:
        return

    update_lru_history(get_text_model(), "model_lru", get_current_endpoint())
    system_prompt = get_config("extend_selection_system_prompt")
    max_tokens = get_config("chatbot.extend_selection_max_tokens")

    record_agent_turn_before(model)
    session = WriterStreamedAppendSession(
        model, text_range, original_text,
        track_reviewable=review_recording_enabled(),
    )
    _stream_into_session(
        ctx,
        title,
        client,
        original_text,
        system_prompt,
        max_tokens,
        session,
        host=host,
    )


def _whole_body_target(ctx: Any, model: Any, title: str) -> tuple[Any, str] | None:
    """Writer body range and text when slash rewrite has no selection."""
    body = model.getText()
    cursor = body.createTextCursor()
    cursor.gotoStart(False)
    cursor.gotoEnd(True)
    try:
        refuse_tracked_insert_or_delete(cursor)
    except TrackedChangesInSelection:
        msgbox(ctx, title, _(TRACKED_SELECTION_MESSAGE))
        return None
    original_text = normalize_linebreaks(get_string_without_tracked_deletions(cursor))
    if not original_text.strip():
        msgbox(ctx, title, _("The document is empty."))
        return None
    return cursor, original_text


def do_edit_selection(
    ctx: Any,
    model: Any,
    *,
    preset_instruction: str | None = None,
    allow_whole_document: bool = False,
    sidebar_notify: Callable[[str], None] | None = None,
    host: Any | None = None,
) -> None:
    title = _("WriterAgent: Edit Selection")
    extra_instructions = ""
    client: Any | None = None
    if preset_instruction is not None:
        user_input = preset_instruction
        target = _selection_target(ctx, model, title, silent_empty=True)
        if target is None and allow_whole_document:
            target = _whole_body_target(ctx, model, title)
    else:
        target = _selection_target(ctx, model, title)
        if target is None:
            return
        client = create_validated_client(ctx, title)
        if client is None:
            return
        edit_request = prompt_for_edit_instructions(ctx, title)
        if edit_request is None:
            return
        user_input, extra_instructions = edit_request

    if target is None:
        return
    text_range, original_text = target
    preserve_paragraph_breaks = bool(
        allow_whole_document and preset_instruction is not None and "\n" in original_text
    )
    if preserve_paragraph_breaks:
        user_input = (
            user_input
            + " Preserve paragraph breaks; separate paragraphs with a single newline character."
        )

    if preset_instruction is not None:
        client = create_validated_client(ctx, title)
        if client is None:
            return

    update_lru_history(get_text_model(), "model_lru", get_current_endpoint())
    prompt = build_writer_rewrite_prompt(original_text, user_input)
    system_prompt = extra_instructions or get_config("edit_selection_system_prompt")
    max_tokens = len(original_text) + get_config("chatbot.edit_selection_max_new_tokens")

    record_agent_turn_before(model)
    session = WriterStreamedRewriteSession(
        model,
        text_range,
        original_text,
        track_reviewable=review_recording_enabled(),
        preserve_paragraph_breaks=preserve_paragraph_breaks,
    )
    assert client is not None
    _stream_into_session(
        ctx,
        title,
        client,
        prompt,
        system_prompt,
        max_tokens,
        session,
        preset_instruction=preset_instruction,
        sidebar_notify=sidebar_notify,
        host=host,
    )
