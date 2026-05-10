import pytest
import time
import subprocess
import sys
import threading
from unittest.mock import MagicMock, patch

from plugin.framework.worker_pool import run_in_background, AsyncProcess
from plugin.framework.errors import WorkerPoolError, ToolExecutionError

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
    assert ap._popen_kwargs["text"] is True
    assert ap._popen_kwargs["bufsize"] == 1
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

    # Test ValueError
    mock_stream = MagicMock()
    mock_stream.__iter__.side_effect = ValueError("I/O operation on closed file")

    ap._read_stream(mock_stream, lambda x: None)
    mock_stream.close.assert_called()

    # Test OSError
    mock_stream = MagicMock()
    mock_stream.__iter__.side_effect = OSError("read error")

    ap._read_stream(mock_stream, lambda x: None)
    mock_stream.close.assert_called()

    # Test stream close throwing OSError
    mock_stream = MagicMock()
    mock_stream.__iter__.return_value = ["line1"]
    mock_stream.close.side_effect = OSError("close error")

    ap._read_stream(mock_stream, lambda x: None)
    mock_stream.close.assert_called()

def test_async_process_drain_stream_errors():
    ap = AsyncProcess(["ls"])

    # Test OSError in loop
    mock_stream = MagicMock()
    mock_stream.__iter__.side_effect = OSError("drain error")

    ap._drain_stream(mock_stream)
    mock_stream.close.assert_called()

    # Test stream close throwing OSError
    mock_stream = MagicMock()
    mock_stream.__iter__.return_value = ["line1"]
    mock_stream.close.side_effect = OSError("close error")

    ap._drain_stream(mock_stream)
    mock_stream.close.assert_called()

def test_async_process_terminate_not_running():
    ap = AsyncProcess(["ls"])
    # Not started, terminate should return silently
    ap.terminate()

    # Started but already exited
    ap.start()
    ap._wait_thread.join(timeout=2)
    ap.terminate()
