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
def test_extend_selection_in_hand_chunk_lands_even_with_stop_checker(mock_create_client, mock_stream_completion):
    set_configs({'extend_selection_max_tokens': 100, 'doc.agent_edit_review_mode': 'none', 'additional_instructions': ''})
    # Setup mocks
    mock_client = MagicMock()
    mock_create_client.return_value = mock_client

    ctx = MagicMock()
    # Extend/Edit have no Stop control; this stands in for a user clicking Stop.
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
def test_edit_selection_in_hand_chunk_lands_even_with_stop_checker(mock_create_client, mock_stream_completion):
    set_configs({'edit_selection_max_new_tokens': 100, 'doc.agent_edit_review_mode': 'none', 'additional_instructions': ''})
    # Setup mocks
    mock_client = MagicMock()
    mock_create_client.return_value = mock_client

    ctx = MagicMock()
    # Extend/Edit have no Stop control; this stands in for a user clicking Stop.
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


@patch("plugin.writer.editselection.msgbox")
@patch("plugin.writer.editselection.stream_completion")
@patch("plugin.writer.editselection.create_validated_client")
def test_extend_selection_on_error_long_error_does_not_raise_contract_error(mock_create_client, mock_stream_completion, mock_msgbox):
    from plugin.framework.deal_shim import DEAL_MAX_MSGID
    from plugin.framework.errors import format_error_message

    set_configs({'extend_selection_max_tokens': 100, 'doc.agent_edit_review_mode': 'none', 'additional_instructions': ''})
    mock_create_client.return_value = MagicMock()
    ctx = MagicMock()
    model = MagicMock()
    selection = MagicMock()
    selection.getByIndex.return_value = MagicMock()
    model.CurrentController.getSelection.return_value = selection

    long_err = ValueError("x" * (DEAL_MAX_MSGID + 50))
    with patch("plugin.writer.editselection.get_string_without_tracked_deletions", return_value="some text"), \
         patch("plugin.writer.editselection._stop_for_tracked_changes", return_value=False):
        do_extend_selection(ctx, model, MagicMock())
        on_error_fn = mock_stream_completion.call_args.args[7]
        on_error_fn(long_err)
        mock_msgbox.assert_called_once()
        assert mock_msgbox.call_args.args[2] == format_error_message(long_err)


@patch("plugin.writer.editselection.msgbox")
@patch("plugin.writer.editselection.stream_completion")
@patch("plugin.writer.editselection.create_validated_client")
def test_edit_selection_on_error_long_error_does_not_raise_contract_error(mock_create_client, mock_stream_completion, mock_msgbox):
    from plugin.framework.deal_shim import DEAL_MAX_MSGID
    from plugin.framework.errors import format_error_message

    set_configs({'edit_selection_max_new_tokens': 100, 'doc.agent_edit_review_mode': 'none', 'additional_instructions': ''})
    mock_create_client.return_value = MagicMock()
    ctx = MagicMock()
    model = MagicMock()
    selection = MagicMock()
    selection.getByIndex.return_value = MagicMock()
    model.CurrentController.getSelection.return_value = selection

    long_err = ValueError("x" * (DEAL_MAX_MSGID + 50))
    with patch("plugin.writer.editselection.get_string_without_tracked_deletions", return_value="some text"), \
         patch("plugin.writer.editselection._stop_for_tracked_changes", return_value=False), \
         patch("plugin.writer.editselection.prompt_for_edit_instructions", return_value=("make it better", "")):
        do_edit_selection(ctx, model, MagicMock())
        on_error_fn = mock_stream_completion.call_args.args[7]
        on_error_fn(long_err)
        mock_msgbox.assert_called_once()
        assert mock_msgbox.call_args.args[2] == format_error_message(long_err)


@patch("plugin.writer.editselection.msgbox")
@patch("plugin.writer.editselection.get_string_without_tracked_deletions")
def test_extend_selection_none_selection_shows_message_and_returns(mock_get_str, mock_msgbox):
    ctx = MagicMock()
    model = MagicMock()
    model.CurrentController.getSelection.return_value = None

    do_extend_selection(ctx, model, MagicMock())

    mock_msgbox.assert_called_once()
    assert "Select text first" in mock_msgbox.call_args.args[2]
    mock_get_str.assert_not_called()


