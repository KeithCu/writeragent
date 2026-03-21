# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
# Copyright (c) 2026 LibreCalc AI Assistant (Calc integration features, originally MIT)
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
"""Legacy operations for Calc (Extend/Edit Selection)."""
from plugin.framework.config import get_config, get_config_int, get_api_config, validate_api_config
from plugin.modules.http.errors import format_error_message
from plugin.modules.http.client import LlmClient
from plugin.framework.async_stream import run_stream_completion_async
from plugin.framework.dialogs import msgbox
from plugin.framework.i18n import _

def do_calc_extend_edit(ctx, model, input_box_fn, is_edit):
    sheet = model.CurrentController.ActiveSheet
    selection = model.CurrentController.Selection

    user_input = ""
    if is_edit:
        user_input, _extra = input_box_fn(ctx, _("Please enter edit instructions!"), _("Input"), "")
        if not user_input:
            return

    area = selection.getRangeAddress()
    col_range = range(area.StartColumn, area.EndColumn + 1)
    row_range = range(area.StartRow, area.EndRow + 1)

    extend_sys = get_config(ctx, "extend_selection_system_prompt")
    extend_max = get_config_int(ctx, "extend_selection_max_tokens", 1000)
    edit_sys = get_config(ctx, "edit_selection_system_prompt")
    edit_max = get_config_int(ctx, "edit_selection_max_new_tokens", 1000)

    tasks = []
    cell_range = sheet.getCellRangeByPosition(area.StartColumn, area.StartRow, area.EndColumn, area.EndRow)
    data_array = cell_range.getDataArray()

    for row_idx, row in enumerate(row_range):
        for col_idx, col in enumerate(col_range):
            raw_val = data_array[row_idx][col_idx]
            # Convert values/empty cells to strings (similar to what getString() would do)
            cell_text = str(raw_val) if raw_val != "" and raw_val is not None else ""

            if not is_edit:
                if not cell_text:
                    continue
                cell = sheet.getCellByPosition(col, row)
                tasks.append((cell, cell_text, extend_sys, extend_max, None))
            else:
                cell_original = cell_text
                prompt = "ORIGINAL VERSION:\n" + cell_original + "\n Below is an edited version according to the following instructions. Don't waste time thinking, be as fast as you can. The edited text will be a shorter or longer version of the original text based on the instructions. There are no comments in the edited version. The edited version is followed by the end of the document. The original version will be edited as follows to create the edited version:\n" + user_input + "\nEDITED VERSION:\n"
                max_tokens = len(cell_original) + edit_max
                cell = sheet.getCellByPosition(col, row)
                tasks.append((cell, prompt, edit_sys, max_tokens, cell_original))

    if not tasks:
        return

    api_config = get_api_config(ctx)
    ok, err_msg = validate_api_config(api_config)
    if not ok:
        title = _("WriterAgent: Edit Selection (Calc)") if is_edit else _("WriterAgent: Extend Selection (Calc)")
        msgbox(ctx, title, _(err_msg))
        return

    client = LlmClient(api_config)
    task_index = [0]

    def run_next_cell():
        if task_index[0] >= len(tasks):
            return
        cell, prompt, system_prompt, max_tokens, original = tasks[task_index[0]]
        task_index[0] += 1
        if is_edit and original is not None:
            cell.setString("")

        def apply_chunk(chunk_text, is_thinking=False):
            if not is_thinking:
                cell.setString(cell.getString() + chunk_text)

        def on_done():
            run_next_cell()

        def on_error(e):
            if original is not None:
                cell.setString(original)
            title = _("WriterAgent: Edit Selection (Calc)") if is_edit else _("WriterAgent: Extend Selection (Calc)")
            msgbox(ctx, title, format_error_message(e))

        run_stream_completion_async(
            ctx, client, prompt, system_prompt, max_tokens,
            apply_chunk, on_done, on_error
        )

    run_next_cell()
