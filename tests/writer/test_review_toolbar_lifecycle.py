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
    # The crash / force-close path: UNO calls disposing() on the listener; the entry must go.
    rt._modify_listeners.clear()
    model = FakeModel("uid-1")
    rt._register_modify_listener(model)
    assert "uid-1" in rt._modify_listeners
    listener = rt._modify_listeners["uid-1"]
    assert listener in model.listeners

    listener.disposing(MagicMock())
    assert "uid-1" not in rt._modify_listeners


def test_unregister_removes_listener_and_entry():
    # The clean OnUnload path still works.
    rt._modify_listeners.clear()
    model = FakeModel("uid-2")
    rt._register_modify_listener(model)
    rt._unregister_modify_listener(model)
    assert "uid-2" not in rt._modify_listeners
    assert model.listeners == []


def test_disposing_is_idempotent_and_safe():
    # disposing() for a uid that's already gone (e.g. OnUnload ran first) must not raise.
    rt._modify_listeners.clear()
    listener = rt._ReviewModifyListener("uid-3")
    listener.disposing(MagicMock())
    assert "uid-3" not in rt._modify_listeners


def test_disposing_does_not_evict_a_recycled_uid_entry():
    # If the RuntimeUID was recycled for a newer document, a late disposing() from the OLD listener
    # must not evict the NEW listener's entry (which would disable auto-hide on the new doc).
    rt._modify_listeners.clear()
    old = rt._ReviewModifyListener("uid-R")
    rt._modify_listeners["uid-R"] = old
    new = rt._ReviewModifyListener("uid-R")
    rt._modify_listeners["uid-R"] = new  # uid recycled -> new owner
    old.disposing(MagicMock())
    assert rt._modify_listeners["uid-R"] is new


def test_register_dedups_by_uid():
    rt._modify_listeners.clear()
    model = FakeModel("uid-4")
    rt._register_modify_listener(model)
    first = rt._modify_listeners["uid-4"]
    rt._register_modify_listener(model)  # second call -> dedup, no duplicate listener
    assert rt._modify_listeners["uid-4"] is first
    assert len(model.listeners) == 1


def test_toolbar_docks_once_per_document():
    # dock on first appearance only; cycling pending 0->N->0->N must not re-dock and override a
    # user who has since moved/floated the toolbar.
    rt._modify_listeners.clear()
    rt._docked_uids.clear()
    model = MagicMock()
    model.getRuntimeUID.return_value = "uid-D"
    with patch("plugin.writer.review_toolbar._layout_manager", return_value=MagicMock()), \
         patch("plugin.writer.review_toolbar._dock_top") as dock, \
         patch("plugin.writer.inline_review.pending_agent_change_count", side_effect=[1, 0, 1]):
        rt.refresh_review_toolbar(model)   # 0 -> 1 : show + dock
        rt.refresh_review_toolbar(model)   #   -> 0 : hide
        rt.refresh_review_toolbar(model)   # 0 -> 1 : show, NO re-dock
    assert dock.call_count == 1
    assert "uid-D" in rt._docked_uids


def test_disposing_clears_docked_state():
    rt._modify_listeners.clear()
    rt._docked_uids.clear()
    listener = rt._ReviewModifyListener("uid-E")
    rt._modify_listeners["uid-E"] = listener
    rt._docked_uids.add("uid-E")
    listener.disposing(MagicMock())
    assert "uid-E" not in rt._modify_listeners
    assert "uid-E" not in rt._docked_uids


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


def _capture_posts():
    posted = []
    return posted, patch("plugin.framework.queue_executor.post_to_main_thread",
                         side_effect=lambda fn, *a, **k: posted.append((fn, a)))


def test_modified_does_not_recount_synchronously():
    _reset_pending()
    listener = rt._ReviewModifyListener("uid-sync")
    posted, post_patch = _capture_posts()
    with _visible_toolbar_patch(True), post_patch, patch.object(rt, "refresh_review_toolbar") as refresh:
        listener.modified(_event(FakeModel("uid-sync")))
        refresh.assert_not_called()
    assert [fn for fn, _ in posted] == [rt._run_deferred_refresh], "the recount must be queued, not run"
    _reset_pending()


def test_keystroke_burst_collapses_into_one_queued_recount():
    _reset_pending()
    listener = rt._ReviewModifyListener("uid-burst")
    model = FakeModel("uid-burst")
    posted, post_patch = _capture_posts()
    with _visible_toolbar_patch(True), post_patch, patch.object(rt, "refresh_review_toolbar"):
        for _ in range(25):  # a long comment, one modified() per keystroke
            listener.modified(_event(model))
    assert len(posted) == 1
    _reset_pending()


def test_normal_typing_without_a_review_queues_nothing():
    _reset_pending()
    listener = rt._ReviewModifyListener("uid-idle")
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
    rt._modify_listeners.clear()
    model = FakeModel("uid-close")
    rt._register_modify_listener(model)
    posted, post_patch = _capture_posts()
    with _visible_toolbar_patch(True), post_patch, patch.object(rt, "refresh_review_toolbar") as refresh:
        rt._modify_listeners["uid-close"].modified(_event(model))
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
