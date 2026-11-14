# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Extend/Edit uses the sidebar frame when the hamburger passes one."""

from __future__ import annotations
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from types import SimpleNamespace

from plugin.chatbot.selection import (
    action_edit_selection,
    action_extend_selection,
    do_selection_action_for_document,
)


def _services(active):
    return SimpleNamespace(document=SimpleNamespace(get_active_document=lambda: active))


def test_frame_model_is_used(monkeypatch) -> None:
    frame = object()
    model = object()
    active = object()
    fake_listener = object()
    fake_panel = SimpleNamespace(send_listener=fake_listener)
    fake_session = SimpleNamespace(frame=frame, panel=fake_panel)
    seen: dict[str, object] = {}

    def _from_frame(got):
        assert got is frame
        return model

    def _do(ctx, doc, is_edit, **kwargs):
        seen["doc"] = doc
        seen["is_edit"] = is_edit
        seen["host"] = kwargs.get("host")

    monkeypatch.setattr("plugin.chatbot.selection.get_ctx", lambda: object())
    monkeypatch.setattr("plugin.framework.frame_session.session_for_frame", lambda f: fake_session if f is frame else None)
    monkeypatch.setattr("plugin.chatbot.selection.get_document_from_frame", _from_frame)
    monkeypatch.setattr("plugin.chatbot.selection.do_selection_action_for_document", _do)
    action_extend_selection(_services(active), frame=frame)
    assert seen == {"doc": model, "is_edit": False, "host": fake_listener}


def test_no_frame_uses_active_document(monkeypatch) -> None:
    active = object()
    seen: dict[str, object] = {}

    def _from_frame(got):
        raise AssertionError(got)

    def _do(ctx, doc, is_edit, **kwargs):
        seen["doc"] = doc
        seen["is_edit"] = is_edit
        seen["host"] = kwargs.get("host")

    monkeypatch.setattr("plugin.chatbot.selection.get_ctx", lambda: object())
    monkeypatch.setattr("plugin.chatbot.selection.get_document_from_frame", _from_frame)
    monkeypatch.setattr("plugin.chatbot.selection.do_selection_action_for_document", _do)
    action_edit_selection(_services(active))
    assert seen == {"doc": active, "is_edit": True, "host": None}


def test_registered_handler_passes_frame(monkeypatch) -> None:
    from plugin.chatbot import ChatbotModule
    from plugin.framework.main_shared import _ACTION_HANDLERS, get_action_handler

    frame = object()
    model = object()
    seen: dict[str, object] = {}

    def _do(ctx, doc, is_edit, **kwargs):
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


@pytest.mark.parametrize("doc_label", ["draw", "impress"])
def test_draw_impress_selection_with_sidebar_notify_does_not_msgbox(doc_label: str) -> None:
    from plugin.framework.doc_type import DocumentType

    ctx = MagicMock()
    model = MagicMock()
    doc_type = DocumentType.DRAW if doc_label == "draw" else DocumentType.IMPRESS

    with patch("plugin.chatbot.selection.get_document_type", return_value=doc_type), \
         patch("plugin.draw.editselection.do_draw_extend_edit") as draw_edit:
        do_selection_action_for_document(
            ctx,
            model,
            is_edit=True,
            preset_instruction="proofread",
            sidebar_notify=lambda s: None,
        )

    draw_edit.assert_called_once()


