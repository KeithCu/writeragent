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

from plugin.framework.async_drain_guard import acquire_drain_owner, release_drain_owner, get_drain_owner, reset_sentry_state, _drain_idle_callbacks

def test_stream_completion_tasks_defers_next_task():
    client = MagicMock()
    ctx = MagicMock()
    tasks = [StreamCompletionTask(str(i), "sys", 10) for i in range(8)]

    errors = []
    completed_prompts = []

    # snapshot callbacks to ensure we clean up after ourselves
    original_callbacks = list(_drain_idle_callbacks)
    reset_sentry_state()

    try:
        def fake_stream_completion(ctx_arg, client_arg, prompt, sys_prompt, max_tokens, apply_chunk, on_done, on_error):
            if get_drain_owner() is not None:
                on_error(RuntimeError(f"nested drain: {get_drain_owner()}"))
                return
            acquire_drain_owner("stream")

            # Simulate finishing later
            def finish():
                completed_prompts.append(prompt)
                # the issue was: release_drain_owner restores the previous owner if passed one, or None if depth is 0.
                # Actually, release_drain_owner(None) on an acquired lock will make active_owner None when depth hits 0.
                # However, acquire_drain_owner("stream") returns the PREVIOUS owner, which in our test might be None.
                # But if we just call it, the depth increases. Wait, we call it in the fake. Let's trace.
                release_drain_owner(None)
                on_done()

            # Store finish so we can call it after stream_completion returns
            ctx_arg.finish_callbacks.append(finish)

        ctx.finish_callbacks = []

        with patch("plugin.chatbot.selection.stream_completion", side_effect=fake_stream_completion):
            with patch("plugin.chatbot.selection.post_to_main_thread", side_effect=lambda fn: fn()):
                stream_completion_tasks(ctx, client, tasks, lambda t: (MagicMock(), errors.append))

                # We start task 0 right away
                assert len(ctx.finish_callbacks) == 1

                for i in range(8):
                    # pop the finish callback for the current task
                    finish = ctx.finish_callbacks.pop(0)
                    finish()

                    # check that any pending idle callbacks run now
                    from plugin.framework.async_drain_guard import _notify_drain_idle
                    _notify_drain_idle()

                    if i < 7:
                        assert len(ctx.finish_callbacks) == 1
                    else:
                        assert len(ctx.finish_callbacks) == 0

        assert not errors
        assert completed_prompts == [str(i) for i in range(8)]
        # After it's all done, any added idle callbacks should have unregistered
        assert len(_drain_idle_callbacks) == len(original_callbacks)
    finally:
        reset_sentry_state()
        _drain_idle_callbacks.clear()
        _drain_idle_callbacks.extend(original_callbacks)


def test_stream_completion_tasks_waits_for_existing_owner():
    client = MagicMock()
    ctx = MagicMock()
    tasks = [StreamCompletionTask("1", "sys", 10), StreamCompletionTask("2", "sys", 10)]

    errors = []
    completed_prompts = []

    original_callbacks = list(_drain_idle_callbacks)
    reset_sentry_state()

    try:
        def fake_stream_completion(ctx_arg, client_arg, prompt, sys_prompt, max_tokens, apply_chunk, on_done, on_error):
            if get_drain_owner() is not None:
                on_error(RuntimeError(f"nested drain: {get_drain_owner()}"))
                return
            acquire_drain_owner("stream")

            def finish():
                completed_prompts.append(prompt)
                release_drain_owner(None)
                on_done()

            ctx_arg.finish_callbacks.append(finish)

        ctx.finish_callbacks = []

        with patch("plugin.chatbot.selection.stream_completion", side_effect=fake_stream_completion):
            with patch("plugin.chatbot.selection.post_to_main_thread", side_effect=lambda fn: fn()):
                stream_completion_tasks(ctx, client, tasks, lambda t: (MagicMock(), errors.append))

                # task 1 finishes
                finish = ctx.finish_callbacks.pop(0)

                # Simulate another owner holding the pump right as task 1's cleanup finishes
                # but before it notifies idle callbacks / runs the posted task.
                # Actually, finish() releases the pump, so we need to hold it before we
                # call post_to_main_thread(run_next_task), or we can simulate run_next_task
                # executing while the pump is held.

                # Instead of normal flow, we'll manually invoke on_done while the pump is held
                # to simulate another owner sneaking in.

                finish() # finishes task 1, releases "stream"

                acquire_drain_owner("other_owner")
                from plugin.framework.async_drain_guard import _notify_drain_idle
                _notify_drain_idle() # runs schedule_next_when_idle's _once which posts run_next_task which we stubbed to run synchronously.
                # run_next_task sees existing owner, reschedules.

                assert len(ctx.finish_callbacks) == 0 # no new stream started

                # Release the pump
                release_drain_owner(None)

                # the resched was an add_drain_idle_callback. let's fire it.
                _notify_drain_idle()

                assert len(ctx.finish_callbacks) == 1 # task 2 started
                finish = ctx.finish_callbacks.pop(0)
                finish()

                # one more idle pump to drain any post-task 2 callbacks
                _notify_drain_idle()

        assert not errors
        assert completed_prompts == ["1", "2"]
        assert len(_drain_idle_callbacks) == len(original_callbacks)
    finally:
        reset_sentry_state()
        _drain_idle_callbacks.clear()
        _drain_idle_callbacks.extend(original_callbacks)
