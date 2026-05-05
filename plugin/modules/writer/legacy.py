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
"""Legacy operations for Writer (Extend/Edit Selection)."""

from plugin.framework.config import get_config_int, get_config_str, get_text_model, get_api_config, validate_api_config, get_current_endpoint, update_lru_history
from plugin.modules.http.errors import format_error_message
from plugin.framework.async_stream import run_stream_completion_async
from plugin.framework.dialogs import msgbox
from plugin.framework.i18n import _
from plugin.framework.config import set_config
from plugin.modules.http.client import LlmClient
from plugin.framework.document import WriterCompoundUndo, WriterStreamedRewriteSession, build_writer_rewrite_prompt, get_string_without_tracked_deletions


def do_extend_selection(ctx, model, input_box_fn):
    selection = model.CurrentController.getSelection()
    text_range = selection.getByIndex(0)
    original_text = get_string_without_tracked_deletions(text_range)
    if len(original_text) == 0:
        return

    extra_instructions = get_config_str(ctx, "additional_instructions")
    system_prompt = extra_instructions
    current_endpoint = get_current_endpoint(ctx)
    update_lru_history(ctx, system_prompt, "prompt_lru", current_endpoint)
    prompt = original_text
    max_tokens = get_config_int(ctx, "extend_selection_max_tokens")
    model_val = get_text_model(ctx)
    update_lru_history(ctx, model_val, "model_lru", current_endpoint)

    api_config = get_api_config(ctx)
    ok, err_msg = validate_api_config(api_config)
    if not ok:
        msgbox(ctx, _("WriterAgent: Extend Selection"), _(err_msg))
        return

    client = LlmClient(api_config, ctx)

    compound_undo = WriterCompoundUndo(model, "WriterAgent: Extend selection")

    def apply_chunk(chunk_text, is_thinking=False):
        if not is_thinking:
            text_range.setString(text_range.getString() + chunk_text)

    def on_done():
        compound_undo.close()

    def on_error(e):
        try:
            msgbox(ctx, _("WriterAgent: Extend Selection"), _(format_error_message(e)))
        finally:
            compound_undo.close()

    try:
        run_stream_completion_async(ctx, client, prompt, system_prompt, max_tokens, apply_chunk, on_done, on_error)
    except Exception as e:
        on_error(e)


def do_edit_selection(ctx, model, input_box_fn):
    selection = model.CurrentController.getSelection()
    text_range = selection.getByIndex(0)
    original_text = get_string_without_tracked_deletions(text_range)

    try:
        user_input, extra_instructions = input_box_fn(ctx, _("Please enter edit instructions!"), _("Input"), "")
        if not user_input:
            return
        if extra_instructions:
            set_config(ctx, "additional_instructions", extra_instructions)
            update_lru_history(ctx, extra_instructions, "prompt_lru", get_current_endpoint(ctx))
    except Exception as e:
        msgbox(ctx, _("WriterAgent: Edit Selection"), _(format_error_message(e)))
        return

    prompt = build_writer_rewrite_prompt(original_text, user_input)
    system_prompt = extra_instructions or ""
    max_tokens = len(original_text) + get_config_int(ctx, "edit_selection_max_new_tokens")

    api_config = get_api_config(ctx)
    ok, err_msg = validate_api_config(api_config)
    if not ok:
        msgbox(ctx, _("WriterAgent: Edit Selection"), _(err_msg))
        return

    client = LlmClient(api_config, ctx)
    session = WriterStreamedRewriteSession(model, text_range, original_text)

    def apply_chunk(chunk_text, is_thinking=False):
        if not is_thinking:
            session.append_chunk(chunk_text)

    def on_done():
        warning = session.finish()
        if warning:
            msgbox(ctx, _("WriterAgent: Edit Selection"), warning)

    def on_error(e):
        session.abort_and_restore()
        msgbox(ctx, _("WriterAgent: Edit Selection"), _(format_error_message(e)))

    try:
        run_stream_completion_async(ctx, client, prompt, system_prompt, max_tokens, apply_chunk, on_done, on_error)
    except Exception as e:
        on_error(e)
