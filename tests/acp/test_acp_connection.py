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
                # Non-object JSON is skipped. The next readline is EOF,
                # which ends the loop and must still unblock the prompt.
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

    def test_non_object_json_skipped_while_prompt_in_flight(self):
        """Bare number, true/false, string, and array lines must not kill the reader.

        What was wrong: those lines parse, then ``"id" in msg`` or ``msg.get``
        raised, the inner except left the loop, and the exit sweep failed the
        prompt with "ACP process terminated" while the child was still alive.
        The response that follows those lines has to be returned.
        """
        conn = ACPConnection(cmd_line=["agent"])
        release = threading.Event()
        entered = threading.Event()
        done = threading.Event()
        proc = _live_proc()
        lines = [
            b"42\n",
            b"true\n",
            b"false\n",
            b'"stray"\n',
            b"[1, 2]\n",
            b'{"jsonrpc": "2.0", "id": 1, "result": {"text": "kept-answer"}}\n',
        ]
        calls = {"n": 0}

        def readline() -> bytes:
            calls["n"] += 1
            if calls["n"] == 1:
                entered.set()
                assert release.wait(timeout=2)
            index = calls["n"] - 1
            if index < len(lines):
                return lines[index]
            # Hold past the assertion so EOF cannot be what delivered the result.
            assert done.wait(timeout=5)
            return b""

        proc.stdout.readline.side_effect = readline
        conn._proc = proc
        conn._running = True
        reader = threading.Thread(target=conn._reader_loop, daemon=True)
        reader.start()
        assert entered.wait(timeout=2)
        results: list[object] = []
        errors: list[BaseException] = []

        def run() -> None:
            try:
                results.append(conn.send_request("session/prompt", {"sessionId": "s"}, timeout=5))
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
            assert worker.is_alive() is False
            assert errors == []
            assert results == [{"text": "kept-answer"}]
            assert reader.is_alive()
            assert time.monotonic() - started < 2
        finally:
            done.set()
            release.set()
            conn.stop()
            worker.join(timeout=2)
            reader.join(timeout=2)

    def test_unhashable_id_does_not_kill_reader(self):
        """An object id that is an array, object, or JSON boolean must not kill the reader.

        What was wrong: those lines parse as dicts, then ``pending.get(req_id)``
        raised TypeError (unhashable) or, for JSON ``true``, hashed equal to
        request id 1 and stole the in-flight response. The inner except left
        the loop and the exit sweep failed the prompt with "ACP process
        terminated" while the child was still alive. The response that follows
        has to be returned, and the reader has to stay up.
        """
        conn = ACPConnection(cmd_line=["agent"])
        release = threading.Event()
        entered = threading.Event()
        done = threading.Event()
        proc = _live_proc()
        lines = [
            b'{"jsonrpc": "2.0", "id": true, "result": {"text": "stolen"}}\n',
            b'{"jsonrpc": "2.0", "id": [1], "result": {}}\n',
            b'{"jsonrpc": "2.0", "id": {"n": 1}, "result": {}}\n',
            b'{"jsonrpc": "2.0", "id": 1, "result": {"text": "kept-answer"}}\n',
        ]
        calls = {"n": 0}

        def readline() -> bytes:
            calls["n"] += 1
            if calls["n"] == 1:
                entered.set()
                assert release.wait(timeout=2)
            index = calls["n"] - 1
            if index < len(lines):
                return lines[index]
            # Hold past the assertion so EOF cannot be what delivered the result.
            assert done.wait(timeout=5)
            return b""

        proc.stdout.readline.side_effect = readline
        conn._proc = proc
        conn._running = True
        reader = threading.Thread(target=conn._reader_loop, daemon=True)
        reader.start()
        assert entered.wait(timeout=2)
        results: list[object] = []
        errors: list[BaseException] = []

        def run() -> None:
            try:
                results.append(conn.send_request("session/prompt", {"sessionId": "s"}, timeout=5))
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
            assert worker.is_alive() is False
            assert errors == []
            assert results == [{"text": "kept-answer"}]
            assert reader.is_alive()
            assert time.monotonic() - started < 2
        finally:
            done.set()
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

    def test_proc_cleared_between_check_and_readline_is_not_a_reader_error(self, caplog):
        """stop() may null _proc after the loop sees it and before stdout.

        What was wrong: the body read self._proc.stdout. That second lookup
        was None, so AttributeError was logged as Reader error. The second
        _proc read here is None, the same window stop() opens.
        """
        caplog.set_level("ERROR", logger="plugin.acp.acp_connection")
        proc = _live_proc()
        reads = {"n": 0}

        def readline() -> bytes:
            reads["n"] += 1
            return b""

        proc.stdout.readline.side_effect = readline

        class _SecondProcReadIsNone(ACPConnection):
            def __getattribute__(self, name: str):
                if name == "_proc":
                    count = object.__getattribute__(self, "_proc_reads")
                    object.__setattr__(self, "_proc_reads", count + 1)
                    if count == 0:
                        return object.__getattribute__(self, "_live_proc")
                    return None
                return object.__getattribute__(self, name)

        conn = _SecondProcReadIsNone(cmd_line=["agent"])
        conn._live_proc = proc
        conn._proc_reads = 0
        conn._running = True
        reader = threading.Thread(target=conn._reader_loop, daemon=True)
        reader.start()
        reader.join(timeout=2)
        assert reader.is_alive() is False
        assert reads["n"] == 1
        assert "Reader error" not in caplog.text


