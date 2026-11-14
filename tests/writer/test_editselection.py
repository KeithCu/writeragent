import inspect
from typing import Any
from unittest.mock import MagicMock, patch

from plugin.chatbot.selection import stream_completion as _stream_completion_fn
from plugin.writer.editselection import do_extend_selection, do_edit_selection
from plugin.framework.config import save


def _stream_completion_args(mock_stream: MagicMock) -> dict[str, Any]:
    """Bind mock stream_completion call arguments to parameter names."""
    call = mock_stream.call_args
    bound = inspect.signature(_stream_completion_fn).bind(*call.args, **call.kwargs)
    bound.apply_defaults()
    return dict(bound.arguments)

@patch("plugin.writer.editselection.stream_completion")
@patch("plugin.writer.editselection.create_validated_client")
def test_extend_selection_uses_non_empty_range(mock_create_client, mock_stream_completion):
    save({'extend_selection_max_tokens': 100, 'doc.agent_edit_review_mode': 'none', 'additional_instructions': ''})
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

    with patch("plugin.writer.editselection.get_string_without_tracked_deletions", return_value="orig"):
        do_extend_selection(ctx, model)

        # apply_chunk should be passed to stream_completion
        assert mock_stream_completion.called
        prompt = _stream_completion_args(mock_stream_completion)["prompt"]
        assert "orig" in prompt

        apply_chunk_fn = _stream_completion_args(mock_stream_completion)["apply_chunk_fn"]
        apply_chunk_fn("new chunk")

        rng0.setString.assert_not_called()
        rng1.setString.assert_called()


@patch("plugin.writer.editselection.stream_completion")
@patch("plugin.writer.editselection.create_validated_client")
def test_extend_selection_falls_back_to_index0_if_all_empty(mock_create_client, mock_stream_completion):
    save({'extend_selection_max_tokens': 100, 'doc.agent_edit_review_mode': 'none', 'additional_instructions': ''})
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

    with patch("plugin.writer.editselection.get_string_without_tracked_deletions", return_value="fallback_orig"):
        do_extend_selection(ctx, model)

        apply_chunk_fn = _stream_completion_args(mock_stream_completion)["apply_chunk_fn"]
        apply_chunk_fn("new chunk")

        rng0.setString.assert_called()
        rng1.setString.assert_not_called()
        assert mock_stream_completion.called


@patch("plugin.writer.editselection.msgbox")
@patch("plugin.writer.editselection.stream_completion")
@patch("plugin.writer.editselection.create_validated_client")
def test_extend_selection_fails_when_setString_fails(mock_create_client, mock_stream_completion, mock_msgbox):
    save({'extend_selection_max_tokens': 100, 'doc.agent_edit_review_mode': 'none', 'additional_instructions': ''})
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

    with patch("plugin.writer.editselection.get_string_without_tracked_deletions", return_value="some text"):

        do_extend_selection(ctx, model)

        apply_chunk_fn = _stream_completion_args(mock_stream_completion)["apply_chunk_fn"]
        on_done_fn = _stream_completion_args(mock_stream_completion)["on_done_fn"]

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

    save({'extend_selection_max_tokens': 100, 'doc.agent_edit_review_mode': 'none', 'additional_instructions': ''})
    mock_create_client.return_value = MagicMock()
    ctx = MagicMock()
    model = MagicMock()
    selection = MagicMock()
    selection.getByIndex.return_value = MagicMock()
    model.CurrentController.getSelection.return_value = selection

    long_err = ValueError("x" * (DEAL_MAX_MSGID + 50))
    with patch("plugin.writer.editselection.get_string_without_tracked_deletions", return_value="some text"):
        do_extend_selection(ctx, model)
        on_error_fn = _stream_completion_args(mock_stream_completion)["on_error_fn"]
        on_error_fn(long_err)
        mock_msgbox.assert_called_once()
        assert mock_msgbox.call_args.args[2] == format_error_message(long_err)


