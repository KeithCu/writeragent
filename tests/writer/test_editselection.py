from unittest.mock import MagicMock, patch

from plugin.writer.editselection import do_extend_selection, do_edit_selection
from plugin.framework.config import set_configs


@patch("plugin.writer.editselection.stream_completion")
@patch("plugin.writer.editselection.create_validated_client")
def test_extend_selection_applies_chunk_when_stopped(mock_create_client, mock_stream_completion):
    set_configs({'extend_selection_max_tokens': 100, 'doc.agent_edit_review_mode': 'none', 'additional_instructions': ''})
    mock_client = MagicMock()
    mock_create_client.return_value = mock_client

    ctx = MagicMock()
    ctx.stop_checker = MagicMock(return_value=True)

    model = MagicMock()
    text_range = MagicMock()
    selection = MagicMock()
    selection.getByIndex.return_value = text_range
    model.CurrentController.getSelection.return_value = selection

    with patch("plugin.writer.editselection.get_string_without_tracked_deletions", return_value="some text"), \
         patch("plugin.writer.editselection._stop_for_tracked_changes", return_value=False):

        do_extend_selection(ctx, model, MagicMock())

        assert mock_stream_completion.called
        apply_chunk_fn = mock_stream_completion.call_args.args[5]

        # In-hand chunk should land without raising an exception
        apply_chunk_fn("new chunk")
        text_range.setString.assert_called_with("some textnew chunk")


@patch("plugin.writer.editselection.stream_completion")
@patch("plugin.writer.editselection.create_validated_client")
def test_edit_selection_applies_chunk_when_stopped(mock_create_client, mock_stream_completion):
    set_configs({'edit_selection_max_new_tokens': 100, 'doc.agent_edit_review_mode': 'none', 'additional_instructions': ''})
    mock_client = MagicMock()
    mock_create_client.return_value = mock_client

    ctx = MagicMock()
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

        assert mock_stream_completion.called
        apply_chunk_fn = mock_stream_completion.call_args.args[5]

        apply_chunk_fn("new chunk")
        text_range.setString.assert_called_with("new chunk")

