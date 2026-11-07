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

from __future__ import annotations

from typing import Any, Callable

from plugin.calc.calc_utils import FORMULA, TEXT
from plugin.writer.edit_review import build_writer_rewrite_prompt, strip_echoed_edit_scaffolding
from plugin.chatbot.dialogs import msgbox
from plugin.chatbot.selection import (
    ApplyChunkFn,
    ErrorFn,
    StreamCompletionTask,
    bind_selection_cancel,
    create_validated_client,
    end_selection_cancel,
    prompt_for_edit_instructions,
    stream_completion_tasks,
)
from plugin.doc.compound_undo import WriterCompoundUndo
from plugin.framework.config import get_config
from plugin.framework.errors import format_error_message
from plugin.framework.i18n import _

# Caps queued LLM tasks so a huge selection cannot queue minutes of requests.
_MAX_CELLS = 250


def do_calc_extend_edit(
    ctx: Any,
    model: Any,
    is_edit: bool,
    *,
    preset_instruction: str | None = None,
    sidebar_notify: Callable[[str], None] | None = None,
    allow_whole_document: bool = False,
    host: Any | None = None,
) -> None:
    sheet = model.CurrentController.ActiveSheet
    selection = model.CurrentController.Selection

    user_input = ""
    extra_instructions = ""
    title = _("WriterAgent: Edit Selection (Calc)") if is_edit else _("WriterAgent: Extend Selection (Calc)")
    if is_edit:
        if preset_instruction is not None:
            user_input = preset_instruction
        else:
            edit_request = prompt_for_edit_instructions(ctx, title)
            if edit_request is None:
                return
            user_input, extra_instructions = edit_request

    area = selection.getRangeAddress()

    extend_sys = get_config("extend_selection_system_prompt")
    extend_max = get_config("chatbot.extend_selection_max_tokens")
    edit_sys = extra_instructions or get_config("edit_selection_system_prompt")
    edit_max = get_config("chatbot.edit_selection_max_new_tokens")

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
        # Calc always has an active cell; slash edit with allow_whole_document uses the used
        # range when the selection misses the data rectangle (same flag as Writer whole-body edit).
        # The edit is one compound undo step (same as other Calc cell edits) and still capped at
        # _MAX_CELLS non-empty cells below.
        if is_edit and allow_whole_document:
            start_col = used_area.StartColumn
            end_col = used_area.EndColumn
            start_row = used_area.StartRow
            end_row = used_area.EndRow
        if start_col > end_col or start_row > end_row:
            if preset_instruction is not None and sidebar_notify is not None:
                sidebar_notify(_("Select one or more non-empty cells."))
                return
            msgbox(ctx, title, _("Select one or more non-empty cells."))
            return

    cell_range = sheet.getCellRangeByPosition(start_col, start_row, end_col, end_row)
    formula_array = cell_range.getFormulaArray()

    saw_skipped_formula = False
    saw_nonempty_for_formula_msg = False

    for r_i, row_vals in enumerate(formula_array):
        for c_i, raw_val in enumerate(row_vals):
            if len(tasks) >= _MAX_CELLS:
                break
            # Use the formula array, not getString(), so formulas are not
            # replaced by their evaluated display text.
            cell_text = "" if raw_val is None else str(raw_val)
            if not cell_text:
                continue
            col_abs = start_col + c_i
            row_abs = start_row + r_i
            if is_edit:
                cell = sheet.getCellByPosition(col_abs, row_abs)
                if cell.getType() == FORMULA:
                    saw_nonempty_for_formula_msg = True
                    saw_skipped_formula = True
                    continue
            tasks.append(
                StreamCompletionTask(
                    build_writer_rewrite_prompt(cell_text, user_input) if is_edit else cell_text,
                    edit_sys if is_edit else extend_sys,
                    len(cell_text) + edit_max if is_edit else extend_max,
                    (col_abs, row_abs, cell_text if is_edit else None),
                )
            )
        if len(tasks) >= _MAX_CELLS:
            break

    if len(tasks) >= _MAX_CELLS:
        msgbox(ctx, title, _("Only the first {0} non-empty cells are edited.").format(_MAX_CELLS))

    if not tasks:
        if is_edit and preset_instruction is not None and sidebar_notify is not None:
            if saw_nonempty_for_formula_msg and saw_skipped_formula:
                sidebar_notify(_("Formula cells are skipped."))
                return
            sidebar_notify(_("Select one or more non-empty cells."))
            return
        msgbox(ctx, title, _("Select one or more non-empty cells."))
        return

    client = create_validated_client(ctx, title)
    if client is None:
        return

    from plugin.doc.agent_undo import record_agent_turn_after, record_agent_turn_before

    record_agent_turn_before(model)

    completion_meta: dict[str, Any] = {}
    produced_text = [False]

    def prepare_task(task: StreamCompletionTask) -> tuple[ApplyChunkFn, ErrorFn]:
        col, row, original = task.payload
        cell = sheet.getCellByPosition(col, row)
        cell_kind = cell.getType()

        # Keep Calc writes local to the cell adapter; the shared code only drives the stream.
        accumulated_text = [task.prompt if not is_edit else ""]

        # Only a text cell stays text; numbers and formulas go through setFormula
        # so an edited 42 -> 43 stays numeric.
        def _write_cell(text: str) -> None:
            if cell_kind == TEXT:
                cell.setString(text)
            else:
                cell.setFormula(text)

        def apply_chunk(chunk_text: str, is_thinking: bool = False) -> None:
            if not is_thinking:
                if chunk_text.strip():
                    produced_text[0] = True
                accumulated_text[0] += chunk_text
                _write_cell(strip_echoed_edit_scaffolding(accumulated_text[0]))

        def on_error(e: BaseException) -> None:
            if original is not None:
                _write_cell(original)
            msgbox(ctx, title, format_error_message(e))

        return apply_chunk, on_error

    # The runner returns before later cells are written. One context covers
    # every chunk until the run stops. A user edit during the stream joins
    # this step (WriterCompoundUndo).
    undo = WriterCompoundUndo(model, title)

    def on_finished() -> None:
        try:
            undo.close()
            record_agent_turn_after(model)
            if (
                is_edit
                and preset_instruction is not None
                and sidebar_notify is not None
                and not produced_text[0]
            ):
                finish = completion_meta.get("finish_reason")
                if finish == "length":
                    sidebar_notify(
                        _("The rewrite ran out of tokens before producing text. Try again or shorten the selection.")
                    )
                elif finish == "stop":
                    sidebar_notify(_("The model returned no text. Try again."))
        finally:
            end_selection_cancel(arm)

    arm = bind_selection_cancel(host, model)
    try:
        stream_completion_tasks(
            ctx,
            client,
            tasks,
            prepare_task,
            on_finished,
            cancel=arm.scope,
            completion_out=completion_meta,
        )
    except BaseException:
        try:
            undo.close()
            record_agent_turn_after(model)
        finally:
            end_selection_cancel(arm)
        raise