@patch("plugin.writer.editselection.msgbox")
@patch("plugin.writer.editselection.stream_completion")
@patch("plugin.writer.editselection.create_validated_client")
def test_edit_selection_on_error_long_error_does_not_raise_contract_error(mock_create_client, mock_stream_completion, mock_msgbox):
    from plugin.framework.deal_shim import DEAL_MAX_MSGID
    from plugin.framework.errors import format_error_message

    save({'edit_selection_max_new_tokens': 100, 'doc.agent_edit_review_mode': 'none', 'additional_instructions': ''})
    mock_create_client.return_value = MagicMock()
    ctx = MagicMock()
    model = MagicMock()
    selection = MagicMock()
    selection.getByIndex.return_value = MagicMock()
    model.CurrentController.getSelection.return_value = selection

    long_err = ValueError("x" * (DEAL_MAX_MSGID + 50))
    with patch("plugin.writer.editselection.get_string_without_tracked_deletions", return_value="some text"), \
         patch("plugin.writer.editselection.prompt_for_edit_instructions", return_value=("make it better", "")):
        do_edit_selection(ctx, model)
        on_error_fn = _stream_completion_args(mock_stream_completion)["on_error_fn"]
        on_error_fn(long_err)
        mock_msgbox.assert_called_once()
        assert mock_msgbox.call_args.args[2] == format_error_message(long_err)


@patch("plugin.writer.editselection.msgbox")
@patch("plugin.writer.editselection.get_string_without_tracked_deletions")
def test_extend_selection_none_selection_shows_message_and_returns(mock_get_str, mock_msgbox):
    ctx = MagicMock()
    model = MagicMock()
    model.CurrentController.getSelection.return_value = None

    do_extend_selection(ctx, model)

    mock_msgbox.assert_called_once()
    mock_get_str.assert_not_called()


@patch("plugin.writer.editselection.msgbox")
@patch("plugin.writer.editselection.get_string_without_tracked_deletions")
def test_edit_selection_none_selection_shows_message_and_returns(mock_get_str, mock_msgbox):
    ctx = MagicMock()
    model = MagicMock()
    model.CurrentController.getSelection.return_value = None

    do_edit_selection(ctx, model)

    mock_msgbox.assert_called_once()
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

    do_edit_selection(ctx, model)

    mock_msgbox.assert_called_once()
    mock_prompt.assert_not_called()


@patch("plugin.writer.editselection.stream_completion")
@patch("plugin.writer.editselection.create_validated_client")
def test_edit_selection_preset_skips_prompt_and_validates_client_first(
    mock_create_client, mock_stream,
):
    save({"edit_selection_max_new_tokens": 100, "doc.agent_edit_review_mode": "none", "additional_instructions": ""})
    mock_create_client.return_value = MagicMock()
    ctx = MagicMock()
    model = MagicMock()
    text_range = MagicMock()
    selection = MagicMock()
    selection.getCount.return_value = 1
    selection.getByIndex.return_value = text_range
    model.CurrentController.getSelection.return_value = selection

    with patch("plugin.writer.editselection.get_string_without_tracked_deletions", return_value="orig"), \
         patch("plugin.writer.editselection.prompt_for_edit_instructions") as mock_prompt:
        do_edit_selection(ctx, model, preset_instruction="Fix typos")

    mock_prompt.assert_not_called()
    assert mock_create_client.called
    assert mock_stream.called


@patch("plugin.writer.editselection.stream_completion")
@patch("plugin.writer.editselection.create_validated_client")
@patch("plugin.writer.editselection.msgbox")
def test_edit_selection_preset_whole_body_without_select_first_msgbox(
    mock_msgbox, mock_create_client, mock_stream,
):
    save({"edit_selection_max_new_tokens": 100, "doc.agent_edit_review_mode": "none", "additional_instructions": ""})
    mock_create_client.return_value = MagicMock()
    ctx = MagicMock()
    model = MagicMock()
    body = MagicMock()
    cursor = MagicMock()
    body.createTextCursor.return_value = cursor
    model.getText.return_value = body
    model.CurrentController.getSelection.return_value = None

    with patch("plugin.writer.editselection.selected_text_range", return_value=None), \
         patch("plugin.writer.editselection.get_string_without_tracked_deletions", return_value="Para one\nPara two"), \
         patch("plugin.writer.editselection.normalize_linebreaks", return_value="Para one\nPara two"), \
         patch("plugin.writer.editselection.refuse_tracked_insert_or_delete"), \
         patch("plugin.writer.editselection.review_recording_enabled", return_value=False), \
         patch("plugin.writer.editselection.get_config", return_value=100), \
         patch("plugin.writer.editselection.WriterStreamedRewriteSession") as session_cls:
        do_edit_selection(
            ctx,
            model,
            preset_instruction="Proofread",
            allow_whole_document=True,
        )

    mock_msgbox.assert_not_called()
    assert mock_stream.called
    prompt = _stream_completion_args(mock_stream)["prompt"]
    assert "Para one\nPara two" in prompt
    assert session_cls.call_args.kwargs.get("preserve_paragraph_breaks") is True


