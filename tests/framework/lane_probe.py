# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Test helper to verify that status_callback satisfies the lane safety contract."""

from __future__ import annotations

from typing import Any, Callable
from unittest.mock import patch

import plugin.framework.llm_lanes as llm_lanes_mod
import plugin.framework.queue_executor as queue_executor_mod


class _SpyLock:
    def __init__(self) -> None:
        self.acquire_count = 0

    def acquire(self, *args: Any, **kwargs: Any) -> bool:
        self.acquire_count += 1
        return True

    def release(self) -> None:
        pass


def assert_lane_callback_safe(cb: Callable[[str], None], text: str) -> None:
    """Invoke cb(text) and assert it neither marshals via main nor takes/re-enters the lane lock."""
    spy_lock = _SpyLock()
    real_llm_request_lane = llm_lanes_mod.llm_request_lane

    with (
        patch.object(queue_executor_mod.main, "call", side_effect=AssertionError("status_callback must not marshal via main.call")),
        patch.object(queue_executor_mod.main, "run", side_effect=AssertionError("status_callback must not marshal via main.run")),
        patch.object(queue_executor_mod.main, "post", side_effect=AssertionError("status_callback must not marshal via main.post")),
        patch.object(llm_lanes_mod, "_LLM_REQUEST_LOCK", spy_lock),
        patch("plugin.framework.llm_lanes.llm_request_lane", real_llm_request_lane),
        patch("plugin.chatbot.tool_loop.llm_request_lane", real_llm_request_lane),
        patch("plugin.chatbot.send_handlers.llm_request_lane", real_llm_request_lane),
    ):
        cb(text)

    assert spy_lock.acquire_count == 0, f"_LLM_REQUEST_LOCK acquired {spy_lock.acquire_count} time(s) from status_callback"