def test_draw_selection_without_sidebar_notify_still_msgbox() -> None:
    from plugin.framework.doc_type import DocumentType

    ctx = MagicMock()
    model = MagicMock()

    with patch("plugin.chatbot.selection.get_document_type", return_value=DocumentType.DRAW), \
         patch("plugin.draw.editselection.do_draw_extend_edit") as draw_edit, \
         patch("plugin.chatbot.selection.msgbox") as msgbox:
        do_selection_action_for_document(ctx, model, is_edit=False)

    draw_edit.assert_called_once()
    msgbox.assert_not_called()


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
    # Start from an empty list: other tests (e.g. Calc multi-cell selection)
    # can leave a stream_completion_tasks one-shot registered under xdist, and
    # the peer-message idle hook is not under test here. Restored in finally.
    _drain_idle_callbacks.clear()
    reset_sentry_state()

    try:
        def fake_stream_completion(
            ctx_arg,
            client_arg,
            prompt,
            sys_prompt,
            max_tokens,
            apply_chunk,
            on_done,
            on_error,
            **kwargs,
        ):
            if get_drain_owner() is not None:
                on_error(RuntimeError(f"nested drain: {get_drain_owner()}"))
                return
            acquire_drain_owner("stream")

            # Simulate finishing later
            def finish():
                completed_prompts.append(prompt)
                # release_drain_owner() decrements depth and clears active_owner when depth hits 0.
                release_drain_owner()
                on_done()

            # Store finish so we can call it after stream_completion returns
            ctx_arg.finish_callbacks.append(finish)

        ctx.finish_callbacks = []

        with patch("plugin.chatbot.selection.stream_completion", side_effect=fake_stream_completion):
            with patch("plugin.framework.queue_executor.main.post", side_effect=lambda fn: fn()):
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
        assert list(_drain_idle_callbacks) == []
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
    # Start from an empty list: other tests (e.g. Calc multi-cell selection)
    # can leave a stream_completion_tasks one-shot registered under xdist, and
    # the peer-message idle hook is not under test here. Restored in finally.
    _drain_idle_callbacks.clear()
    reset_sentry_state()

    try:
        def fake_stream_completion(
            ctx_arg,
            client_arg,
            prompt,
            sys_prompt,
            max_tokens,
            apply_chunk,
            on_done,
            on_error,
            **kwargs,
        ):
            if get_drain_owner() is not None:
                on_error(RuntimeError(f"nested drain: {get_drain_owner()}"))
                return
            acquire_drain_owner("stream")

            def finish():
                completed_prompts.append(prompt)
                release_drain_owner()
                on_done()

            ctx_arg.finish_callbacks.append(finish)

        ctx.finish_callbacks = []

        with patch("plugin.chatbot.selection.stream_completion", side_effect=fake_stream_completion):
            with patch("plugin.framework.queue_executor.main.post", side_effect=lambda fn: fn()):
                stream_completion_tasks(ctx, client, tasks, lambda t: (MagicMock(), errors.append))

                # task 1 finishes
                finish = ctx.finish_callbacks.pop(0)

                # Simulate another owner holding the pump while task 1's cleanup posts run_next_task.
                finish() # finishes task 1, releases "stream"

                acquire_drain_owner("other_owner")
                from plugin.framework.async_drain_guard import _notify_drain_idle
                _notify_drain_idle() # runs schedule_next_when_idle's _once which posts run_next_task which we stubbed to run synchronously.
                # run_next_task sees existing owner, reschedules.

                assert len(ctx.finish_callbacks) == 0 # no new stream started

                # Release the pump
                release_drain_owner()

                # Trigger the rescheduled drain-idle callback.
                _notify_drain_idle()

                assert len(ctx.finish_callbacks) == 1 # task 2 started
                finish = ctx.finish_callbacks.pop(0)
                finish()

                # one more idle pump to drain any post-task 2 callbacks
                _notify_drain_idle()

        assert not errors
        assert completed_prompts == ["1", "2"]
        assert list(_drain_idle_callbacks) == []
    finally:
        reset_sentry_state()
        _drain_idle_callbacks.clear()
        _drain_idle_callbacks.extend(original_callbacks)


def test_prompt_for_edit_instructions_long_error_does_not_raise_contract_error():
    from plugin.chatbot.selection import prompt_for_edit_instructions
    from plugin.framework.deal_shim import DEAL_MAX_MSGID
    from plugin.framework.errors import format_error_message

    long_err = ValueError("x" * (DEAL_MAX_MSGID + 50))

    ctx = MagicMock()
    with patch("plugin.chatbot.selection.input_box", side_effect=long_err), \
         patch("plugin.chatbot.selection.msgbox") as mock_msgbox:
        result = prompt_for_edit_instructions(ctx, "Title")
        assert result is None
        mock_msgbox.assert_called_once_with(ctx, "Title", format_error_message(long_err))


def test_initialize_import_failure_still_registers_selection_actions(monkeypatch) -> None:
    from plugin.chatbot import ChatbotModule
    from plugin.framework.main_shared import get_action_handler
    import plugin.chatbot.__init__ as chatbot_init

    orig_import = chatbot_init.import_module

    def _failing_import(name: str):
        if name.endswith("ppt_master"):
            raise ImportError("boom")
        return orig_import(name)

    monkeypatch.setattr(chatbot_init, "import_module", _failing_import)
    services = SimpleNamespace(tools=SimpleNamespace(auto_discover=MagicMock()))

    ChatbotModule().initialize(services)
    assert get_action_handler("chatbot.extend_selection") is not None
    assert get_action_handler("chatbot.edit_selection") is not None


