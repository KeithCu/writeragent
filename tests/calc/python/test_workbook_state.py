# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for ``plugin.calc.python.workbook_state``."""

from __future__ import annotations

import threading
from unittest.mock import patch

from plugin.calc.python.workbook_state import Debounce, WorkbookState


def test_spill_timers_prune_on_fire_register_and_cancel() -> None:
    """Finished debounce timers must not keep callbacks in registry."""
    debounce = Debounce()
    try:
        ran = {"n": 0}
        debounce.schedule("fired", 0.1, lambda: ran.__setitem__("n", ran["n"] + 1))
        assert "fired" in debounce.pending_for_tests()
        debounce.flush_for_tests("fired")
        assert ran["n"] == 1
        assert "fired" not in debounce.pending_for_tests()

        debounce.schedule("live", 0.1, lambda: None)
        assert "live" in debounce.pending_for_tests()
        debounce.cancel("live")
        assert "live" not in debounce.pending_for_tests()
    finally:
        debounce.cancel_all()


def test_second_spill_timer_cancels_the_first() -> None:
    """Debounce replaces callback and maintains one active thread per token."""
    debounce = Debounce()
    try:
        called = []

        debounce.schedule("Sheet1:0:0", 0.1, lambda: called.append("first"))
        debounce.schedule("Sheet1:0:0", 0.1, lambda: called.append("second"))
        debounce.schedule("Sheet1:1:0", 0.1, lambda: called.append("third"))

        assert len(debounce.pending_for_tests()) == 2
        debounce.flush_for_tests("Sheet1:0:0")
        assert called == ["second"]
        debounce.flush_for_tests("Sheet1:1:0")
        assert called == ["second", "third"]
    finally:
        debounce.cancel_all()


def test_debounce_posts_due_callback_to_main() -> None:
    """Timer expiration dispatches due callbacks via main.post and prunes the key."""
    debounce = Debounce()
    fn = lambda: None  # noqa: E731
    post_seen = threading.Event()
    try:
        with patch("plugin.framework.queue_executor.main.post", side_effect=lambda _f: post_seen.set()) as mock_post:
            debounce.schedule("k", 0.0, fn)
            assert post_seen.wait(2.0)
            mock_post.assert_called_once_with(fn)
            assert "k" not in debounce.pending_for_tests()
    finally:
        debounce.cancel_all()


def test_workbook_state_dirty_spill_registry_schedules_once_without_rearming() -> None:
    """Marking dirty schedules 'spill-registry' once without re-arming, and flush clears it."""
    state = WorkbookState(key="uid-dirty-once")
    doc = object()
    saved: list[object] = []
    try:
        with (
            patch.object(state.debounce, "schedule", wraps=state.debounce.schedule) as mock_sched,
            patch("plugin.calc.python.spill.save_spill_registry_for_doc", side_effect=lambda d, _st: saved.append(d)),
        ):
            state.mark_spill_registry_dirty(doc)
            state.mark_spill_registry_dirty(doc)
            state.mark_spill_registry_dirty(doc)
            mock_sched.assert_called_once_with("spill-registry", 0.1, state.flush_spill_registry)
            assert "spill-registry" in state.debounce.pending_for_tests()

            state.flush_spill_registry()
            assert saved == [doc]
            assert "spill-registry" not in state.debounce.pending_for_tests()

            # A second flush (e.g. from a late timer callback) is a no-op.
            state.flush_spill_registry()
            assert saved == [doc]
    finally:
        state.clear()
