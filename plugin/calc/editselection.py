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

from typing import Any

from plugin.calc.calc_utils import TEXT
from plugin.chatbot.dialogs import msgbox
from plugin.chatbot.selection import ApplyChunkFn, ErrorFn, StreamCompletionTask, create_validated_client, prompt_for_edit_instructions, stream_completion_tasks
from plugin.doc.compound_undo import WriterCompoundUndo
from plugin.framework.config import get_config_int, get_config_str
from plugin.framework.errors import format_error_message
from plugin.framework.i18n import _

# Caps queued LLM tasks so a huge selection cannot queue minutes of requests.
_MAX_CELLS = 250


def _build_calc_edit_prompt(original: str, instructions: str) -> str:
    return (
        "ORIGINAL VERSION:\n"
        + original
        + "\n Below is an edited version according to the following instructions. Don't waste time thinking, be as fast as you can. The edited text will be a shorter or longer version of the original text based on the instructions. There are no comments in the edited version. The edited version is followed by the end of the document. The original version will be edited as follows to create the edited version:\n"
        + instructions
        + "\nEDITED VERSION:\n"
    )


def do_calc_extend_edit(ctx: Any, model: Any, input_box_fn: Any, is_edit: bool) -> None:
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

    extend_sys = get_config_str("extend_selection_system_prompt")
    extend_max = get_config_int("extend_selection_max_tokens")
    edit_sys = extra_instructions or get_config_str("edit_selection_system_prompt")
    edit_max = get_config_int("edit_selection_max_new_tokens")

    tasks: list[StreamCompletionTask] = []

    # Intersect selection with used area to prevent evaluating 1M empty rows
    cursor = sheet.createCursor()
    cursor.gotoStartOfUsedArea(False)
    cursor.gotoEndOfUsedArea(True)
    used_area = cursor.getRangeAddress()

    start_col = max(area.StartColumn, used_area.StartColumn)
    end_col = min(area.EndColumn, used_area.EndColumn)
    start_row = max(area.StartRow, used_area.StartRow)
    end_row = min(area.EndRow, used_area.EndRow)

    if start_col > end_col or start_row > end_row:
        msgbox(ctx, title, _("Select one or more non-empty cells."))
        return

    col_range = range(start_col, end_col + 1)
    row_range = range(start_row, end_row + 1)

    cell_range = sheet.getCellRangeByPosition(start_col, start_row, end_col, end_row)
    formula_array = cell_range.getFormulaArray()

    truncated = False
    for row_idx, row in enumerate(row_range):
        for col_idx, col in enumerate(col_range):
            if len(tasks) >= _MAX_CELLS:
                truncated = True
                break
            raw_val = formula_array[row_idx][col_idx]

            # Use the formula array, not getString(), so formulas are not
            # replaced by their evaluated display text.
            cell_text = "" if raw_val is None else str(raw_val)

            if not cell_text:
                continue

            if not is_edit:
                tasks.append(StreamCompletionTask(cell_text, extend_sys, extend_max, (col, row, None)))
            else:
                prompt = _build_calc_edit_prompt(cell_text, user_input)
                max_tokens = len(cell_text) + edit_max
                tasks.append(StreamCompletionTask(prompt, edit_sys, max_tokens, (col, row, cell_text)))
        if len(tasks) >= _MAX_CELLS:
            truncated = True
            break

    if truncated:
        msgbox(ctx, title, _("Only the first {0} non-empty cells are edited.").format(_MAX_CELLS))

    if not tasks:
        msgbox(ctx, title, _("Select one or more non-empty cells."))
        return

    client = create_validated_client(ctx, title)
    if client is None:
        return

    def prepare_task(task: StreamCompletionTask) -> tuple[ApplyChunkFn, ErrorFn]:
        col, row, original = task.payload
        cell = sheet.getCellByPosition(col, row)
        cell_kind = cell.getType()

        # Keep Calc writes local to the cell adapter; the shared code only drives the stream.
        accumulated_text = [""]
        if not is_edit:
            accumulated_text[0] = task.prompt

        # Only a text cell stays text; numbers and formulas go through setFormula
        # so an edited 42 -> 43 stays numeric.
        def _write_cell(text: str) -> None:
            if cell_kind == TEXT:
                cell.setString(text)
            else:
                cell.setFormula(text)

        def apply_chunk(chunk_text: str, is_thinking: bool = False) -> None:
            if not is_thinking:
                accumulated_text[0] += chunk_text
                _write_cell(accumulated_text[0])

        def on_error(e: BaseException) -> None:
            if original is not None:
                _write_cell(original)
            msgbox(ctx, title, format_error_message(e))

        return apply_chunk, on_error

    # The runner returns before later cells are written. One context covers
    # every chunk until the run stops. A user edit during the stream joins
    # this step (WriterCompoundUndo).
    undo = WriterCompoundUndo(model, title)

    def _close_undo() -> None:
        undo.close()

    try:
        stream_completion_tasks(ctx, client, tasks, prepare_task, _close_undo)
    except BaseException:
        _close_undo()
        raise
