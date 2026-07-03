from unittest.mock import MagicMock, patch


def test_prompt_for_edit_instructions_persists_extra_prompt():
    from plugin.chatbot.selection import prompt_for_edit_instructions

    input_box = MagicMock(return_value=("rewrite", "house style"))

    with patch("plugin.chatbot.selection.set_config") as set_config, \
         patch("plugin.chatbot.selection.update_lru_history") as update_lru_history:
        result = prompt_for_edit_instructions(MagicMock(), input_box, "Title")

    assert result == ("rewrite", "house style")
    set_config.assert_called_once_with("additional_instructions", "house style")
    update_lru_history.assert_called_once_with("house style", "prompt_lru", "")


def test_stream_completion_routes_startup_error_to_error_callback():
    from plugin.chatbot.selection import stream_completion

    error = RuntimeError("boom")
    on_error = MagicMock()

    with patch("plugin.chatbot.selection.run_stream_completion_async", side_effect=error):
        stream_completion(MagicMock(), MagicMock(), "prompt", "system", 10, MagicMock(), MagicMock(), on_error)

    on_error.assert_called_once_with(error)


def test_stream_completion_tasks_runs_cells_sequentially():
    from plugin.chatbot.selection import StreamCompletionTask, stream_completion_tasks

    calls = []
    tasks = [
        StreamCompletionTask("first", "sys", 10, "a"),
        StreamCompletionTask("second", "sys", 20, "b"),
    ]

    def prepare(task):
        calls.append(("prepare", task.payload))
        return MagicMock(), MagicMock()

    def fake_stream_completion(ctx, client, prompt, system_prompt, max_tokens, apply_chunk_fn, on_done_fn, on_error_fn):
        calls.append(("stream", prompt, max_tokens))
        on_done_fn()

    with patch("plugin.chatbot.selection.stream_completion", side_effect=fake_stream_completion):
        stream_completion_tasks(MagicMock(), MagicMock(), tasks, prepare)

    assert calls == [
        ("prepare", "a"),
        ("stream", "first", 10),
        ("prepare", "b"),
        ("stream", "second", 20),
    ]
