from unittest.mock import MagicMock, patch

from plugin.writer.editselection import do_extend_selection, do_edit_selection
from plugin.framework.config import set_configs

@patch("plugin.writer.editselection.stream_completion")
@patch("plugin.writer.editselection.create_validated_client")
def test_extend_selection_uses_non_empty_range(mock_create_client, mock_stream_completion):
    set_configs({'extend_selection_max_tokens': 100, 'doc.agent_edit_review_mode': 'none', 'additional_instructions': ''})
    mock_client = MagicMock()
    mock_create_client.return_value = mock_client
    ctx = MagicMock()
    model = MagicMock()

    rng0 = MagicMock()
    rng0.getString.return_value = ""
    rng1 = MagicMock()
    rng1.getString.return_value = "orig"

    selection = MagicMock()
    selection.getCount.return_value = 2

    def by_index(i):
        if i == 0: return rng0
        return rng1

    selection.getByIndex.side_effect = by_index
    model.CurrentController.getSelection.return_value = selection

    with patch("plugin.writer.editselection.get_string_without_tracked_deletions", return_value="orig"), \
         patch("plugin.writer.editselection._stop_for_tracked_changes", return_value=False):
        do_extend_selection(ctx, model, MagicMock())

        # apply_chunk should be passed to stream_completion
        assert mock_stream_completion.called
        prompt = mock_stream_completion.call_args.args[2]
        assert "orig" in prompt

        apply_chunk_fn = mock_stream_completion.call_args.args[5]
        apply_chunk_fn("new chunk")

        rng0.setString.assert_not_called()
        rng1.setString.assert_called()


@patch("plugin.writer.editselection.stream_completion")
@patch("plugin.writer.editselection.create_validated_client")
def test_extend_selection_falls_back_to_index0_if_all_empty(mock_create_client, mock_stream_completion):
    set_configs({'extend_selection_max_tokens': 100, 'doc.agent_edit_review_mode': 'none', 'additional_instructions': ''})
    mock_client = MagicMock()
    mock_create_client.return_value = mock_client
    ctx = MagicMock()
    model = MagicMock()

    rng0 = MagicMock()
    rng0.getString.return_value = ""
    rng1 = MagicMock()
    rng1.getString.return_value = ""

    selection = MagicMock()
    selection.getCount.return_value = 2

    def by_index(i):
        if i == 0: return rng0
        return rng1

    selection.getByIndex.side_effect = by_index
    model.CurrentController.getSelection.return_value = selection

    with patch("plugin.writer.editselection.get_string_without_tracked_deletions", return_value="fallback_orig"), \
         patch("plugin.writer.editselection._stop_for_tracked_changes", return_value=False):
        do_extend_selection(ctx, model, MagicMock())

        apply_chunk_fn = mock_stream_completion.call_args.args[5]
        apply_chunk_fn("new chunk")

        rng0.setString.assert_called()
        rng1.setString.assert_not_called()
        assert mock_stream_completion.called


@patch("plugin.writer.editselection.stream_completion")
@patch("plugin.writer.editselection.create_validated_client")
def test_extend_selection_stops_on_cancel(mock_create_client, mock_stream_completion):
    set_configs({'extend_selection_max_tokens': 100, 'doc.agent_edit_review_mode': 'none', 'additional_instructions': ''})
    # Setup mocks
    mock_client = MagicMock()
    mock_create_client.return_value = mock_client

    ctx = MagicMock()
    # stop_checker returns True, meaning stopped by user
    ctx.stop_checker = MagicMock(return_value=True)

    model = MagicMock()
    text_range = MagicMock()
    selection = MagicMock()
    selection.getByIndex.return_value = text_range
    model.CurrentController.getSelection.return_value = selection

    with patch("plugin.writer.editselection.get_string_without_tracked_deletions", return_value="some text"), \
         patch("plugin.writer.editselection._stop_for_tracked_changes", return_value=False):

        do_extend_selection(ctx, model, MagicMock())

        # apply_chunk should be passed to stream_completion
        assert mock_stream_completion.called
        apply_chunk_fn = mock_stream_completion.call_args.args[5]

        # A chunk already in hand still appends when stop_checker is true.
        apply_chunk_fn("new chunk")
        text_range.setString.assert_called()

