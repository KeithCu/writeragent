from unittest.mock import MagicMock, patch

from plugin.tests.testing_utils import CalcDocStub


def _model_for_data(data):
    """Build a CalcDocStub with selection covering the full seeded data rectangle."""
    end_col = max(0, len(data[0]) - 1) if data and data[0] else 0
    end_row = max(0, len(data) - 1) if data else 0
    doc = CalcDocStub(data=data)
    sheet = doc.getSheets().getByIndex(0)
    doc.CurrentController.Selection = sheet.getCellRangeByPosition(0, 0, end_col, end_row)
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

    model, sheet = _model_for_data((("=1+1", ""),))
    stream_calls = []

    def fake_run_stream(ctx, client, prompt, system_prompt, max_tokens, apply_chunk_fn, on_done_fn, on_error_fn):
        stream_calls.append((prompt, system_prompt, max_tokens))
        apply_chunk_fn("+2", False)
        on_done_fn()

    with patch("plugin.calc.editselection.get_config_str", side_effect=_config_str), \
         patch("plugin.calc.editselection.get_config_int", side_effect=_config_int), \
         patch("plugin.calc.editselection.create_validated_client", return_value=object()), \
         patch("plugin.chatbot.selection.run_stream_completion_async", side_effect=fake_run_stream):
        do_calc_extend_edit(MagicMock(), model, MagicMock(), is_edit=False)

    assert stream_calls == [("=1+1", "extend system", 50)]
    assert sheet.getCellByPosition(0, 0).getFormula() == "=1+1+2"
    assert sheet.getCellByPosition(1, 0).getFormula() == ""


def test_calc_edit_uses_extra_prompt_and_restores_original_on_error():
    from plugin.calc.editselection import do_calc_extend_edit

    model, sheet = _model_for_data((("=1+1",),))
    input_box = MagicMock(return_value=("shorten", "extra system"))
    error = RuntimeError("provider failed")
    stream_calls = []

    def fake_run_stream(ctx, client, prompt, system_prompt, max_tokens, apply_chunk_fn, on_done_fn, on_error_fn):
        stream_calls.append((prompt, system_prompt, max_tokens))
        apply_chunk_fn("=2", False)
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
    assert stream_calls[0][2] == len("=1+1") + 7
    assert "ORIGINAL VERSION:\n=1+1" in stream_calls[0][0]
    assert sheet.getCellByPosition(0, 0).getFormula() == "=1+1"
    msgbox.assert_called_once()
