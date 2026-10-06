# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""agent_session exits in either order once sessions span VCL callbacks (#1324)."""

from __future__ import annotations

from plugin.framework.queue_executor import SendCancellation, agent_session, get_current_send_cancellation, is_agent_active


def test_out_of_order_exit_never_restores_a_finished_scope():
    a, b = SendCancellation(), SendCancellation()
    cm_a, cm_b = agent_session(a), agent_session(b)
    cm_a.__enter__()
    cm_b.__enter__()
    assert get_current_send_cancellation() is b
    # Doc A's drain finishes first: B stays current.
    cm_a.__exit__(None, None, None)
    assert get_current_send_cancellation() is b
    assert is_agent_active()
    # B finishes: A is done, so nothing stale comes back.
    cm_b.__exit__(None, None, None)
    assert get_current_send_cancellation() is None
    assert not is_agent_active()
    assert not a.is_cancelled() and not b.is_cancelled()


def test_nested_exit_still_restores_the_open_outer_scope():
    outer, inner = SendCancellation(), SendCancellation()
    with agent_session(outer):
        with agent_session(inner):
            assert get_current_send_cancellation() is inner
        assert get_current_send_cancellation() is outer
    assert get_current_send_cancellation() is None


def test_exception_exit_cancels_and_clears():
    scope = SendCancellation()
    try:
        with agent_session(scope):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert scope.is_cancelled()
    assert get_current_send_cancellation() is None
