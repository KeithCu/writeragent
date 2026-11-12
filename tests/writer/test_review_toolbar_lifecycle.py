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

import plugin.writer.review_view_attachment as rva


def _doc_key(uid: str) -> str:
    return "uid:%s" % uid


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


def test_disposing_drops_registry_entry(review_registry):
    model = FakeModel("uid-1")
    rva.ensure_modify_listener(model)
    key = _doc_key("uid-1")
    att = review_registry.get(key)
    assert att is not None and att.modify_listener is not None
    listener = att.modify_listener
    assert listener in model.listeners

    listener.disposing(MagicMock())
    assert key not in review_registry


def test_unregister_removes_listener_and_entry(review_registry):
    model = FakeModel("uid-2")
    rva.ensure_modify_listener(model)
    rva.release_document(model)
    assert _doc_key("uid-2") not in review_registry
    assert model.listeners == []


def test_disposing_is_idempotent_and_safe(review_registry):
    listener = rva.ReviewModifyListener(_doc_key("uid-3"))
    listener.disposing(MagicMock())
    assert _doc_key("uid-3") not in review_registry


def test_disposing_does_not_evict_a_recycled_uid_entry(review_registry):
    key = _doc_key("uid-R")
    old = rva.ReviewModifyListener(key)
    new = rva.ReviewModifyListener(key)
    review_registry[key] = rva.ReviewViewAttachment(key=key, model=object(), modify_listener=new)
    old.disposing(MagicMock())
    assert review_registry[key].modify_listener is new


def test_register_dedups_by_uid(review_registry):
    model = FakeModel("uid-4")
    rva.ensure_modify_listener(model)
    key = _doc_key("uid-4")
    first = review_registry[key].modify_listener
    rva.ensure_modify_listener(model)
    assert review_registry[key].modify_listener is first
    assert len(model.listeners) == 1


def test_toolbar_docks_once_per_document(review_registry):
    model = MagicMock()
    model.getRuntimeUID.return_value = "uid-D"
    key = _doc_key("uid-D")
    rva._get_or_create(model)
    with patch("plugin.writer.review_view_attachment.layout_manager", return_value=MagicMock()), \
         patch("plugin.writer.review_view_attachment._dock_top") as dock, \
         patch("plugin.writer.review_scan.agent_change_presence", side_effect=[(1, True), (0, True), (1, True)]):
        rva.refresh_review_toolbar(model)
        rva.refresh_review_toolbar(model)
        rva.refresh_review_toolbar(model)
    assert dock.call_count == 1
    assert review_registry[key].toolbar_docked is True


def test_disposing_clears_docked_state(review_registry):
    key = _doc_key("uid-E")
    listener = rva.ReviewModifyListener(key)
    review_registry[key] = rva.ReviewViewAttachment(key=key, model=object(), modify_listener=listener, toolbar_docked=True)
    listener.disposing(MagicMock())
    assert key not in review_registry


# --- modified() must never recount inside the modify notification ------------
# Regression: typing a comment while a review was open fired modified() from inside
# the comment editor's EditEngine::InsertText. The recount's XTextCursor.getString()
# forced a synchronous repaint that re-entered the half-updated comment box and
# crashed; LibreOffice's emergency-save dialog then hung the app (11 .hang reports
# on one machine in one night, all in sw::sidebarwindows::SidebarTextControl).

def _visible_toolbar_patch(visible=True):
    lm = MagicMock()
    lm.isElementVisible.return_value = visible
    return patch.object(rva, "layout_manager", return_value=lm)


def _event(model):
    ev = MagicMock()
    ev.Source = model
    return ev


def _reset_pending():
    with rva._pending_lock:
        rva._pending_refresh.clear()


def _capture_posts():
    posted = []
    return posted, patch("plugin.framework.queue_executor.main.post",
                         side_effect=lambda fn, *a, **k: posted.append((fn, a)))


def test_modified_does_not_recount_synchronously():
    _reset_pending()
    listener = rva.ReviewModifyListener(_doc_key("uid-sync"))
    posted, post_patch = _capture_posts()
    with _visible_toolbar_patch(True), post_patch, patch.object(rva, "refresh_review_toolbar") as refresh:
        listener.modified(_event(FakeModel("uid-sync")))
        refresh.assert_not_called()
    assert [fn for fn, _ in posted] == [rva._run_deferred_refresh], "the recount must be queued, not run"
    _reset_pending()


def test_keystroke_burst_collapses_into_one_queued_recount():
    _reset_pending()
    listener = rva.ReviewModifyListener(_doc_key("uid-burst"))
    model = FakeModel("uid-burst")
    posted, post_patch = _capture_posts()
    with _visible_toolbar_patch(True), post_patch, patch.object(rva, "refresh_review_toolbar"):
        for _ in range(25):  # a long comment, one modified() per keystroke
            listener.modified(_event(model))
    assert len(posted) == 1
    _reset_pending()


def test_normal_typing_without_a_review_queues_nothing():
    _reset_pending()
    listener = rva.ReviewModifyListener(_doc_key("uid-idle"))
    posted, post_patch = _capture_posts()
    with _visible_toolbar_patch(False), post_patch, patch.object(rva, "refresh_review_toolbar") as refresh:
        listener.modified(_event(FakeModel("uid-idle")))
    refresh.assert_not_called()
    assert posted == []


def test_queued_recount_runs_once_then_allows_the_next():
    _reset_pending()
    model = FakeModel("uid-run")
    posted, post_patch = _capture_posts()
    with _visible_toolbar_patch(True), post_patch, patch.object(rva, "refresh_review_toolbar") as refresh:
        rva.schedule_toolbar_refresh(model, "uid-run")
        fn, args = posted[0]
        fn(*args)  # the main loop gets to it
        refresh.assert_called_once()
        rva.schedule_toolbar_refresh(model, "uid-run")  # a later keystroke queues again
    assert len(posted) == 2
    _reset_pending()


