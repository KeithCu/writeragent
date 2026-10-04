from unittest.mock import MagicMock, patch

from plugin.tests.testing_utils import CalcDocStub


def _model_for_data(data):
    """Build a CalcDocStub with selection covering the full seeded data rectangle."""
    end_col = max(0, len(data[0]) - 1) if data and data[0] else 0
    end_row = max(0, len(data) - 1) if data else 0
    doc = CalcDocStub(data=data)
    sheet = doc.getSheets().getByIndex(0)
    doc.CurrentController.Selection = sheet.getCellRangeByPosition(0, 0, end_col, end_row)

    # Mock createCursor for used area bounds
    cursor = MagicMock()
    cursor.getRangeAddress.return_value = MagicMock(StartColumn=0, StartRow=0, EndColumn=end_col, EndRow=end_row)
    sheet.createCursor = MagicMock(return_value=cursor)

    return doc, sheet


def _config_str(key):
    return {
        "extend_selection_system_prompt": "extend system",
        "edit_selection_system_prompt": "edit system",
    }[key]


def _config_int(key):
    return {
        "extend_selection_max_tokens": 50,
        "edit_selection_max_new_tokens": 7,
    }[key]


def test_calc_extend_streams_each_non_empty_cell():
    from plugin.calc.editselection import do_calc_extend_edit

    model, sheet = _model_for_data((("A", ""),))
    stream_calls = []

    def fake_run_stream(ctx, client, prompt, system_prompt, max_tokens, apply_chunk_fn, on_done_fn, on_error_fn):
        stream_calls.append((prompt, system_prompt, max_tokens))
        apply_chunk_fn(" plus", False)
        on_done_fn()

    with patch("plugin.calc.editselection.get_config_str", side_effect=_config_str), \
         patch("plugin.calc.editselection.get_config_int", side_effect=_config_int), \
         patch("plugin.calc.editselection.create_validated_client", return_value=object()), \
         patch("plugin.chatbot.selection.run_stream_completion_async", side_effect=fake_run_stream):
        do_calc_extend_edit(MagicMock(), model, MagicMock(), is_edit=False)

    assert stream_calls == [("A", "extend system", 50)]
    assert sheet.getCellByPosition(0, 0).getString() == "A plus"
    assert sheet.getCellByPosition(1, 0).getString() == ""


def test_calc_edit_skips_empty_cells():
    from plugin.calc.editselection import do_calc_extend_edit

    model, sheet = _model_for_data((("Original", ""),))
    input_box = MagicMock(return_value=("shorten", "extra system"))
    stream_calls = []

    def fake_run_stream(ctx, client, prompt, system_prompt, max_tokens, apply_chunk_fn, on_done_fn, on_error_fn):
        stream_calls.append((prompt, system_prompt, max_tokens))
        apply_chunk_fn("Edited", False)
        on_done_fn()

    with patch("plugin.calc.editselection.get_config_str", side_effect=_config_str), \
         patch("plugin.calc.editselection.get_config_int", side_effect=_config_int), \
         patch("plugin.calc.editselection.create_validated_client", return_value=object()), \
         patch("plugin.chatbot.selection.set_config"), \
         patch("plugin.chatbot.selection.update_lru_history"), \
         patch("plugin.chatbot.selection.run_stream_completion_async", side_effect=fake_run_stream):
        do_calc_extend_edit(MagicMock(), model, input_box, is_edit=True)

    assert len(stream_calls) == 1
    assert "Original" in stream_calls[0][0]


def test_calc_edit_uses_extra_prompt_and_restores_original_on_error():
    from plugin.calc.editselection import do_calc_extend_edit

    model, sheet = _model_for_data((("Original",),))
    input_box = MagicMock(return_value=("shorten", "extra system"))
    error = RuntimeError("provider failed")
    stream_calls = []

    def fake_run_stream(ctx, client, prompt, system_prompt, max_tokens, apply_chunk_fn, on_done_fn, on_error_fn):
        stream_calls.append((prompt, system_prompt, max_tokens))
        apply_chunk_fn("Partial", False)
        on_error_fn(error)

    with patch("plugin.calc.editselection.get_config_str", side_effect=_config_str), \
         patch("plugin.calc.editselection.get_config_int", side_effect=_config_int), \
         patch("plugin.calc.editselection.create_validated_client", return_value=object()), \
         patch("plugin.calc.editselection.msgbox") as msgbox, \
         patch("plugin.chatbot.selection.set_config"), \
         patch("plugin.chatbot.selection.update_lru_history"), \
         patch("plugin.chatbot.selection.run_stream_completion_async", side_effect=fake_run_stream):
        do_calc_extend_edit(MagicMock(), model, input_box, is_edit=True)

    assert stream_calls[0][1] == "extra system"
    assert stream_calls[0][2] == len("Original") + 7
    assert "ORIGINAL VERSION:\nOriginal" in stream_calls[0][0]
    assert sheet.getCellByPosition(0, 0).getString() == "Original"
    msgbox.assert_called_once()
