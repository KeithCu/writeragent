# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""ACPConnection stop must terminate the child and unblock send_request."""

import threading
import time
from unittest.mock import MagicMock

import pytest

from plugin.acp.acp_connection import ACPConnection
from plugin.framework.errors import ToolExecutionError


def _live_proc():
    proc = MagicMock()
    proc.poll.return_value = None
    proc.stdin = MagicMock()
    return proc


class TestACPConnectionStop:
    def test_stop_unblocks_in_flight_send_request(self):
        conn = ACPConnection(cmd_line=["agent"])
        conn._proc = _live_proc()
        conn._running = True
        errors = []

        def run():
            try:
                conn.send_request("session/prompt", {"sessionId": "s"}, timeout=30)
            except Exception as exc:
                errors.append(exc)

        worker = threading.Thread(target=run)
        worker.start()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not conn._pending:
            time.sleep(0.01)
        assert conn._pending
        conn.stop()
        worker.join(timeout=2)
        assert worker.is_alive() is False
        assert errors
        assert isinstance(errors[0], ToolExecutionError)
        assert "stopped" in str(errors[0]).lower()
        assert conn._proc is None

    def test_send_request_after_stop_does_not_wait(self):
        conn = ACPConnection(cmd_line=["agent"])
        conn._proc = _live_proc()
        conn._running = True
        conn.stop()
        started = time.monotonic()
        with pytest.raises(ToolExecutionError):
            conn.send_request("session/prompt", {}, timeout=30)
        assert time.monotonic() - started < 2

    def test_stop_keeps_a_response_already_received(self):
        conn = ACPConnection(cmd_line=["agent"])
        conn._proc = _live_proc()
        conn._running = True
        event = threading.Event()
        conn._pending[1] = {"event": event, "response": {"result": {"stopReason": "end_turn"}}}
        conn.stop()
        assert event.is_set()
        assert conn._pending[1]["response"]["result"]["stopReason"] == "end_turn"

    def test_second_stop_is_safe(self):
        conn = ACPConnection(cmd_line=["agent"])
        proc = _live_proc()
        conn._proc = proc
        conn._running = True
        conn.stop()
        conn.stop()
        proc.terminate.assert_called_once()
