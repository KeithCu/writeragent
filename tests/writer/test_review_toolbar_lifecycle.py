# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless tests for the review toolbar's per-document modify-listener lifecycle.

The listener must be torn down when the document is disposed even if the OnUnload document event
never reaches us (crash / force-close / a failed event-listener install) -- otherwise the listener
and its registry entry leak, and a later document that reuses the RuntimeUID would be skipped.
"""
from unittest.mock import MagicMock, patch

import plugin.writer.review_toolbar as rt
from plugin.writer import review_view_attachment as rva


def _doc_key(uid: str) -> str:
    return "uid:%s" % uid


def _clear_registry() -> None:
    rva.reset_all()


class FakeModel:
    def __init__(self, uid):
        self._uid = uid
        self.listeners = []

    def getRuntimeUID(self):
        return self._uid

    def addModifyListener(self, listener):
        self.listeners.append(listener)

    def removeModifyListener(self, listener):
        self.listeners.remove(listener)


def test_disposing_drops_registry_entry():
    _clear_registry()
    model = FakeModel("uid-1")
    rt._register_modify_listener(model)
    key = _doc_key("uid-1")
    assert key in rva.modify_listener_map()
    listener = rva.modify_listener_map()[key]
    assert listener in model.listeners

    listener.disposing(MagicMock())
    assert key not in rva.modify_listener_map()


def test_unregister_removes_listener_and_entry():
    _clear_registry()
    model = FakeModel("uid-2")
    rt._register_modify_listener(model)
    rt._unregister_modify_listener(model)
    assert _doc_key("uid-2") not in rva.modify_listener_map()
    assert model.listeners == []


def test_disposing_is_idempotent_and_safe():
    _clear_registry()
    listener = rt._ReviewModifyListener(_doc_key("uid-3"))
    listener.disposing(MagicMock())
    assert _doc_key("uid-3") not in rva.modify_listener_map()


def test_disposing_does_not_evict_a_recycled_uid_entry():
    _clear_registry()
    key = _doc_key("uid-R")
    old = rt._ReviewModifyListener(key)
    rva.seed_modify_listener(key, old)
    new = rt._ReviewModifyListener(key)
    rva.seed_modify_listener(key, new)
    old.disposing(MagicMock())
    assert rva.modify_listener_map()[key] is new


def test_register_dedups_by_uid():
    _clear_registry()
    model = FakeModel("uid-4")
    rt._register_modify_listener(model)
    key = _doc_key("uid-4")
    first = rva.modify_listener_map()[key]
    rt._register_modify_listener(model)
    assert rva.modify_listener_map()[key] is first
    assert len(model.listeners) == 1


def test_toolbar_docks_once_per_document():
    _clear_registry()
    model = MagicMock()
    model.getRuntimeUID.return_value = "uid-D"
    key = _doc_key("uid-D")
    rva._get_or_create(model)
    with patch("plugin.writer.review_toolbar._layout_manager", return_value=MagicMock()), \
         patch("plugin.writer.review_toolbar._dock_top") as dock, \
         patch("plugin.writer.review_scan.agent_change_presence", side_effect=[(1, True), (0, True), (1, True)]):
        rt.refresh_review_toolbar(model)
        rt.refresh_review_toolbar(model)
        rt.refresh_review_toolbar(model)
    assert dock.call_count == 1
    assert rva._attachments[key].toolbar_docked is True


def test_disposing_clears_docked_state():
    _clear_registry()
    key = _doc_key("uid-E")
    listener = rt._ReviewModifyListener(key)
    rva.seed_modify_listener(key, listener)
    rva._attachments[key].toolbar_docked = True
    listener.disposing(MagicMock())
    assert key not in rva.modify_listener_map()
    assert key not in rva._attachments


# --- modified() must never recount inside the modify notification ------------
# Regression: typing a comment while a review was open fired modified() from inside
# the comment editor's EditEngine::InsertText. The recount's XTextCursor.getString()
# forced a synchronous repaint that re-entered the half-updated comment box and
# crashed; LibreOffice's emergency-save dialog then hung the app (11 .hang reports
# on one machine in one night, all in sw::sidebarwindows::SidebarTextControl).

def _visible_toolbar_patch(visible=True):
    lm = MagicMock()
    lm.isElementVisible.return_value = visible
    return patch.object(rt, "_layout_manager", return_value=lm)


def _event(model):
    ev = MagicMock()
    ev.Source = model
    return ev


def _reset_pending():
    with rt._pending_lock:
        rt._pending_refresh.clear()


def _reset():
    _reset_pending()
    _clear_registry()


def _capture_posts():
    posted = []
    return posted, patch("plugin.framework.queue_executor.post_to_main_thread",
                         side_effect=lambda fn, *a, **k: posted.append((fn, a)))


def test_modified_does_not_recount_synchronously():
    _reset_pending()
    listener = rt._ReviewModifyListener(_doc_key("uid-sync"))
    posted, post_patch = _capture_posts()
    with _visible_toolbar_patch(True), post_patch, patch.object(rt, "refresh_review_toolbar") as refresh:
        listener.modified(_event(FakeModel("uid-sync")))
        refresh.assert_not_called()
    assert [fn for fn, _ in posted] == [rt._run_deferred_refresh], "the recount must be queued, not run"
    _reset_pending()


def test_keystroke_burst_collapses_into_one_queued_recount():
    _reset_pending()
    listener = rt._ReviewModifyListener(_doc_key("uid-burst"))
    model = FakeModel("uid-burst")
    posted, post_patch = _capture_posts()
    with _visible_toolbar_patch(True), post_patch, patch.object(rt, "refresh_review_toolbar"):
        for _ in range(25):  # a long comment, one modified() per keystroke
            listener.modified(_event(model))
    assert len(posted) == 1
    _reset_pending()


def test_normal_typing_without_a_review_queues_nothing():
    _reset_pending()
    listener = rt._ReviewModifyListener(_doc_key("uid-idle"))
    posted, post_patch = _capture_posts()
    with _visible_toolbar_patch(False), post_patch, patch.object(rt, "refresh_review_toolbar") as refresh:
        listener.modified(_event(FakeModel("uid-idle")))
    refresh.assert_not_called()
    assert posted == []


def test_queued_recount_runs_once_then_allows_the_next():
    _reset_pending()
    model = FakeModel("uid-run")
    posted, post_patch = _capture_posts()
    with _visible_toolbar_patch(True), post_patch, patch.object(rt, "refresh_review_toolbar") as refresh:
        rt._schedule_refresh(model, "uid-run")
        fn, args = posted[0]
        fn(*args)  # the main loop gets to it
        refresh.assert_called_once()
        rt._schedule_refresh(model, "uid-run")  # a later keystroke queues again
    assert len(posted) == 2
    _reset_pending()


def test_deferred_recount_rechecks_visibility():
    _reset_pending()
    model = FakeModel("uid-v")
    with rt._pending_lock:
        rt._pending_refresh.add("uid-v")
    with _visible_toolbar_patch(False), patch.object(rt, "refresh_review_toolbar") as refresh:
        rt._run_deferred_refresh(model, "uid-v")
    refresh.assert_not_called()


def test_closing_the_document_cancels_a_queued_recount():
    _reset_pending()
    _clear_registry()
    model = FakeModel("uid-close")
    rt._register_modify_listener(model)
    key = _doc_key("uid-close")
    posted, post_patch = _capture_posts()
    with _visible_toolbar_patch(True), post_patch, patch.object(rt, "refresh_review_toolbar") as refresh:
        rva.modify_listener_map()[key].modified(_event(model))
        rt._unregister_modify_listener(model)
        fn, args = posted[0]
        fn(*args)  # the queued run arrives after the close
        refresh.assert_not_called()
    _reset_pending()


def test_a_failed_queue_does_not_block_later_recounts():
    _reset_pending()
    model = FakeModel("uid-fail")
    with patch("plugin.framework.queue_executor.post_to_main_thread", side_effect=RuntimeError("no AsyncCallback")):
        rt._schedule_refresh(model, "uid-fail")
    assert "uid-fail" not in rt._pending_refresh, "a failed post must not leave the doc marked as queued"


def test_non_writer_unload_does_not_ask_disposed_controller():
    broadcaster = MagicMock()
    ctx = MagicMock()
    ctx.getServiceManager.return_value.createInstanceWithContext.return_value = broadcaster
    rva.reset_all()
    with patch("plugin.framework.uno_context.get_desktop", return_value=None):
        rt.install_review_toolbar(ctx)
        listener = broadcaster.addDocumentEventListener.call_args.args[0]
    impress = MagicMock()
    impress.supportsService.return_value = False
    impress.getCurrentController.side_effect = RuntimeError("DrawController object has already been disposed")
    event = MagicMock(EventName="OnUnload", ViewController=None, Source=impress)
    with patch.object(rva, "release_document") as unreg:
        listener.on_document_event(event)
    impress.getCurrentController.assert_not_called()
    unreg.assert_not_called()


def test_dock_failure_is_retried_on_the_next_show():
    _reset()
    model = FakeModel("uid-dock")
    key = _doc_key("uid-dock")
    lm = MagicMock()
    with patch.object(rt, "_layout_manager", return_value=lm), \
         patch.object(rt, "_dock_top", side_effect=[False, True]) as dock, \
         patch("plugin.writer.review_scan.agent_change_presence",
               side_effect=[(1, True), (0, True), (1, True)]):
        rt.refresh_review_toolbar(model)
        assert not rva._attachments.get(key, rva.ReviewViewAttachment(key, model)).toolbar_docked
        rt.refresh_review_toolbar(model)
        rt.refresh_review_toolbar(model)
    assert dock.call_count == 2
    assert rva._attachments[key].toolbar_docked is True
    _reset()


def test_dock_top_returns_boolean():
    lm = MagicMock()
    assert rt._dock_top(lm) is True
    lm.dockWindow.side_effect = RuntimeError("dock error")
    assert rt._dock_top(lm) is False


def test_refresh_review_toolbar_show_hide_unreliable():
    lm = MagicMock()
    model = FakeModel("uid-sh")
    with patch.object(rt, "_layout_manager", return_value=lm), \
         patch.object(rt, "_dock_top", return_value=True):
        # 1 pending change -> show
        with patch("plugin.writer.review_scan.agent_change_presence", return_value=(1, True)):
            rt.refresh_review_toolbar(model)
            lm.showElement.assert_called_once_with(rt.TOOLBAR_RESOURCE_URL)
            lm.hideElement.assert_not_called()

        lm.reset_mock()
        # 0 pending changes -> hide
        with patch("plugin.writer.review_scan.agent_change_presence", return_value=(0, True)):
            rt.refresh_review_toolbar(model)
            lm.hideElement.assert_called_once_with(rt.TOOLBAR_RESOURCE_URL)
            lm.showElement.assert_not_called()

        lm.reset_mock()
        # unreliable scan -> no-op
        with patch("plugin.writer.review_scan.agent_change_presence", return_value=(0, False)):
            rt.refresh_review_toolbar(model)
            lm.showElement.assert_not_called()
            lm.hideElement.assert_not_called()


def test_install_review_toolbar_warns_on_sweep_failure():
    ctx = MagicMock()
    ctx.getServiceManager.return_value.createInstanceWithContext.return_value = MagicMock()
    rva.reset_all()
    with patch("plugin.framework.uno_context.get_desktop", side_effect=RuntimeError("sweep error")), \
         patch.object(rva.log, "warning") as warn:
        rt.install_review_toolbar(ctx)
        warn.assert_any_call("review_view_attachment: initial sweep failed", exc_info=True)


def test_install_review_toolbar_warns_on_listener_install_failure():
    ctx = MagicMock()
    ctx.getServiceManager.return_value.createInstanceWithContext.side_effect = RuntimeError("service error")
    rva.reset_all()
    with patch("plugin.framework.uno_context.get_desktop", return_value=None), \
         patch("plugin.framework.uno_listeners.log.warning") as warn:
        rt.install_review_toolbar(ctx)
        warn.assert_any_call("%s: doc-event listener install failed", "review_view_attachment", exc_info=True)