def _proc_nulled_on_bool(conn: ACPConnection, stdin: object):
    """Process whose truthiness test clears ``conn._proc``.

    ``if self._proc and self._proc.stdin`` loads ``_proc`` twice. The
    truthiness test is the gap ``stop()`` uses to set ``_proc`` to None,
    so the second load used to be ``None.stdin``.
    """

    class _Proc:
        def poll(self):
            return None

        def __bool__(self) -> bool:
            conn._proc = None
            return True

        @property
        def stdin(self):
            return stdin

    return _Proc()


class _Stdin:
    def __init__(self) -> None:
        self.written: list[bytes] = []

    def write(self, data: bytes) -> int:
        self.written.append(data)
        return len(data)

    def flush(self) -> None:
        return None


class TestStdinWriteCapturesProc:
    """stop() may clear _proc between the two loads in `self._proc and self._proc.stdin`."""

    def test_send_request_writes_captured_proc(self):
        conn = ACPConnection(cmd_line=["agent"])
        stdin = _Stdin()

        def flush() -> None:
            conn._wake_pending("ACP process stopped")

        stdin.flush = flush  # type: ignore[method-assign]
        conn._proc = _proc_nulled_on_bool(conn, stdin)
        conn._running = True
        with pytest.raises(ToolExecutionError, match="stopped"):
            conn.send_request("session/prompt", {"sessionId": "s"}, timeout=2)
        assert stdin.written
        assert b"session/prompt" in stdin.written[0]

    def test_send_request_closed_stdin_is_tool_error(self):
        """A pipe stop() already closed must not escape as ValueError.

        Capturing the process means the write can run after stop() has
        closed stdin. That raises ValueError, not BrokenPipeError.
        """
        conn = ACPConnection(cmd_line=["agent"])
        proc = _live_proc()

        def write(data: bytes) -> int:
            raise ValueError("I/O operation on closed file")

        proc.stdin.write.side_effect = write
        conn._proc = proc
        conn._running = True
        with pytest.raises(ToolExecutionError, match="Failed to write"):
            conn.send_request("initialize", {}, timeout=2)

    def test_send_notification_writes_captured_proc(self):
        conn = ACPConnection(cmd_line=["agent"])
        stdin = _Stdin()
        conn._proc = _proc_nulled_on_bool(conn, stdin)
        conn._running = True
        conn.send_notification("session/cancel", {"sessionId": "s"})
        import time
        for _ in range(50):
            if stdin.written:
                break
            time.sleep(0.01)
        assert stdin.written
        assert b"session/cancel" in stdin.written[0]

    def test_send_response_writes_captured_proc(self):
        conn = ACPConnection(cmd_line=["agent"])
        stdin = _Stdin()
        conn._proc = _proc_nulled_on_bool(conn, stdin)
        conn._running = True
        conn.send_response(4, result={"outcome": {"outcome": "cancelled"}})
        import time
        for _ in range(50):
            if stdin.written:
                break
            time.sleep(0.01)
        assert stdin.written
        assert b'"id": 4' in stdin.written[0]


def test_stop_flushes_before_terminate():
    from unittest.mock import MagicMock

    conn = ACPConnection(["dummy"])
    mock_proc = MagicMock()
    mock_stdin = MagicMock()
    mock_proc.stdin = mock_stdin

    events = []

    def mock_write(_data):
        events.append("write")

    def mock_terminate():
        events.append("terminate")

    mock_stdin.write.side_effect = mock_write
    mock_proc.terminate.side_effect = mock_terminate

    conn._proc = mock_proc
    conn._running = True
    mock_proc.poll.return_value = None

    conn.send_notification("session/cancel", {})
    conn.stop()

    import time
    for _ in range(50):
        if "terminate" in events:
            break
        time.sleep(0.01)

    assert "write" in events, "write was never called"
    assert "terminate" in events, "terminate was never called"
    assert events.index("write") < events.index("terminate")
