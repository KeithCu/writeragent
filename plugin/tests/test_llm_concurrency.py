from __future__ import annotations

import threading
import time

from plugin.framework import llm_concurrency as lc


def test_agent_session_marks_active_with_nesting() -> None:
    assert lc.is_agent_active() is False
    with lc.agent_session():
        assert lc.is_agent_active() is True
        with lc.agent_session():
            assert lc.is_agent_active() is True
        assert lc.is_agent_active() is True
    assert lc.is_agent_active() is False


def test_llm_request_lane_serializes_callers() -> None:
    order: list[str] = []

    def first() -> None:
        with lc.llm_request_lane():
            order.append("first-enter")
            time.sleep(0.08)
            order.append("first-exit")

    def second() -> None:
        time.sleep(0.01)
        with lc.llm_request_lane():
            order.append("second-enter")

    t1 = threading.Thread(target=first)
    t2 = threading.Thread(target=second)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert order == ["first-enter", "first-exit", "second-enter"]
