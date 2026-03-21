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
from plugin.framework.config import get_config, get_config_int, get_api_config, validate_api_config, get_current_endpoint, update_lru_history
from plugin.modules.http.errors import format_error_message
from plugin.framework.async_stream import run_stream_completion_async
from plugin.framework.dialogs import msgbox
from plugin.framework.i18n import _

def do_extend_selection(ctx, model, input_box_fn):
    selection = model.CurrentController.getSelection()
    text_range = selection.getByIndex(0)
    original_text = text_range.getString()
    if len(original_text) == 0:
        return

    extra_instructions = get_config(ctx, "additional_instructions")
    system_prompt = extra_instructions
    current_endpoint = get_current_endpoint(ctx)
    update_lru_history(ctx, system_prompt, "prompt_lru", current_endpoint)
    prompt = original_text
    max_tokens = get_config_int(ctx, "extend_selection_max_tokens", 1000)
    model_val = get_config(ctx, "text_model") or get_config(ctx, "model")
    update_lru_history(ctx, model_val, "model_lru", current_endpoint)

    api_config = get_api_config(ctx)
    ok, err_msg = validate_api_config(api_config)
    if not ok:
        msgbox(ctx, _("WriterAgent: Extend Selection"), _(err_msg))
        return

    from plugin.modules.http.client import LlmClient
    client = LlmClient(api_config)

    def apply_chunk(chunk_text, is_thinking=False):
        if not is_thinking:
            text_range.setString(text_range.getString() + chunk_text)

    def on_error(e):
        msgbox(ctx, _("WriterAgent: Extend Selection"), _(format_error_message(e)))

    try:
        run_stream_completion_async(
            ctx, client, prompt, system_prompt, max_tokens,
            apply_chunk, lambda: None, on_error
        )
    except Exception as e:
        on_error(e)

def do_edit_selection(ctx, model, input_box_fn):
    selection = model.CurrentController.getSelection()
    text_range = selection.getByIndex(0)
    original_text = text_range.getString()
    
    try:
        user_input, extra_instructions = input_box_fn(ctx, _("Please enter edit instructions!"), _("Input"), "")
        if not user_input:
            return
        if extra_instructions:
            from plugin.framework.config import set_config
            set_config(ctx, "additional_instructions", extra_instructions)
            update_lru_history(ctx, extra_instructions, "prompt_lru", get_current_endpoint(ctx))
    except Exception as e:
        msgbox(ctx, _("WriterAgent: Edit Selection"), _(format_error_message(e)))
        return

    prompt = "ORIGINAL VERSION:\n" + original_text + "\n Below is an edited version according to the following instructions. There are no comments in the edited version. The edited version is followed by the end of the document. The original version will be edited as follows to create the edited version:\n" + user_input + "\nEDITED VERSION:\n"
    system_prompt = extra_instructions or ""
    max_tokens = len(original_text) + get_config_int(ctx, "edit_selection_max_new_tokens", 1000)

    api_config = get_api_config(ctx)
    ok, err_msg = validate_api_config(api_config)
    if not ok:
        msgbox(ctx, _("WriterAgent: Edit Selection"), _(err_msg))
        return

    from plugin.modules.http.client import LlmClient
    client = LlmClient(api_config)

    text_range.setString("")

    def apply_chunk(chunk_text, is_thinking=False):
        if not is_thinking:
            text_range.setString(text_range.getString() + chunk_text)

    def on_error(e):
        text_range.setString(original_text)
        msgbox(ctx, _("WriterAgent: Edit Selection"), _(format_error_message(e)))

    try:
        run_stream_completion_async(
            ctx, client, prompt, system_prompt, max_tokens,
            apply_chunk, lambda: None, on_error
        )
    except Exception as e:
        on_error(e)
