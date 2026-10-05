from plugin.framework.errors import WorkerPoolError
from unittest.mock import MagicMock, patch
import pytest
import time
import subprocess
import sys

import threading

from plugin.framework.thread_guard import get_background_task_name
from plugin.framework.worker_pool import (
    BackgroundHandle,
    _DaemonWorkPool,
    background_pool_max_workers,
    reset_background_pool_for_tests,
    run_in_background,
    AsyncProcess,
    start_stderr_drain,
    _get_pool,
)
from plugin.framework.errors import ToolExecutionError

def test_run_in_background_success():
    result = []
    def success_func():
        result.append(True)
        return "done"

    t = run_in_background(success_func)
    t.join()
    assert result == [True]

def test_run_in_background_exception():
    error_called = []
    def error_func():
        raise ValueError("test error")

    def error_cb(err):
        error_called.append(err)

    t = run_in_background(error_func, error_callback=error_cb)
    t.join()

    assert len(error_called) == 1
    assert isinstance(error_called[0], WorkerPoolError)
    assert error_called[0].code == "WORKER_TASK_FAILED"
    assert "test error" in error_called[0].details["original_error"]
    assert error_called[0].details["error_type"] == "ValueError"

def test_run_in_background_exception_in_error_callback():
    error_called = []
    def error_func():
        raise RuntimeError("first error")

    def error_cb(err):
        error_called.append(err)
        raise RuntimeError("second error")

    # Should not crash the program
    t = run_in_background(error_func, error_callback=error_cb)
    t.join()
    assert len(error_called) == 1

def test_async_process_init():
    ap = AsyncProcess(["ls", "-l"], stdout_cb=lambda x: None)
    assert ap.args == ["ls", "-l"]
    assert ap._popen_kwargs["stdout"] == subprocess.PIPE
    assert ap._popen_kwargs["stderr"] == subprocess.PIPE
    assert ap._popen_kwargs["text"] is False
    assert ap._popen_kwargs["bufsize"] == 0
    assert ap.is_running is False

def test_async_process_start_success():
    stdout_lines = []
    stderr_lines = []
    exit_codes = []

    def on_stdout(line):
        stdout_lines.append(line)

    def on_stderr(line):
        stderr_lines.append(line)

    def on_exit(code):
        exit_codes.append(code)

    ap = AsyncProcess(
        [sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr)"],
        stdout_cb=on_stdout,
        stderr_cb=on_stderr,
        on_exit_cb=on_exit
    )
    ap.start()
    assert ap.is_running is True

    ap._wait_thread.join(timeout=2)
    assert ap.is_running is False

    # Allow some time for stream reading threads to finish
    if ap._stdout_thread:
        ap._stdout_thread.join(timeout=1)
    if ap._stderr_thread:
        ap._stderr_thread.join(timeout=1)

    assert any("out" in line for line in stdout_lines)
    assert any("err" in line for line in stderr_lines)
    assert exit_codes == [0]

def test_async_process_start_drain_only():
    ap = AsyncProcess([sys.executable, "-c", "print('hello')"])
    ap.start()
    ap._wait_thread.join(timeout=2)
    assert ap.is_running is False