def test_deferred_recount_rechecks_visibility():
    _reset_pending()
    model = FakeModel("uid-v")
    with rva._pending_lock:
        rva._pending_refresh.add("uid-v")
    with _visible_toolbar_patch(False), patch.object(rva, "refresh_review_toolbar") as refresh:
        rva._run_deferred_refresh(model, "uid-v")
    refresh.assert_not_called()


def test_closing_the_document_cancels_a_queued_recount(review_registry):
    _reset_pending()
    model = FakeModel("uid-close")
    rva.ensure_modify_listener(model)
    key = _doc_key("uid-close")
    posted, post_patch = _capture_posts()
    with _visible_toolbar_patch(True), post_patch, patch.object(rva, "refresh_review_toolbar") as refresh:
        review_registry[key].modify_listener.modified(_event(model))
        rva.release_document(model)
        fn, args = posted[0]
        fn(*args)  # the queued run arrives after the close
        refresh.assert_not_called()
    _reset_pending()


def test_a_failed_queue_does_not_block_later_recounts():
    _reset_pending()
    model = FakeModel("uid-fail")
    with patch("plugin.framework.queue_executor.main.post", side_effect=RuntimeError("no AsyncCallback")):
        rva.schedule_toolbar_refresh(model, "uid-fail")
    assert "uid-fail" not in rva._pending_refresh, "a failed post must not leave the doc marked as queued"


def test_non_writer_unload_does_not_ask_disposed_controller(review_registry):
    broadcaster = MagicMock()
    ctx = MagicMock()
    ctx.getServiceManager.return_value.createInstanceWithContext.return_value = broadcaster
    with patch("plugin.framework.uno_context.get_desktop", return_value=None):
        rva.install(ctx)
        listener = broadcaster.addDocumentEventListener.call_args.args[0]
    impress = MagicMock()
    impress.supportsService.return_value = False
    impress.getCurrentController.side_effect = RuntimeError("DrawController object has already been disposed")
    event = MagicMock(EventName="OnUnload", ViewController=None, Source=impress)
    with patch.object(rva, "release_document") as unreg:
        listener.on_document_event(event)
    impress.getCurrentController.assert_not_called()
    unreg.assert_not_called()


def test_dock_failure_is_retried_on_the_next_show(review_registry):
    _reset_pending()
    model = FakeModel("uid-dock")
    key = _doc_key("uid-dock")
    lm = MagicMock()
    with patch.object(rva, "layout_manager", return_value=lm), \
         patch.object(rva, "_dock_top", side_effect=[False, True]) as dock, \
         patch("plugin.writer.review_scan.agent_change_presence",
               side_effect=[(1, True), (0, True), (1, True)]):
        rva.refresh_review_toolbar(model)
        assert not review_registry.get(key, rva.ReviewViewAttachment(key, model)).toolbar_docked
        rva.refresh_review_toolbar(model)
        rva.refresh_review_toolbar(model)
    assert dock.call_count == 2
    assert review_registry[key].toolbar_docked is True
    _reset_pending()


def test_dock_top_returns_boolean():
    lm = MagicMock()
    assert rva._dock_top(lm) is True
    lm.dockWindow.side_effect = RuntimeError("dock error")
    assert rva._dock_top(lm) is False


def test_refresh_review_toolbar_show_hide_unreliable():
    lm = MagicMock()
    model = FakeModel("uid-sh")
    with patch.object(rva, "layout_manager", return_value=lm), \
         patch.object(rva, "_dock_top", return_value=True):
        # 1 pending change -> show
        with patch("plugin.writer.review_scan.agent_change_presence", return_value=(1, True)):
            rva.refresh_review_toolbar(model)
            lm.showElement.assert_called_once_with(rva.TOOLBAR_RESOURCE_URL)
            lm.hideElement.assert_not_called()

        lm.reset_mock()
        # 0 pending changes -> hide
        with patch("plugin.writer.review_scan.agent_change_presence", return_value=(0, True)):
            rva.refresh_review_toolbar(model)
            lm.hideElement.assert_called_once_with(rva.TOOLBAR_RESOURCE_URL)
            lm.showElement.assert_not_called()

        lm.reset_mock()
        # unreliable scan -> no-op
        with patch("plugin.writer.review_scan.agent_change_presence", return_value=(0, False)):
            rva.refresh_review_toolbar(model)
            lm.showElement.assert_not_called()
            lm.hideElement.assert_not_called()


def test_install_review_toolbar_warns_on_sweep_failure(review_registry):
    ctx = MagicMock()
    ctx.getServiceManager.return_value.createInstanceWithContext.return_value = MagicMock()
    with patch("plugin.framework.uno_context.get_desktop", side_effect=RuntimeError("sweep error")), \
         patch.object(rva.log, "warning") as warn:
        rva.install(ctx)
        warn.assert_any_call("review_view_attachment: initial sweep failed", exc_info=True)


def test_install_review_toolbar_warns_on_listener_install_failure(review_registry):
    ctx = MagicMock()
    ctx.getServiceManager.return_value.createInstanceWithContext.side_effect = RuntimeError("service error")
    with patch("plugin.framework.uno_context.get_desktop", return_value=None), \
         patch("plugin.framework.uno_listeners.log.warning") as warn:
        rva.install(ctx)
        warn.assert_any_call("%s: doc-event listener install failed", "review_view_attachment", exc_info=True)

