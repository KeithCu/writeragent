# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""ACPConnection stop and reader exit must unblock send_request.

A response still buffered on stdout after the child exits must be
delivered, not replaced by the reader-exit sweep.
"""

import subprocess
import sys
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


def _assert_prompt_unblocked(errors: list[BaseException], worker: threading.Thread) -> None:
    assert worker.is_alive() is False
    assert errors
    assert isinstance(errors[0], ToolExecutionError)
    text = str(errors[0]).lower()
    assert "terminated" in text or "stopped" in text


class TestACPConnectionReaderExit:
    """Reader-loop exit must fail an in-flight send_request without stop()."""

    def test_killing_child_unblocks_in_flight_send_request(self):
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        raw_stdin = proc.stdin
        assert raw_stdin is not None
        wrote = threading.Event()

        class _Stdin:
            def write(self, data: bytes) -> int:
                return raw_stdin.write(data)

            def flush(self) -> None:
                raw_stdin.flush()
                wrote.set()

            def close(self) -> None:
                raw_stdin.close()

        proc.stdin = _Stdin()  # type: ignore[assignment]
        conn = ACPConnection(cmd_line=["agent"])
        conn._proc = proc
        conn._running = True
        reader = threading.Thread(target=conn._reader_loop, daemon=True)
        reader.start()
        errors: list[BaseException] = []

        def run() -> None:
            try:
                conn.send_request("session/prompt", {"sessionId": "s"}, timeout=30)
            except Exception as exc:
                errors.append(exc)

        worker = threading.Thread(target=run)
        worker.start()
        try:
            assert wrote.wait(timeout=2)
            started = time.monotonic()
            proc.kill()
            worker.join(timeout=2)
            _assert_prompt_unblocked(errors, worker)
            assert time.monotonic() - started < 2
        finally:
            if proc.poll() is None:
                proc.kill()
            conn.stop()
            worker.join(timeout=2)
            reader.join(timeout=2)

    def test_malformed_json_unblocks_in_flight_send_request(self):
        conn = ACPConnection(cmd_line=["agent"])
        release = threading.Event()
        entered = threading.Event()
        proc = _live_proc()

        calls = {"n": 0}

        def readline() -> bytes:
            calls["n"] += 1
            if calls["n"] == 1:
                entered.set()
                assert release.wait(timeout=2)
                # Non-object JSON. The reader raises and leaves the loop.
                # A later call is EOF so a handled non-object cannot spin.
                return b"[1, 2]\n"
            return b""

        proc.stdout.readline.side_effect = readline
        conn._proc = proc
        conn._running = True
        reader = threading.Thread(target=conn._reader_loop, daemon=True)
        reader.start()
        assert entered.wait(timeout=2)
        errors: list[BaseException] = []

        def run() -> None:
            try:
                conn.send_request("session/prompt", {"sessionId": "s"}, timeout=30)
            except Exception as exc:
                errors.append(exc)

        worker = threading.Thread(target=run)
        worker.start()
        try:
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and not conn._pending:
                time.sleep(0.01)
            assert conn._pending
            started = time.monotonic()
            release.set()
            worker.join(timeout=2)
            _assert_prompt_unblocked(errors, worker)
            assert time.monotonic() - started < 2
            assert "terminated" in str(errors[0]).lower()
        finally:
            release.set()
            conn.stop()
            worker.join(timeout=2)
            reader.join(timeout=2)

    def test_response_buffered_after_child_exit_is_returned(self):
        """In-flight send_request returns a line written as the child exits.

        The child emits a session/update, then waits. The reader is parked
        in that callback when the child writes the JSON-RPC result and
        exits, so the next loop check observes a dead process with the
        answer still unread. That result must win over the exit sweep.
        """
        script = r"""
import json, sys
note = {"jsonrpc": "2.0", "method": "session/update", "params": {}}
sys.stdout.buffer.write((json.dumps(note) + "\n").encode())
sys.stdout.buffer.flush()
sys.stdin.buffer.readline()
resp = {"jsonrpc": "2.0", "id": 1, "result": {"text": "kept-answer"}}
sys.stdout.buffer.write((json.dumps(resp) + "\n").encode())
sys.stdout.buffer.flush()
"""
        proc = subprocess.Popen(
            [sys.executable, "-c", script],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        in_callback = threading.Event()
        release = threading.Event()

        def on_notify(method: str, params: object, msg_id: object = None) -> None:
            # Park the reader between lines until the child has exited.
            # A mismatch must not set the gate: the reader swallows callback
            # errors and would otherwise block in the next readline, hiding
            # the poll() race this test is here to catch.
            assert method == "session/update"
            assert params == {}
            assert msg_id is None
            in_callback.set()
            assert release.wait(timeout=5)

        conn = ACPConnection(cmd_line=["agent"])
        conn._proc = proc
        conn._running = True
        conn.set_notification_callback(on_notify)
        reader = threading.Thread(target=conn._reader_loop, daemon=True)
        reader.start()
        results: list[object] = []
        errors: list[BaseException] = []

        def run() -> None:
            try:
                results.append(conn.send_request("session/prompt", {"sessionId": "s"}, timeout=5))
            except Exception as exc:
                errors.append(exc)

        worker = threading.Thread(target=run, daemon=True)
        try:
            assert in_callback.wait(timeout=2)
            worker.start()
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and proc.poll() is None:
                time.sleep(0.01)
            assert proc.poll() is not None
            started = time.monotonic()
            release.set()
            worker.join(timeout=2)
            assert worker.is_alive() is False
            assert errors == []
            assert results == [{"text": "kept-answer"}]
            assert time.monotonic() - started < 2
        finally:
            release.set()
            if proc.poll() is None:
                proc.kill()
            conn.stop()
            if worker.ident is not None:
                worker.join(timeout=2)
            reader.join(timeout=2)