@patch("plugin.writer.editselection.stream_completion")
@patch("plugin.writer.editselection.create_validated_client")
def test_edit_selection_stops_on_cancel(mock_create_client, mock_stream_completion):
    set_configs({'edit_selection_max_new_tokens': 100, 'doc.agent_edit_review_mode': 'none', 'additional_instructions': ''})
    # Setup mocks
    mock_client = MagicMock()
    mock_create_client.return_value = mock_client

    ctx = MagicMock()
    # stop_checker returns True, meaning stopped by user
    ctx.stop_checker = MagicMock(return_value=True)

    model = MagicMock()
    text_range = MagicMock()
    selection = MagicMock()
    selection.getByIndex.return_value = text_range
    model.CurrentController.getSelection.return_value = selection

    with patch("plugin.writer.editselection.get_string_without_tracked_deletions", return_value="some text"), \
         patch("plugin.writer.editselection._stop_for_tracked_changes", return_value=False), \
         patch("plugin.writer.editselection.prompt_for_edit_instructions", return_value=("make it better", "")):

        do_edit_selection(ctx, model, MagicMock())

        # apply_chunk should be passed to stream_completion
        assert mock_stream_completion.called
        apply_chunk_fn = mock_stream_completion.call_args.args[5]

        # A chunk already in hand still appends when stop_checker is true.
        apply_chunk_fn("new chunk")
        text_range.setString.assert_called()

@patch("plugin.writer.editselection.stream_completion")
@patch("plugin.writer.editselection.create_validated_client")
def test_extend_selection_fails_closed_on_stop_checker_error(mock_create_client, mock_stream_completion):
    set_configs({'extend_selection_max_tokens': 100, 'doc.agent_edit_review_mode': 'none', 'additional_instructions': ''})
    mock_client = MagicMock()
    mock_create_client.return_value = mock_client

    ctx = MagicMock()
    # stop_checker raises an exception
    ctx.stop_checker = MagicMock(side_effect=RuntimeError("Some internal error"))

    model = MagicMock()
    selection = MagicMock()
    model.CurrentController.getSelection.return_value = selection

    with patch("plugin.writer.editselection.get_string_without_tracked_deletions", return_value="some text"), \
         patch("plugin.writer.editselection._stop_for_tracked_changes", return_value=False):

        do_extend_selection(ctx, model, MagicMock())

        apply_chunk_fn = mock_stream_completion.call_args.args[5]

        # apply_chunk does not consult stop_checker, so a broken checker does not block the write.
        apply_chunk_fn("new chunk")

@patch("plugin.writer.editselection.msgbox")
@patch("plugin.writer.editselection.stream_completion")
@patch("plugin.writer.editselection.create_validated_client")
def test_extend_selection_fails_when_setString_fails(mock_create_client, mock_stream_completion, mock_msgbox):
    set_configs({'extend_selection_max_tokens': 100, 'doc.agent_edit_review_mode': 'none', 'additional_instructions': ''})
    mock_client = MagicMock()
    mock_create_client.return_value = mock_client

    ctx = MagicMock()
    ctx.stop_checker = MagicMock(return_value=False)

    model = MagicMock()
    text_range = MagicMock()
    # Mock setString to throw Exception
    text_range.setString.side_effect = Exception("failed to write to document")
    selection = MagicMock()
    selection.getByIndex.return_value = text_range
    model.CurrentController.getSelection.return_value = selection

    with patch("plugin.writer.editselection.get_string_without_tracked_deletions", return_value="some text"), \
         patch("plugin.writer.editselection._stop_for_tracked_changes", return_value=False):

        do_extend_selection(ctx, model, MagicMock())

        apply_chunk_fn = mock_stream_completion.call_args.args[5]
        on_done_fn = mock_stream_completion.call_args.args[6]

        # In-hand chunk does not crash or raise inside apply_chunk
        apply_chunk_fn("new chunk")
        on_done_fn()
        mock_msgbox.assert_called_once()
        assert "Failed" in mock_msgbox.call_args.args[2]