@patch("plugin.writer.editselection.create_validated_client")
def test_edit_selection_preset_length_empty_notifies_sidebar(mock_create_client):
    import json

    from plugin.framework.client.llm_client import LlmClient
    from plugin.tests.testing_utils import MockContext, create_mock_http_response

    save({"chatbot.edit_selection_max_new_tokens": 100, "doc.agent_edit_review_mode": "none", "additional_instructions": ""})
    ctx = MockContext()
    client = LlmClient({"endpoint": "https://api.openai.com", "api_key": "sk-test", "model": "gpt-4"}, ctx)
    mock_create_client.return_value = client
    model = MagicMock()
    text_range = MagicMock()
    selection = MagicMock()
    selection.getCount.return_value = 1
    selection.getByIndex.return_value = text_range
    model.CurrentController.getSelection.return_value = selection
    sidebar_lines: list[str] = []
    session_inst = MagicMock()
    session_inst.generated_text = ""
    session_inst.finish.return_value = None
    session_inst.doc = model

    sse_lines = [
        f'data: {json.dumps({"choices": [{"finish_reason": "length", "delta": {}}]})}'.encode(),
        b"data: [DONE]\n\n",
    ]
    resp = create_mock_http_response(sse_lines=sse_lines)

    def sync_run_stream(
        ctx_arg,
        client_arg,
        prompt,
        system_prompt,
        max_tokens,
        apply_chunk_fn,
        on_done_fn,
        on_error_fn,
        **kwargs,
    ):
        completion_out = kwargs.get("completion_out")
        finish = client_arg.stream_completion(
            prompt,
            system_prompt,
            max_tokens,
            append_callback=lambda t: apply_chunk_fn(t, False),
            cancel=kwargs.get("cancel"),
        )
        if completion_out is not None:
            completion_out["finish_reason"] = finish
        on_done_fn()

    with (
        patch("plugin.writer.editselection.get_string_without_tracked_deletions", return_value="orig"),
        patch("plugin.writer.editselection.WriterStreamedRewriteSession", return_value=session_inst),
        patch("plugin.chatbot.selection.run_stream_completion_async", side_effect=sync_run_stream),
        patch("http.client.HTTPSConnection") as mock_https,
    ):
        mock_conn = MagicMock()
        mock_https.return_value = mock_conn
        mock_conn.getresponse.return_value = resp
        do_edit_selection(
            ctx,
            model,
            preset_instruction="shorten",
            sidebar_notify=sidebar_lines.append,
        )

    assert sidebar_lines


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

    with patch("plugin.writer.editselection.get_string_without_tracked_deletions", return_value="orig"):
        do_edit_selection(ctx, model)

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

    do_extend_selection(ctx, model)

    mock_msgbox.assert_called_once()
    mock_stream.assert_not_called()


@patch("plugin.writer.editselection.stream_completion")
@patch("plugin.writer.editselection.create_validated_client")
@patch("plugin.writer.editselection.get_current_endpoint", return_value="ep")
@patch("plugin.writer.editselection.get_text_model", return_value="m")
@patch("plugin.writer.editselection.update_lru_history")
def test_both_commands_write_model_lru_once(
    mock_update_lru, mock_get_model, mock_get_ep, mock_create_client, mock_stream
):
    # Seed both token caps: LRU test exercises Extend then Edit, and without these
    # get_config_int raises once create_validated_client is mocked through.
    save(
        {
            "extend_selection_max_tokens": 100,
            "edit_selection_max_new_tokens": 100,
            "doc.agent_edit_review_mode": "none",
            "additional_instructions": "",
        }
    )
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
         patch("plugin.writer.editselection.prompt_for_edit_instructions", return_value=("rewrite", "")):

        do_extend_selection(ctx, model)
        assert mock_update_lru.call_args_list == [(("m", "model_lru", "ep"),)]

        mock_update_lru.reset_mock()

        do_edit_selection(ctx, model)
        assert mock_update_lru.call_args_list == [(("m", "model_lru", "ep"),)]


