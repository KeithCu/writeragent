# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for ``plugin.calc.python.workbook_state``."""

from __future__ import annotations

import threading
from typing import Any
from unittest.mock import patch

from plugin.calc.python.workbook_state import Debounce


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


def test_debounce_with_on_drain_brackets_due_callbacks() -> None:
    """When on_drain is set, one timer wakeup brackets due callbacks with in_drain=True then calls on_drain once."""
    events: list[tuple[str, bool]] = []
    debounce = Debounce(on_drain=lambda: events.append(("drain", debounce.in_drain)))
    posted: list[Any] = []
    all_posted = threading.Event()

    def _capture_post(fn: Any) -> None:
        posted.append(fn)
        if not debounce.pending_for_tests() and fn == debounce._end_drain:
            all_posted.set()

    try:
        with patch("plugin.framework.queue_executor.main.post", side_effect=_capture_post):
            debounce.schedule("a", 0.01, lambda: events.append(("cb1", debounce.in_drain)))
            debounce.schedule("b", 0.01, lambda: events.append(("cb2", debounce.in_drain)))
            assert all_posted.wait(2.0)
        for fn in posted:
            fn()
        assert events == [("cb1", True), ("cb2", True), ("drain", False)]
    finally:
        debounce.cancel_all()