@patch("plugin.writer.editselection.msgbox")
@patch("plugin.writer.editselection.get_string_without_tracked_deletions")
def test_edit_selection_none_selection_shows_message_and_returns(mock_get_str, mock_msgbox):
    ctx = MagicMock()
    model = MagicMock()
    model.CurrentController.getSelection.return_value = None

    do_edit_selection(ctx, model, MagicMock())

    mock_msgbox.assert_called_once()
    assert "Select text first" in mock_msgbox.call_args.args[2]
    mock_get_str.assert_not_called()


@patch("plugin.writer.editselection.msgbox")
@patch("plugin.writer.editselection.prompt_for_edit_instructions")
@patch("plugin.writer.editselection.get_string_without_tracked_deletions", return_value="")
def test_edit_selection_empty_text_shows_message_before_prompt(mock_get_str, mock_prompt, mock_msgbox):
    ctx = MagicMock()
    model = MagicMock()
    text_range = MagicMock()
    text_range.getString.return_value = "something"
    selection = MagicMock()
    selection.getCount.return_value = 1
    selection.getByIndex.return_value = text_range
    model.CurrentController.getSelection.return_value = selection

    with patch("plugin.writer.editselection._stop_for_tracked_changes", return_value=False):
        do_edit_selection(ctx, model, MagicMock())

    mock_msgbox.assert_called_once()
    assert "Select text first" in mock_msgbox.call_args.args[2]
    mock_prompt.assert_not_called()


@patch("plugin.writer.editselection.stream_completion")
@patch("plugin.writer.editselection.prompt_for_edit_instructions")
@patch("plugin.writer.editselection.create_validated_client", return_value=None)
def test_edit_selection_no_client_skips_prompt(mock_create_client, mock_prompt, mock_stream):
    ctx = MagicMock()
    model = MagicMock()
    text_range = MagicMock()
    text_range.getString.return_value = "orig"
    selection = MagicMock()
    selection.getCount.return_value = 1
    selection.getByIndex.return_value = text_range
    model.CurrentController.getSelection.return_value = selection

    with patch("plugin.writer.editselection.get_string_without_tracked_deletions", return_value="orig"), \
         patch("plugin.writer.editselection._stop_for_tracked_changes", return_value=False):
        do_edit_selection(ctx, model, MagicMock())

    mock_prompt.assert_not_called()
    mock_stream.assert_not_called()


@patch("plugin.writer.editselection.msgbox")
@patch("plugin.writer.editselection.stream_completion")
@patch("plugin.writer.editselection.get_string_without_tracked_deletions", return_value="")
def test_extend_selection_empty_text_shows_message(mock_get_str, mock_stream, mock_msgbox):
    ctx = MagicMock()
    model = MagicMock()
    text_range = MagicMock()
    text_range.getString.return_value = "something"
    selection = MagicMock()
    selection.getCount.return_value = 1
    selection.getByIndex.return_value = text_range
    model.CurrentController.getSelection.return_value = selection

    with patch("plugin.writer.editselection._stop_for_tracked_changes", return_value=False):
        do_extend_selection(ctx, model, MagicMock())

    mock_msgbox.assert_called_once()
    assert "Select text first" in mock_msgbox.call_args.args[2]
    mock_stream.assert_not_called()


@patch("plugin.writer.editselection.stream_completion")
@patch("plugin.writer.editselection.create_validated_client")
@patch("plugin.writer.editselection.get_current_endpoint", return_value="ep")
@patch("plugin.writer.editselection.get_text_model", return_value="m")
@patch("plugin.writer.editselection.update_lru_history")
def test_both_commands_write_model_lru_once(
    mock_update_lru, mock_get_model, mock_get_ep, mock_create_client, mock_stream
):
    mock_create_client.return_value = MagicMock()
    ctx = MagicMock()
    model = MagicMock()
    text_range = MagicMock()
    text_range.getString.return_value = "orig"
    selection = MagicMock()
    selection.getCount.return_value = 1
    selection.getByIndex.return_value = text_range
    model.CurrentController.getSelection.return_value = selection

    with patch("plugin.writer.editselection.get_string_without_tracked_deletions", return_value="orig"), \
         patch("plugin.writer.editselection._stop_for_tracked_changes", return_value=False), \
         patch("plugin.writer.editselection.prompt_for_edit_instructions", return_value=("rewrite", "")):

        do_extend_selection(ctx, model, MagicMock())
        assert mock_update_lru.call_args_list == [(("m", "model_lru", "ep"),)]

        mock_update_lru.reset_mock()

        do_edit_selection(ctx, model, MagicMock())
        assert mock_update_lru.call_args_list == [(("m", "model_lru", "ep"),)]


