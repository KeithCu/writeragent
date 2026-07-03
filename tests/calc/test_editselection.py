from types import SimpleNamespace
from unittest.mock import MagicMock, patch


class _FakeCell:
    def __init__(self, value=""):
        self.value = value

    def setString(self, value):
        self.value = value


class _FakeCellRange:
    def __init__(self, data):
        self._data = data

    def getDataArray(self):
        return self._data


class _FakeSheet:
    def __init__(self, data):
        self.data = data
        self.cells = {
            (col, row): _FakeCell(str(value) if value != "" and value is not None else "")
            for row, values in enumerate(data)
            for col, value in enumerate(values)
        }

    def getCellRangeByPosition(self, start_col, start_row, end_col, end_row):
        return _FakeCellRange(self.data)

    def getCellByPosition(self, col, row):
        return self.cells[(col, row)]


def _model_for_data(data):
    sheet = _FakeSheet(data)
    selection = MagicMock()
    selection.getRangeAddress.return_value = SimpleNamespace(StartColumn=0, EndColumn=len(data[0]) - 1, StartRow=0, EndRow=len(data) - 1)
    controller = SimpleNamespace(ActiveSheet=sheet, Selection=selection)
    return SimpleNamespace(CurrentController=controller), sheet


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
    assert sheet.getCellByPosition(0, 0).value == "A plus"
    assert sheet.getCellByPosition(1, 0).value == ""


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
    assert sheet.getCellByPosition(0, 0).value == "Original"
    msgbox.assert_called_once()
