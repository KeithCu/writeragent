# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for tests.eval_runner."""

from __future__ import annotations

import threading
from typing import Any
from unittest.mock import patch


def test_tool_execution_hops_to_the_main_thread_from_the_suite_worker() -> None:
    # Stripped release trees omit scripts/; skip when pricing imports are absent.
    from tests.harness.strip_bundle import skip_if_release_build

    skip_if_release_build("scripts/ not in stripped release tree")
    from tests.eval_runner import _call_on_main

    ran: list[str] = []

    def _marker() -> str:
        ran.append("marker")
        return "ok"

    def _hop(fn: Any, *a: Any, timeout: int = 120, **k: Any) -> Any:
        assert timeout == 120
        return fn(*a, **k)

    with patch("plugin.framework.queue_executor.main.call", side_effect=_hop) as hop:
        with patch("plugin.framework.queue_executor.main.is_current", return_value=True):
            assert _call_on_main(_marker) == "ok"
        hop.assert_not_called()

        seen: list[bool] = []

        def _from_worker() -> None:
            with patch("plugin.framework.queue_executor.main.is_current", return_value=False):
                seen.append(_call_on_main(_marker) == "ok")

        worker = threading.Thread(target=_from_worker)
        worker.start()
        worker.join(timeout=2)

    assert seen == [True]
    hop.assert_called_once()
    assert ran == ["marker", "marker"]