@patch("plugin.writer.editselection.stream_completion")
@patch("plugin.writer.editselection.create_validated_client")
def test_extend_selection_uses_extend_system_prompt(mock_create_client, mock_stream):
    set_configs({
        "extend_selection_max_tokens": 100,
        "doc.agent_edit_review_mode": "none",
        "additional_instructions": "chat extra",
        "extend_selection_system_prompt": "extend sys",
    })
    mock_create_client.return_value = MagicMock()
    ctx = MagicMock()
    model = MagicMock()
    text_range = MagicMock()
    text_range.getString.return_value = "hello selection"
    selection = MagicMock()
    selection.getCount.return_value = 1
    selection.getByIndex.return_value = text_range
    model.CurrentController.getSelection.return_value = selection

    with patch("plugin.writer.editselection.get_string_without_tracked_deletions", return_value="hello selection"), \
         patch("plugin.writer.editselection._stop_for_tracked_changes", return_value=False):
        do_extend_selection(ctx, model, MagicMock())

    assert mock_stream.called
    assert mock_stream.call_args.args[2] == "hello selection"
    assert mock_stream.call_args.args[3] == "extend sys"


@patch("plugin.writer.editselection.stream_completion")
@patch("plugin.writer.editselection.create_validated_client")
def test_edit_selection_uses_edit_system_prompt(mock_create_client, mock_stream):
    set_configs({
        "edit_selection_max_new_tokens": 100,
        "doc.agent_edit_review_mode": "none",
        "additional_instructions": "chat extra",
        "edit_selection_system_prompt": "edit sys",
    })
    mock_create_client.return_value = MagicMock()
    ctx = MagicMock()
    model = MagicMock()
    text_range = MagicMock()
    text_range.getString.return_value = "hello selection"
    selection = MagicMock()
    selection.getCount.return_value = 1
    selection.getByIndex.return_value = text_range
    model.CurrentController.getSelection.return_value = selection

    with patch("plugin.writer.editselection.get_string_without_tracked_deletions", return_value="hello selection"), \
         patch("plugin.writer.editselection._stop_for_tracked_changes", return_value=False), \
         patch("plugin.writer.editselection.prompt_for_edit_instructions", return_value=("shorter", "")):
        do_edit_selection(ctx, model, MagicMock())

    assert mock_stream.called
    assert mock_stream.call_args.args[3] == "edit sys"


@patch("plugin.writer.editselection.stream_completion")
@patch("plugin.writer.editselection.create_validated_client")
def test_edit_selection_dialog_extras_win_over_config(mock_create_client, mock_stream):
    set_configs({
        "edit_selection_max_new_tokens": 100,
        "doc.agent_edit_review_mode": "none",
        "additional_instructions": "chat extra",
        "edit_selection_system_prompt": "edit sys",
    })
    mock_create_client.return_value = MagicMock()
    ctx = MagicMock()
    model = MagicMock()
    text_range = MagicMock()
    text_range.getString.return_value = "hello selection"
    selection = MagicMock()
    selection.getCount.return_value = 1
    selection.getByIndex.return_value = text_range
    model.CurrentController.getSelection.return_value = selection

    with patch("plugin.writer.editselection.get_string_without_tracked_deletions", return_value="hello selection"), \
         patch("plugin.writer.editselection._stop_for_tracked_changes", return_value=False), \
         patch("plugin.writer.editselection.prompt_for_edit_instructions", return_value=("shorter", "dialog extra")):
        do_edit_selection(ctx, model, MagicMock())

    assert mock_stream.called
    assert mock_stream.call_args.args[3] == "dialog extra"