def test_stderr_drain_prevents_pipe_deadlock():
    """Child floods stderr before reading stdin; parent must not hang on the write/read."""
    script = (
        "import sys\n"
        "sys.stderr.write('x' * (128 * 1024))\n"
        "sys.stderr.flush()\n"
        "line = sys.stdin.buffer.readline()\n"
        "sys.stdout.buffer.write(b'ok:' + line)\n"
        "sys.stdout.buffer.flush()\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )
    # Keep a large diagnostic tail so we can prove the flood was drained (not dropped unread).
    drain = start_stderr_drain(proc.stderr, max_tail_chars=256 * 1024, name="test-stderr-flood")
    assert drain is not None
    assert proc.stdin is not None and proc.stdout is not None
    proc.stdin.write(b"hello\n")
    proc.stdin.flush()
    # Bounded wait: without a live drain this historically deadlocks on Linux pipes.
    deadline = time.time() + 10.0
    out = b""
    while time.time() < deadline and b"\n" not in out:
        chunk = proc.stdout.read(64)
        if not chunk:
            break
        out += chunk
    proc.wait(timeout=5)
    assert out.startswith(b"ok:hello")
    assert len(drain.text()) >= 128 * 1024

def test_async_process_start_error():
    ap = AsyncProcess(["/path/to/nonexistent/executable/xyz123"])
    with pytest.raises(ToolExecutionError) as exc:
        ap.start()
    assert "Failed to start process" in str(exc.value)

def test_async_process_wait_for_exit_callback_error():
    def on_exit_error(code):
        raise ValueError("exit callback error")

    ap = AsyncProcess([sys.executable, "-c", "pass"], on_exit_cb=on_exit_error)
    ap.start()
    ap._wait_thread.join(timeout=2)
    # The error should be caught and logged, not crash
    assert ap.is_running is False

def test_async_process_terminate_does_not_join_on_caller():
    ap = AsyncProcess([sys.executable, "-c", "import time; time.sleep(30)"])
    ap.start()
    caller = threading.current_thread()

    def slow_join(self, timeout=None):
        assert threading.current_thread() is not caller
        time.sleep(0.05)

    try:
        with patch.object(BackgroundHandle, "join", slow_join):
            started = time.monotonic()
            ap.terminate()
            assert time.monotonic() - started < 0.4
    finally:
        if ap.process is not None and ap.process.poll() is None:
            ap.process.kill()
            ap.process.wait(timeout=3)


def test_async_process_exit_callback_sees_trailing_stdout():
    lines: list[str] = []
    saw: list[list[str]] = []
    started = threading.Event()
    release = threading.Event()

    def on_line(line: str) -> None:
        started.set()
        assert release.wait(timeout=2)
        lines.append(line)

    def on_exit(rc: int) -> None:
        saw.append(list(lines))

    script = "import sys; sys.stdout.write('hello'); sys.stdout.flush()"
    ap = AsyncProcess([sys.executable, "-c", script], stdout_cb=on_line, on_exit_cb=on_exit)
    ap.start()
    assert started.wait(timeout=2)
    time.sleep(0.2)
    release.set()
    assert ap._wait_thread is not None
    ap._wait_thread.join(timeout=3)
    assert saw == [["hello"]]


def test_wait_for_exit_delivers_callback_when_reader_never_ends():
    """A reader blocked on an inherited pipe must not skip on_exit_cb."""
    exits: list[int] = []
    ap = AsyncProcess(["dummy"], on_exit_cb=exits.append)
    release = threading.Event()
    reader = run_in_background(lambda: release.wait(30), dedicated=True, name="held-pipe")
    proc = MagicMock()
    proc.wait.return_value = 3
    done = threading.Event()

    def run() -> None:
        try:
            ap._wait_for_exit(proc, reader, None)
        finally:
            done.set()

    waiter = threading.Thread(target=run, daemon=True)
    waiter.start()
    try:
        assert done.wait(3), "on_exit_cb blocked on a reader that never saw EOF"
        assert exits == [3]
        assert reader.is_alive()
    finally:
        release.set()
        reader.join(timeout=2)
        waiter.join(timeout=2)


def test_async_process_terminate():
    ap = AsyncProcess([sys.executable, "-c", "import time; time.sleep(10)"])
    ap.start()
    assert ap.is_running is True

    ap.terminate()
    ap._wait_thread.join(timeout=2)
    assert ap.is_running is False

def test_async_process_terminate_timeout():
    ap = AsyncProcess([sys.executable, "-c", "import time; time.sleep(10)"])
    ap.start()

    # Force a TimeoutExpired to hit the .kill() branch
    original_wait = ap.process.wait
    def mocked_wait(*args, **kwargs):
        if "timeout" in kwargs:
            raise subprocess.TimeoutExpired(ap.args, kwargs["timeout"])
        return original_wait(*args, **kwargs)

    ap.process.wait = mocked_wait

    ap.terminate(timeout=0.1)
    ap._wait_thread.join(timeout=2)
    assert ap.is_running is False

def test_async_process_read_stream_errors():
    ap = AsyncProcess(["ls"])

    # ValueError from a closed pipe ends the reader and still closes.
    mock_stream = MagicMock()
    mock_stream.buffer.read1.side_effect = ValueError("I/O operation on closed file")
    ap._read_stream(mock_stream, lambda x: None)
    mock_stream.close.assert_called()

    mock_stream = MagicMock()
    mock_stream.buffer.read1.side_effect = OSError("read error")
    ap._read_stream(mock_stream, lambda x: None)
    mock_stream.close.assert_called()

    mock_stream = MagicMock()
    mock_stream.buffer.read1.return_value = b""
    mock_stream.close.side_effect = OSError("close error")
    ap._read_stream(mock_stream, lambda x: None)
    mock_stream.close.assert_called()

def test_async_process_drain_stream_errors():
    ap = AsyncProcess(["ls"])

    mock_stream = MagicMock()
    mock_stream.buffer.read1.side_effect = OSError("drain error")
    ap._drain_stream(mock_stream)
    mock_stream.close.assert_called()

    mock_stream = MagicMock()
    mock_stream.buffer.read1.return_value = b""
    mock_stream.close.side_effect = OSError("close error")
    ap._drain_stream(mock_stream)
    mock_stream.close.assert_called()


class _ChunkStream:
    """Bytes delivered by read1, including a newline-free burst."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)
        self.closed = False

    def read1(self, size: int) -> bytes:
        del size
        if not self._chunks:
            return b""
        return self._chunks.pop(0)

    def close(self) -> None:
        self.closed = True


def test_async_process_read_stream_short_reads_and_callback_errors():
    ap = AsyncProcess(["dummy"])
    burst = b"x" * 8192
    stream = _ChunkStream([burst, b"\nnext\n"])
    received = []

    def callback(line: str) -> None:
        received.append(line)
        if line == "x" * 8192:
            raise RuntimeError("callback failed")

    ap._read_stream(stream, callback)
    assert received == ["x" * 8192, "next"]
    assert stream.closed is True

def test_async_process_terminate_not_running():
    ap = AsyncProcess([sys.executable, "-c", "pass"])
    # Not started, terminate should return silently
    ap.terminate()

    # Started but already exited
    ap.start()
    ap._wait_thread.join(timeout=2)
    ap.terminate()


def test_pooled_handle_is_alive_and_join():
    started = threading.Event()
    release = threading.Event()

    def blocker():
        started.set()
        release.wait(2)

    handle = run_in_background(blocker, name="pool-alive")
    assert isinstance(handle, BackgroundHandle)
    assert started.wait(2)
    assert handle.is_alive()
    release.set()
    handle.join(timeout=2)
    assert not handle.is_alive()


def test_join_cancelled_future_does_not_raise():
    from concurrent.futures import Future

    fut = Future()
    assert fut.cancel()
    BackgroundHandle(future=fut).join(timeout=0.1)


def test_join_does_not_reraise_worker_exception():
    def boom():
        raise RuntimeError("pool boom")

    handle = run_in_background(boom, name="pool-boom")
    handle.join(timeout=2)
    assert not handle.is_alive()


def test_pooled_worker_tags_then_clears_task_name():
    seen = []

    def task():
        seen.append(get_background_task_name())

    handle = run_in_background(task, name="tagged-job")
    handle.join(timeout=2)
    assert seen == ["tagged-job"]
    assert get_background_task_name() is None


def test_timeout_join_dedicated_still_alive():
    release = threading.Event()

    def sleeper():
        release.wait(5)

    handle = run_in_background(sleeper, name="timeout-join", dedicated=True)
    handle.join(timeout=0.05)
    assert handle.is_alive()
    release.set()
    handle.join(timeout=2)
    assert not handle.is_alive()


def test_daemon_false_uses_dedicated_thread():
    handle = run_in_background(lambda: None, name="non-daemon", daemon=False)
    handle.join(timeout=2)
    assert handle._thread is not None
    assert handle._future is None


def test_pool_bounds_native_thread_count():
    reset_background_pool_for_tests(max_workers=2)
    try:
        two_running = threading.Event()
        release = threading.Event()
        n_started = 0
        start_lock = threading.Lock()
        handles = []

        def job():
            nonlocal n_started
            with start_lock:
                n_started += 1
                if n_started >= 2:
                    two_running.set()
            release.wait(5)

        for i in range(8):
            handles.append(run_in_background(job, name=f"bound-{i}"))

        assert two_running.wait(2)
        pool = _get_pool()
        assert len(pool._threads) == 2
        assert all(t.is_alive() for t in pool._threads)
        # Live pool workers only — retired threads from a prior larger pool
        # keep running until their current job ends and must not count here.
        live = [t for t in threading.enumerate() if t.name.startswith("wa-bg-") and not t.name.startswith("wa-bg-retired-")]
        assert len(live) == 2
        release.set()
        for h in handles:
            h.join(timeout=3)
    finally:
        reset_background_pool_for_tests()


def test_reset_background_pool_creates_new_executor():
    reset_background_pool_for_tests(max_workers=1)
    first = []

    def mark():
        first.append(threading.current_thread().name)

    run_in_background(mark, name="before-reset").join(timeout=2)
    reset_background_pool_for_tests(max_workers=1)
    second = []

    def mark2():
        second.append(threading.current_thread().name)

    run_in_background(mark2, name="after-reset").join(timeout=2)
    assert first and second
    # After shutdown the old wa-bg-0 is dead; a new pool thread may reuse the name.
    assert first[0].startswith("wa-bg-")
    assert second[0].startswith("wa-bg-")
    reset_background_pool_for_tests()


def test_reset_background_pool_while_job_submits_does_not_deadlock(monkeypatch: pytest.MonkeyPatch):
    """Joining the pool under _pool_lock deadlocks a job that calls run_in_background."""
    import plugin.framework.worker_pool as worker_pool

    reset_background_pool_for_tests(max_workers=1)
    entered = threading.Event()
    joining = threading.Event()
    nested = threading.Event()
    real_join = worker_pool._join_pool_workers

    def wrapped(threads: list[threading.Thread]) -> None:
        joining.set()
        real_join(threads)

    monkeypatch.setattr(worker_pool, "_join_pool_workers", wrapped)

    def job() -> None:
        entered.set()
        assert joining.wait(3)
        run_in_background(lambda: nested.set(), name="nested-from-pool-job").join(timeout=2)

    run_in_background(job, name="holds-pool-worker")
    assert entered.wait(2)
    reset_thread = threading.Thread(target=lambda: reset_background_pool_for_tests(max_workers=1), daemon=True)
    reset_thread.start()
    reset_thread.join(3)
    try:
        assert not reset_thread.is_alive(), "reset_background_pool_for_tests deadlocked on _pool_lock"
        assert nested.is_set()
    finally:
        if not reset_thread.is_alive():
            reset_background_pool_for_tests()


def test_background_pool_max_workers_default_and_env(monkeypatch: pytest.MonkeyPatch):
    reset_background_pool_for_tests()
    monkeypatch.delenv("WRITERAGENT_BG_POOL_WORKERS", raising=False)
    assert background_pool_max_workers() == 2

    monkeypatch.setenv("WRITERAGENT_BG_POOL_WORKERS", "4")
    assert background_pool_max_workers() == 4

    monkeypatch.setenv("WRITERAGENT_BG_POOL_WORKERS", "invalid")
    assert background_pool_max_workers() == 2

    reset_background_pool_for_tests(max_workers=5)
    assert background_pool_max_workers() == 5
    reset_background_pool_for_tests()


class TestWorkerPoolErrorHandling():

    def test_run_in_background_success_thread_exits(self):

        def mock_task(x, y):
            return (x + y)
        thread = run_in_background(mock_task, 2, 3)
        thread.join()
        assert (not thread.is_alive())

    def test_run_in_background_failure(self):
        error_cb = MagicMock()

        def mock_task():
            raise ValueError('Test error')
        thread = run_in_background(mock_task, error_callback=error_cb)
        thread.join()
        assert (error_cb.call_count == 1)
        wrapped_error = error_cb.call_args[0][0]
        assert isinstance(wrapped_error, WorkerPoolError)
        assert ("Task 'mock_task' failed" in wrapped_error.message)
        assert (wrapped_error.code == 'WORKER_TASK_FAILED')
        assert (wrapped_error.details['error_type'] == 'ValueError')


class TestReadStreamStripsNewlines:
    """Regression test for Bug 1: rstrip("\\n\\r") used literal chars, not newlines."""

    def _make_stream(self, lines):
        """Return a closeable text stream that yields the given strings."""
        import io
        return io.StringIO("".join(lines))

    def test_strips_lf(self):
        # Lines from a subprocess on Unix end with \n; the callback must not see it.
        ap = AsyncProcess(["dummy"])
        received = []
        stream = self._make_stream(["hello\n", "world\n"])
        ap._read_stream(stream, received.append)
        assert received == ["hello", "world"], (
            f"Expected no trailing newlines but got: {received!r}"
        )

    def test_strips_crlf(self):
        # Lines from a subprocess on Windows (or text=True on Windows) end with \r\n.
        ap = AsyncProcess(["dummy"])
        received = []
        stream = self._make_stream(["hello\r\n", "world\r\n"])
        ap._read_stream(stream, received.append)
        assert received == ["hello", "world"], (
            f"Expected no trailing CRLF but got: {received!r}"
        )

    def test_empty_line_not_dropped(self):
        # An empty line (just "\n") should yield an empty string, not be skipped.
        ap = AsyncProcess(["dummy"])
        received = []
        stream = self._make_stream(["\n"])
        ap._read_stream(stream, received.append)
        assert received == [""], f"Expected [\"\"] but got: {received!r}"

    def test_partial_line_delivered_at_eof(self):
        ap = AsyncProcess(["dummy"])
        received = []
        stream = self._make_stream(["hello"])
        ap._read_stream(stream, received.append)
        assert received == ["hello"]

    def test_non_newline_separators_stay_in_the_line(self):
        # splitlines() would break on these and leave the separator in the text.
        ap = AsyncProcess(["dummy"])
        received = []
        stream = self._make_stream(["a\vb\fc\u2028d\u2029e\n"])
        ap._read_stream(stream, received.append)
        assert received == ["a\vb\fc\u2028d\u2029e"]

    def test_crlf_split_across_chunks(self):
        class _Parts:
            def __init__(self) -> None:
                self._parts = ["hel\r", "\nlo\n"]

            def read(self, _n: int) -> str:
                if not self._parts:
                    return ""
                return self._parts.pop(0)

            def close(self) -> None:
                return None

        ap = AsyncProcess(["dummy"])
        received = []
        ap._read_stream(_Parts(), received.append)
        assert received == ["hel", "lo"]


def test_stderr_drain_short_read_before_eof():
    """A buffered pipe must surface a short burst while the child is still alive.

    BufferedReader.read(4096) waits for 4096 bytes or EOF, so this used to
    stay empty for the whole sleep.
    """
    script = (
        "import sys, time\n"
        "sys.stderr.buffer.write(b'hello-stderr-tail')\n"
        "sys.stderr.buffer.flush()\n"
        "time.sleep(30)\n"
    )
    cases = (
        {"stderr": subprocess.PIPE},
        {"stderr": subprocess.PIPE, "text": True, "bufsize": 1},
    )
    for popen_kwargs in cases:
        proc = subprocess.Popen([sys.executable, "-c", script], **popen_kwargs)
        drain = None
        try:
            drain = start_stderr_drain(proc.stderr, name="test-stderr-short")
            assert drain is not None
            deadline = time.time() + 2.0
            while time.time() < deadline and "hello-stderr-tail" not in drain.text():
                time.sleep(0.02)
            assert "hello-stderr-tail" in drain.text()
            assert proc.poll() is None
        finally:
            proc.kill()
            proc.wait(timeout=5)
            if drain is not None:
                drain.join(timeout=2)


def test_stderr_drain_joins_split_utf8():
    """A character split across two short reads must not become two replacement chars."""

    class _Chunks:
        def __init__(self) -> None:
            # U+00E9 (é) as two UTF-8 bytes delivered separately, then EOF.
            self._chunks = [b"\xc3", b"\xa9", b""]

        def read1(self, _n: int) -> bytes:
            if not self._chunks:
                return b""
            return self._chunks.pop(0)

        def close(self) -> None:
            return None

    drain = start_stderr_drain(_Chunks(), name="test-utf8-split")
    assert drain is not None
    drain.join(timeout=2)
    assert drain.text() == "é"

    received: list[str] = []
    AsyncProcess(["dummy"])._read_stream(_Chunks(), received.append)
    assert received == ["é"]


def test_shutdown_joins_worker_past_one_slice(monkeypatch: pytest.MonkeyPatch):
    """One 5s join used to return while the in-flight job was still running."""
    monkeypatch.setattr("plugin.framework.worker_pool._POOL_SHUTDOWN_JOIN_SLICE_SEC", 0.05)
    pool = _DaemonWorkPool(1)
    started = threading.Event()
    release = threading.Event()

    def job() -> None:
        started.set()
        release.wait(2.0)

    pool.submit(job)
    assert started.wait(1.0)
    releaser = threading.Thread(target=lambda: (time.sleep(0.2), release.set()), daemon=True)
    releaser.start()
    try:
        pool.shutdown(wait=True, cancel_futures=True)
        # Before release.set() in finally: a single short join would still
        # see the worker blocked in release.wait.
        assert all(not thread.is_alive() for thread in pool._threads)
    finally:
        release.set()
        releaser.join(timeout=1.0)


def test_concurrent_submit_during_shutdown_does_not_hang():
    pool = _DaemonWorkPool(1)
    errors: list[str] = []
    stop = threading.Event()

    def hammer() -> None:
        while not stop.is_set():
            try:
                fut = pool.submit(lambda: None)
            except RuntimeError:
                return
            handle = BackgroundHandle(future=fut)
            handle.join(timeout=2)
            if handle.is_alive():
                errors.append("hung")
                return

    worker = threading.Thread(target=hammer)
    worker.start()
    time.sleep(0.05)
    try:
        pool.shutdown(wait=True, cancel_futures=True)
    finally:
        stop.set()
        worker.join(timeout=3)
    assert not worker.is_alive()
    assert errors == []


def test_async_process_replaces_undecodable_stderr():
    stderr_lines: list[str] = []
    script = "import sys; sys.stderr.buffer.write(bytes([0xFF])); sys.stderr.buffer.write(b'\\n'); sys.stderr.buffer.flush()"
    ap = AsyncProcess([sys.executable, "-c", script], stderr_cb=stderr_lines.append)
    ap.start()
    assert ap._wait_thread is not None
    ap._wait_thread.join(timeout=2)
    if ap._stderr_thread:
        ap._stderr_thread.join(timeout=1)
    assert any("\ufffd" in line for line in stderr_lines)


def test_async_process_does_not_mutate_caller_kwargs():
    kwargs: dict[str, object] = {"close_fds": True}
    AsyncProcess(["dummy"], **kwargs)
    assert kwargs == {"close_fds": True}


def test_async_process_forces_binary_pipes():
    """text=True used to keep encoding while the reader still decoded UTF-8 bytes."""
    ap = AsyncProcess(["dummy"], text=True, encoding="latin-1", errors="strict", universal_newlines=True)
    assert ap._popen_kwargs["text"] is False
    assert "encoding" not in ap._popen_kwargs
    assert "errors" not in ap._popen_kwargs
    assert "universal_newlines" not in ap._popen_kwargs


def test_second_start_wait_does_not_report_new_child(monkeypatch: pytest.MonkeyPatch) -> None:
    """The first wait thread must report the child it was started for.

    _wait_for_exit used to read self.process at wait time. A second start()
    replaced that child, so the first waiter blocked on the new process and
    delivered its exit.
    """
    from collections.abc import Callable

    scheduled: list[tuple[Callable[..., None], tuple[object, ...], str | None]] = []

    def capture(func: Callable[..., None], *args: object, name: str | None = None, **_kwargs: object) -> BackgroundHandle:
        scheduled.append((func, args, name))
        return BackgroundHandle()

    monkeypatch.setattr("plugin.framework.worker_pool.run_in_background", capture)

    exits: list[int] = []
    first = None
    second = None
    ap = AsyncProcess(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout_cb=lambda _line: None,
        on_exit_cb=exits.append,
    )
    try:
        ap.start()
        first = ap.process
        assert first is not None
        first_waits = [row for row in scheduled if row[2] and str(row[2]).startswith("asyncproc-wait")]
        assert len(first_waits) == 1

        ap.start()
        second = ap.process
        assert second is not None and second is not first
        assert second.poll() is None
        assert first.returncode is not None

        func, args, _name = first_waits[0]
        errors: list[BaseException] = []

        def run_wait() -> None:
            try:
                func(*args)
            except BaseException as exc:
                errors.append(exc)

        waiter = threading.Thread(target=run_wait)
        waiter.start()
        waiter.join(1.0)
        blocked = waiter.is_alive()
        if blocked and second.poll() is None:
            second.kill()
            waiter.join(2.0)
        assert not blocked, "first wait thread blocked on the replacement child"
        assert errors == []
        assert second.poll() is None
        assert exits == [first.returncode]
    finally:
        for proc in (first, second, ap.process):
            if proc is None or proc.poll() is not None:
                continue
            proc.kill()
            proc.wait(timeout=5)


def test_is_current_thread_is_false_for_pooled_handle() -> None:
    started = threading.Event()
    release = threading.Event()
    handle_ready = threading.Event()
    seen: list[bool] = []
    box: dict[str, BackgroundHandle] = {}

    def work() -> None:
        assert handle_ready.wait(2)
        seen.append(box["handle"].is_current_thread())
        started.set()
        release.wait(2)

    handle = run_in_background(work, dedicated=True, name="self-check")
    box["handle"] = handle
    handle_ready.set()
    assert started.wait(2)
    assert seen == [True]
    assert handle.is_current_thread() is False
    release.set()
    handle.join(timeout=2)

    from concurrent.futures import Future

    fut: Future[None] = Future()
    assert BackgroundHandle(future=fut).is_current_thread() is False
    fut.cancel()


def test_join_handles_skips_its_own_dedicated_thread() -> None:
    ap = AsyncProcess(["dummy"])
    done = threading.Event()
    ready = threading.Event()
    errors: list[BaseException] = []
    box: dict[str, BackgroundHandle] = {}

    def reader() -> None:
        assert ready.wait(2)
        try:
            ap._join_handles((box["handle"],), timeout=0.2)
        except BaseException as exc:
            errors.append(exc)
        done.set()

    handle = run_in_background(reader, dedicated=True, name="reader-self")
    box["handle"] = handle
    ready.set()
    assert done.wait(2)
    assert errors == []
    handle.join(timeout=1)


def test_join_handles_future_from_pool_thread_still_raises() -> None:
    from concurrent.futures import Future

    ap = AsyncProcess(["dummy"])
    fut: Future[None] = Future()
    handle = BackgroundHandle(future=fut)
    errors: list[BaseException | None] = []

    def run() -> None:
        threading.current_thread().name = "wa-bg-9"
        try:
            ap._join_handles((handle,), timeout=0.2)
        except RuntimeError as exc:
            errors.append(exc)
        else:
            errors.append(None)

    worker = threading.Thread(target=run)
    worker.start()
    worker.join(1)
    fut.cancel()
    assert not worker.is_alive()
    assert errors and isinstance(errors[0], RuntimeError)
    assert "deadlock" in str(errors[0])


def test_join_done_future_from_pool_thread_does_not_raise() -> None:
    """A finished future needs no worker to complete, so join() must not raise deadlock."""
    from concurrent.futures import Future

    fut: Future[str] = Future()
    fut.set_result("finished")
    handle = BackgroundHandle(future=fut)
    errors: list[BaseException | None] = []

    def run() -> None:
        threading.current_thread().name = "wa-bg-9"
        try:
            handle.join(timeout=0.2)
        except Exception as exc:
            errors.append(exc)
        else:
            errors.append(None)

    worker = threading.Thread(target=run)
    worker.start()
    worker.join(1)
    assert not worker.is_alive()
    assert errors == [None]


def test_run_in_background_keeps_submit_time_send_cancellation():
    """A dedicated job keeps the contextvar copied at submit.

    Setting a new scope on the caller after the job has started must not
    change what get_current_send_cancellation returns inside that job.
    """
    from plugin.framework.queue_executor import (
        SendCancellation,
        _current_send_cancellation,
        get_current_send_cancellation,
    )

    old = SendCancellation()
    new = SendCancellation()
    previous = get_current_send_cancellation()
    started = threading.Event()
    release = threading.Event()
    seen = {}

    def job():
        started.set()
        assert release.wait(timeout=2)
        seen["scope"] = get_current_send_cancellation()

    _current_send_cancellation.set(old)
    try:
        handle = run_in_background(job, name="ctx-scope", dedicated=True)
        assert started.wait(timeout=2)
        _current_send_cancellation.set(new)
        release.set()
        handle.join(timeout=2)
    finally:
        _current_send_cancellation.set(previous)

    assert seen["scope"] is old