@patch("plugin.writer.editselection.stream_completion")
@patch("plugin.writer.editselection.create_validated_client")
def test_extend_selection_uses_extend_system_prompt(mock_create_client, mock_stream):
    save({
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

    with patch("plugin.writer.editselection.get_string_without_tracked_deletions", return_value="hello selection"):
        do_extend_selection(ctx, model)

    assert mock_stream.called
    assert _stream_completion_args(mock_stream)["prompt"] == "hello selection"
    assert _stream_completion_args(mock_stream)["system_prompt"] == "extend sys"


@patch("plugin.writer.editselection.stream_completion")
@patch("plugin.writer.editselection.create_validated_client")
def test_edit_selection_uses_edit_system_prompt(mock_create_client, mock_stream):
    save({
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
         patch("plugin.writer.editselection.prompt_for_edit_instructions", return_value=("shorter", "")):
        do_edit_selection(ctx, model)

    assert mock_stream.called
    assert _stream_completion_args(mock_stream)["system_prompt"] == "edit sys"


@patch("plugin.writer.editselection.stream_completion")
@patch("plugin.writer.editselection.create_validated_client")
def test_edit_selection_dialog_extras_win_over_config(mock_create_client, mock_stream):
    save({
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
         patch("plugin.writer.editselection.prompt_for_edit_instructions", return_value=("shorter", "dialog extra")):
        do_edit_selection(ctx, model)

    assert mock_stream.called
    assert _stream_completion_args(mock_stream)["system_prompt"] == "dialog extra"


def test_extend_selection_stop_scope_finishes_and_appends():
    import threading
    import time
    from types import SimpleNamespace
    from plugin.framework.send_cancellation import SendCancellation

    save({
        "extend_selection_max_tokens": 100,
        "doc.agent_edit_review_mode": "none",
        "additional_instructions": "",
    })

    scope = SendCancellation()
    chunk_queued = threading.Event()

    class FakeClient:
        def __init__(self) -> None:
            self.stopped = False

        def stop(self) -> None:
            self.stopped = True

        def stream_completion(self, prompt, system_prompt, max_tokens, **kwargs):
            append_cb = kwargs["append_callback"]
            append_cb("CHUNK")
            chunk_queued.set()
            deadline = time.monotonic() + 5.0
            cancel = kwargs.get("cancel")
            while cancel is not None and not cancel.is_cancelled():
                if time.monotonic() > deadline:
                    raise TimeoutError("Cancel deadline exceeded")
                time.sleep(0.01)
            return "stop"

    fake_client = FakeClient()

    def side_canceller():
        if chunk_queued.wait(timeout=5.0):
            scope.cancel()

    canceller_thread = threading.Thread(target=side_canceller)
    canceller_thread.start()

    ctx = MagicMock()
    model = MagicMock()
    text_range = MagicMock()
    text_range.getString.return_value = "orig"
    selection = MagicMock()
    selection.getCount.return_value = 1
    selection.getByIndex.return_value = text_range
    model.CurrentController.getSelection.return_value = selection

    call_order: list[str] = []
    session = MagicMock()
    session.doc = model
    session.generated_text = "CHUNK"

    def _append(text: str) -> None:
        call_order.append("append_chunk")

    def _finish() -> str:
        call_order.append("finish")
        return ""

    def _abort() -> None:
        call_order.append("abort_and_restore")

    session.append_chunk.side_effect = _append
    session.finish.side_effect = _finish
    session.abort_and_restore.side_effect = _abort

    fake_host = SimpleNamespace(
        _send_cancellation=scope,
        sidebar_state=SimpleNamespace(send=SimpleNamespace(is_busy=True, is_recording=False)),
    )

    with patch("plugin.writer.editselection.create_validated_client", return_value=fake_client), \
         patch("plugin.writer.editselection.get_string_without_tracked_deletions", return_value="orig"), \
         patch("plugin.writer.editselection.WriterStreamedAppendSession", return_value=session):
        do_extend_selection(ctx, model, host=fake_host)

    canceller_thread.join(timeout=5.0)

    assert fake_client.stopped is True
    assert call_order == ["append_chunk", "finish"]
    assert not session.abort_and_restore.called


def test_do_edit_selection_skips_dialog_with_preset():
    from plugin.writer import editselection as es

    ctx = object()
    model = MagicMock()
    model.getCurrentController.return_value = MagicMock()
    with patch.object(es, "_selection_target", return_value=(MagicMock(), "text")):
        with patch.object(es, "create_validated_client", return_value=MagicMock()):
            with patch.object(es, "prompt_for_edit_instructions") as prompt:
                with patch.object(es, "_stream_into_session"):
                    with patch.object(es, "update_lru_history"):
                        with patch("plugin.writer.editselection.review_recording_enabled", return_value=False):
                            with patch("plugin.writer.editselection.get_config", return_value=100):
                                es.do_edit_selection(ctx, model, preset_instruction="Fix typos")
    prompt.assert_not_called()
