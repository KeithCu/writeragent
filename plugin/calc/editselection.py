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
"""Operations for Calc (Extend/Edit Selection)."""

from plugin.framework.config import get_config_int, get_config_str
from plugin.framework.client.errors import format_error_message
from plugin.chatbot.dialogs import msgbox
from plugin.framework.i18n import _
from plugin.chatbot.selection import StreamCompletionTask, create_validated_client, prompt_for_edit_instructions, stream_completion_tasks


def _build_calc_edit_prompt(original: str, instructions: str) -> str:
    return (
        "ORIGINAL VERSION:\n"
        + original
        + "\n Below is an edited version according to the following instructions. Don't waste time thinking, be as fast as you can. The edited text will be a shorter or longer version of the original text based on the instructions. There are no comments in the edited version. The edited version is followed by the end of the document. The original version will be edited as follows to create the edited version:\n"
        + instructions
        + "\nEDITED VERSION:\n"
    )


def do_calc_extend_edit(ctx, model, input_box_fn, is_edit):
    sheet = model.CurrentController.ActiveSheet
    selection = model.CurrentController.Selection

    user_input = ""
    extra_instructions = ""
    title = _("WriterAgent: Edit Selection (Calc)") if is_edit else _("WriterAgent: Extend Selection (Calc)")
    if is_edit:
        edit_request = prompt_for_edit_instructions(ctx, input_box_fn, title)
        if edit_request is None:
            return
        user_input, extra_instructions = edit_request

    area = selection.getRangeAddress()
    col_range = range(area.StartColumn, area.EndColumn + 1)
    row_range = range(area.StartRow, area.EndRow + 1)

    extend_sys = get_config_str("extend_selection_system_prompt")
    extend_max = get_config_int("extend_selection_max_tokens")
    edit_sys = extra_instructions or get_config_str("edit_selection_system_prompt")
    edit_max = get_config_int("edit_selection_max_new_tokens")

    tasks: list[StreamCompletionTask] = []
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
                tasks.append(StreamCompletionTask(cell_text, extend_sys, extend_max, (col, row, None)))
            else:
                cell_original = cell_text
                prompt = _build_calc_edit_prompt(cell_original, user_input)
                max_tokens = len(cell_original) + edit_max
                tasks.append(StreamCompletionTask(prompt, edit_sys, max_tokens, (col, row, cell_original)))

    if not tasks:
        return

    client = create_validated_client(ctx, title)
    if client is None:
        return

    def prepare_task(task: StreamCompletionTask):
        col, row, original = task.payload
        cell = sheet.getCellByPosition(col, row)

        # Keep Calc writes local to the cell adapter; the shared code only drives the stream.
        accumulated_text = [""]
        if not is_edit:
            accumulated_text[0] = task.prompt
        elif original is not None:
            cell.setString("")

        def apply_chunk(chunk_text, is_thinking=False):
            if not is_thinking:
                accumulated_text[0] += chunk_text
                cell.setString(accumulated_text[0])

        def on_error(e):
            if original is not None:
                cell.setString(original)
            msgbox(ctx, title, format_error_message(e))

        return apply_chunk, on_error

    stream_completion_tasks(ctx, client, tasks, prepare_task)
