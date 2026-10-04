# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Extend/Edit uses the sidebar frame when the hamburger passes one."""

from __future__ import annotations
from unittest.mock import MagicMock, patch

from types import SimpleNamespace

from plugin.chatbot.selection import action_edit_selection, action_extend_selection


def _services(active):
    return SimpleNamespace(document=SimpleNamespace(get_active_document=lambda: active))


def test_frame_model_is_used(monkeypatch) -> None:
    frame = object()
    model = object()
    active = object()
    seen: dict[str, object] = {}

    def _from_frame(got):
        assert got is frame
        return model

    def _do(ctx, doc, input_box_fn, is_edit):
        seen["doc"] = doc
        seen["is_edit"] = is_edit

    monkeypatch.setattr("plugin.chatbot.selection.get_ctx", lambda: object())
    monkeypatch.setattr("plugin.chatbot.selection.get_document_from_frame", _from_frame)
    monkeypatch.setattr("plugin.chatbot.selection.do_selection_action_for_document", _do)
    action_extend_selection(_services(active), frame=frame)
    assert seen == {"doc": model, "is_edit": False}


def test_no_frame_uses_active_document(monkeypatch) -> None:
    active = object()
    seen: dict[str, object] = {}

    def _from_frame(got):
        raise AssertionError(got)

    def _do(ctx, doc, input_box_fn, is_edit):
        seen["doc"] = doc
        seen["is_edit"] = is_edit

    monkeypatch.setattr("plugin.chatbot.selection.get_ctx", lambda: object())
    monkeypatch.setattr("plugin.chatbot.selection.get_document_from_frame", _from_frame)
    monkeypatch.setattr("plugin.chatbot.selection.do_selection_action_for_document", _do)
    action_edit_selection(_services(active))
    assert seen == {"doc": active, "is_edit": True}


def test_registered_handler_passes_frame(monkeypatch) -> None:
    from plugin.chatbot import ChatbotModule
    from plugin.framework.main_shared import _ACTION_HANDLERS, get_action_handler

    frame = object()
    model = object()
    seen: dict[str, object] = {}

    def _do(ctx, doc, input_box_fn, is_edit):
        seen["doc"] = doc
        seen["edit"] = is_edit

    monkeypatch.setattr("plugin.chatbot.selection.get_ctx", lambda: object())
    monkeypatch.setattr("plugin.chatbot.selection.get_document_from_frame", lambda got: model if got is frame else None)
    monkeypatch.setattr("plugin.chatbot.selection.do_selection_action_for_document", _do)
    saved = dict(_ACTION_HANDLERS)
    try:
        ChatbotModule()._register_selection_actions(_services(object()))
        extend = get_action_handler("chatbot.extend_selection")
        edit = get_action_handler("chatbot.edit_selection")
        assert extend is not None and edit is not None
        extend(frame)
        assert seen == {"doc": model, "edit": False}
        edit()
        assert seen["doc"] is not model
        assert seen["edit"] is True
    finally:
        _ACTION_HANDLERS.clear()
        _ACTION_HANDLERS.update(saved)

from plugin.chatbot.selection import stream_completion_tasks, StreamCompletionTask

def test_stream_completion_tasks_defers_next_task():
    client = MagicMock()
    ctx = MagicMock()
    tasks = [
        StreamCompletionTask("1", "sys", 10),
        StreamCompletionTask("2", "sys", 10),
    ]

    on_dones = []
    stream_calls = []

    def fake_stream_completion(ctx_arg, client_arg, prompt, sys_prompt, max_tokens, apply_chunk, on_done, on_error):
        stream_calls.append(prompt)
        on_dones.append(on_done)

    # Note: we test that add_drain_idle_callback is actually called with the next execution
    # by verifying that fake_add_drain gets the callback when we trigger on_done

    with patch("plugin.chatbot.selection.stream_completion", side_effect=fake_stream_completion):
        with patch("plugin.chatbot.selection.add_drain_idle_callback") as mock_add_drain:
            stream_completion_tasks(ctx, client, tasks, lambda t: (MagicMock(), MagicMock()))

            assert len(stream_calls) == 1
            assert stream_calls[0] == "1"

            # Call on_done for task 1
            on_dones[0]()

            # Since the lambda inside on_done calls add_drain_idle_callback(run_next_task),
            # we can check that it was called!
            assert mock_add_drain.called

            # Extract the arg and run it
            callback = mock_add_drain.call_args[0][0]
            callback()

            assert len(stream_calls) == 2
            assert stream_calls[1] == "2"