def test_initialize_registers_all_module_yaml_actions() -> None:
    from pathlib import Path
    import yaml
    from plugin.chatbot import ChatbotModule
    from plugin.framework.main_shared import get_action_handler

    yaml_path = Path(__file__).resolve().parents[2] / "plugin" / "chatbot" / "module.yaml"
    with open(yaml_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    actions = data.get("actions", {})
    assert actions, "module.yaml has no actions"

    services = SimpleNamespace(tools=SimpleNamespace(auto_discover=MagicMock()))
    ChatbotModule().initialize(services)

    for action_key in actions:
        handler = get_action_handler(f"chatbot.{action_key}")
        assert handler is not None, f"Missing handler for chatbot.{action_key}"


def test_bind_and_end_selection_cancel_idle_and_busy_hosts() -> None:
    from plugin.chatbot.panel import SendButtonListener
    from plugin.chatbot.selection import bind_selection_cancel, end_selection_cancel
    from plugin.chatbot.send_state import SendEventKind
    from plugin.framework.send_cancellation import SendCancellation

    # Idle fake host
    class FakeIdleHost:
        def __init__(self) -> None:
            self._send_cancellation = None
            self._stop_requested_fallback = False
            self._panel_teardown = False
            self.sidebar_state = SimpleNamespace(send=SimpleNamespace(is_busy=False, is_recording=False))
            self.dispatched: list[Any] = []
            self.synced = False
            self.flushed_restarts = 0
            self._voice = SimpleNamespace(_flush_sticky_restart=self._flush_sticky_restart)

        def _flush_sticky_restart(self) -> None:
            self.flushed_restarts += 1

        def dispatch(self, event: Any) -> None:
            self.dispatched.append(event)
            if event.kind == SendEventKind.EXTRACTED_SEND:
                self.sidebar_state.send.is_busy = True
            elif event.kind == SendEventKind.SEND_COMPLETED:
                self.sidebar_state.send.is_busy = False

        def sync_has_text_from_query(self) -> None:
            self.synced = True

        def close_selection_run(self) -> None:
            SendButtonListener.close_selection_run(self)

    idle_host = FakeIdleHost()
    model = object()
    arm = bind_selection_cancel(idle_host, model)
    assert arm.armed is True
    assert isinstance(idle_host._send_cancellation, SendCancellation)
    assert arm.scope is idle_host._send_cancellation
    assert len(idle_host.dispatched) == 1
    assert idle_host.dispatched[0].kind == SendEventKind.EXTRACTED_SEND

    end_selection_cancel(arm)
    assert idle_host._send_cancellation is None
    assert idle_host.synced is True
    assert idle_host.flushed_restarts == 1
    assert len(idle_host.dispatched) == 2
    assert idle_host.dispatched[1].kind == SendEventKind.SEND_COMPLETED

    # Busy fake host
    class FakeBusyHost:
        def __init__(self, existing_scope: SendCancellation) -> None:
            self._send_cancellation = existing_scope
            self._stop_requested_fallback = False
            self._panel_teardown = False
            self.sidebar_state = SimpleNamespace(send=SimpleNamespace(is_busy=True, is_recording=False))
            self.dispatched: list[Any] = []
            self.synced = False

        def dispatch(self, event: Any) -> None:
            self.dispatched.append(event)

        def sync_has_text_from_query(self) -> None:
            self.synced = True

        def close_selection_run(self) -> None:
            raise AssertionError("busy host must not run close_selection_run")

    existing_scope = SendCancellation()
    busy_host = FakeBusyHost(existing_scope)
    busy_arm = bind_selection_cancel(busy_host, model)
    assert busy_arm.armed is False
    assert busy_arm.scope is existing_scope
    assert busy_host.dispatched == []

    end_selection_cancel(busy_arm)
    assert busy_host._send_cancellation is existing_scope
    assert busy_host.dispatched == []
    assert busy_host.synced is False


def test_bind_selection_cancel_model_disposing_cancels_scope() -> None:
    from plugin.chatbot.selection import bind_selection_cancel, end_selection_cancel

    class FakeModel:
        def __init__(self) -> None:
            self.listeners: list[Any] = []

        def addDocumentEventListener(self, listener: Any) -> None:
            self.listeners.append(listener)

        def removeDocumentEventListener(self, listener: Any) -> None:
            self.listeners.remove(listener)

    model = FakeModel()
    arm = bind_selection_cancel(None, model)
    assert len(model.listeners) == 1
    assert not arm.scope.is_cancelled()

    model.listeners[0].disposing(None)
    assert arm.scope.is_cancelled() is True

    end_selection_cancel(arm)
    assert len(model.listeners) == 0


def test_stream_completion_tasks_stopped_fn_aborts_subsequent_tasks() -> None:
    from plugin.chatbot.selection import stream_completion_tasks, StreamCompletionTask
    from plugin.framework.send_cancellation import SendCancellation

    scope = SendCancellation()
    tasks = [
        StreamCompletionTask("p1", "s1", 10),
        StreamCompletionTask("p2", "s2", 10),
    ]

    started_tasks: list[StreamCompletionTask] = []
    finished_calls: list[bool] = []

    def prepare_task(task: StreamCompletionTask) -> tuple[Any, Any]:
        started_tasks.append(task)
        return (lambda chunk, is_t=False: None), (lambda exc: None)

    def fake_stream_completion(*args: Any, **kwargs: Any) -> None:
        on_stopped_fn = kwargs.get("on_stopped_fn")
        cancel = kwargs.get("cancel")
        if cancel is not None:
            cancel.cancel()
        if on_stopped_fn is not None:
            on_stopped_fn()

    with patch("plugin.chatbot.selection.stream_completion", side_effect=fake_stream_completion):
        stream_completion_tasks(
            MagicMock(),
            MagicMock(),
            tasks,
            prepare_task,
            on_finished=lambda: finished_calls.append(True),
            cancel=scope,
        )

    assert len(started_tasks) == 1
    assert len(finished_calls) == 1


def test_stream_completion_tasks_stop_between_tasks() -> None:
    from plugin.chatbot.selection import stream_completion_tasks, StreamCompletionTask
    from plugin.framework.send_cancellation import SendCancellation
    from plugin.framework.async_drain_guard import _notify_drain_idle

    scope = SendCancellation()
    tasks = [
        StreamCompletionTask("p1", "s1", 10),
        StreamCompletionTask("p2", "s2", 10),
    ]

    started_tasks: list[StreamCompletionTask] = []
    finished_calls: list[bool] = []

    def prepare_task(task: StreamCompletionTask) -> tuple[Any, Any]:
        started_tasks.append(task)
        return (lambda chunk, is_t=False: None), (lambda exc: None)

    def fake_stream_completion(*args: Any, **kwargs: Any) -> None:
        on_done = args[6]  # schedule_next_when_idle
        on_done()
        # Cancel scope right after task 1 signals done (before task 2 runs)
        scope.cancel()
        _notify_drain_idle()

    with patch("plugin.chatbot.selection.stream_completion", side_effect=fake_stream_completion), \
         patch("plugin.framework.queue_executor.main.post", side_effect=lambda fn: fn()):
        stream_completion_tasks(
            MagicMock(),
            MagicMock(),
            tasks,
            prepare_task,
            on_finished=lambda: finished_calls.append(True),
            cancel=scope,
        )

    assert len(started_tasks) == 1
    assert len(finished_calls) == 1


def test_end_selection_cancel_kicks_queued_peer_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    from plugin.chatbot.panel import SendButtonListener
    from plugin.chatbot.selection import bind_selection_cancel, end_selection_cancel
    from plugin.chatbot.send_state import SendEventKind
    import plugin.doc.peer_message as pm
    from plugin.doc.peer_message import PeerPendingTurn, PeerQueue, enqueue_peer_turn

    monkeypatch.setattr(pm, "peer_queue", PeerQueue())

    class FakeHostWithPeer:
        def __init__(self) -> None:
            self._send_cancellation = None
            self._stop_requested_fallback = False
            self._panel_teardown = False
            self.sidebar_state = SimpleNamespace(send=SimpleNamespace(is_busy=False, is_recording=False))
            self.started: list[tuple[str, bool]] = []
            self.flushed_restarts = 0
            self._voice = SimpleNamespace(_flush_sticky_restart=self._flush_sticky_restart)

        def is_busy(self) -> bool:
            return bool(self.sidebar_state.send.is_busy)

        def _flush_sticky_restart(self) -> None:
            self.flushed_restarts += 1

        def dispatch(self, event: Any) -> None:
            if event.kind == SendEventKind.EXTRACTED_SEND:
                self.sidebar_state.send.is_busy = True
            elif event.kind == SendEventKind.SEND_COMPLETED:
                self.sidebar_state.send.is_busy = False

        def sync_has_text_from_query(self) -> None:
            pass

        def close_selection_run(self) -> None:
            SendButtonListener.close_selection_run(self)

        def start_extracted_peer_send(self, query_text: str, *, already_appended: bool) -> bool:
            self.started.append((query_text, already_appended))
            return True

    host = FakeHostWithPeer()
    model = object()
    arm = bind_selection_cancel(host, model)
    assert arm.armed is True

    turn = PeerPendingTurn("peer query", False)
    enqueue_peer_turn(host, turn)
    assert len(host.started) == 0

    end_selection_cancel(arm)
    assert host.flushed_restarts == 1
    assert len(host.started) == 1
    assert host.started[0] == ("peer query", False)
