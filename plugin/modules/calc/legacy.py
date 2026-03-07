"""Legacy operations for Calc (Extend/Edit Selection)."""
from plugin.framework.config import get_config, get_api_config, validate_api_config
from plugin.modules.http.client import format_error_message, LlmClient
from plugin.framework.async_stream import run_stream_completion_async
from plugin.framework.dialogs import msgbox

def do_calc_extend_edit(ctx, model, input_box_fn, is_edit):
    sheet = model.CurrentController.ActiveSheet
    selection = model.CurrentController.Selection

    user_input = ""
    if is_edit:
        user_input, _ = input_box_fn(ctx, "Please enter edit instructions!", "Input", "")
        if not user_input:
            return

    area = selection.getRangeAddress()
    col_range = range(area.StartColumn, area.EndColumn + 1)
    row_range = range(area.StartRow, area.EndRow + 1)

    extend_sys = get_config(ctx, "extend_selection_system_prompt", "")
    extend_max = get_config(ctx, "extend_selection_max_tokens", 70)
    edit_sys = get_config(ctx, "edit_selection_system_prompt", "")
    try:
        edit_max = int(get_config(ctx, "edit_selection_max_new_tokens", 0))
    except (TypeError, ValueError):
        edit_max = 0

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
        msgbox(ctx, "WriterAgent: Edit Selection (Calc)" if is_edit else "WriterAgent: Extend Selection (Calc)", err_msg)
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
            msgbox(ctx, "WriterAgent: Edit Selection (Calc)" if is_edit else "WriterAgent: Extend Selection (Calc)", format_error_message(e))

        run_stream_completion_async(
            ctx, client, prompt, system_prompt, max_tokens,
            apply_chunk, on_done, on_error
        )

    run_next_cell()
