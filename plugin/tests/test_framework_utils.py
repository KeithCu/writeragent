import queue
import time
from unittest.mock import MagicMock, patch
import pytest

from plugin.framework.worker_pool import run_in_background
from plugin.framework.errors import WorkerPoolError
from plugin.framework.async_stream import run_stream_drain_loop
from plugin.framework.logging import SafeLogger, safe_log_exception

class TestWorkerPoolErrorHandling:
    def test_run_in_background_success(self):
        def mock_task(x, y):
            return x + y

        thread = run_in_background(mock_task, 2, 3)
        thread.join()

        assert not thread.is_alive()

    def test_run_in_background_failure(self):
        error_cb = MagicMock()

        def mock_task():
            raise ValueError("Test error")

        thread = run_in_background(mock_task, error_callback=error_cb)
        thread.join()

        assert error_cb.call_count == 1
        wrapped_error = error_cb.call_args[0][0]
        assert isinstance(wrapped_error, WorkerPoolError)
        assert "Task 'mock_task' failed" in wrapped_error.message
        assert wrapped_error.code == "WORKER_TASK_FAILED"
        assert wrapped_error.details['error_type'] == "ValueError"


class TestAsyncStreamErrorHandling:
    def test_stream_drain_loop_success(self):
        q = queue.Queue()
        toolkit = MagicMock()
        job_done = [False]

        on_chunk = MagicMock()
        on_error = MagicMock()
        on_stream_done = MagicMock()
        on_stopped = MagicMock()

        q.put(("chunk", "hello "))
        q.put(("thinking", "thinking..."))
        q.put(("stream_done", "final"))

        run_stream_drain_loop(
            q, toolkit, job_done, on_chunk, on_stream_done, on_stopped, on_error
        )

        assert job_done[0] is True
        on_chunk.assert_any_call("hello ", is_thinking=False)
        on_chunk.assert_any_call("thinking...", is_thinking=True)
        # on_stream_done is called with the entire item tuple ("stream_done", "final")
        on_stream_done.assert_called_once_with(("stream_done", "final"))
        on_error.assert_not_called()

    def test_stream_drain_loop_processing_error(self):
        q = queue.Queue()
        toolkit = MagicMock()
        job_done = [False]

        on_error = MagicMock()

        # Make on_chunk raise an error
        def faulty_on_chunk(data, is_thinking):
            raise ValueError("Processing failed")

        q.put(("chunk", "bad data"))

        run_stream_drain_loop(
            q, toolkit, job_done, faulty_on_chunk, MagicMock(), MagicMock(), on_error
        )

        # The loop should catch the processing error, put an error back in the queue,
        # and eventually call on_error with the formatted payload.
        assert job_done[0] is True
        assert on_error.call_count == 1
        error_payload = on_error.call_args[0][0]
        assert error_payload["status"] == "error"
        assert "Processing failed" in error_payload["message"]

class TestLoggingErrorHandling:
    def test_safe_logger_success(self):
        mock_underlying = MagicMock()
        logger = SafeLogger(mock_underlying)

        logger.error("Test error", exc_info=True)
        logger.warning("Test warning")

        mock_underlying.error.assert_called_once_with("Test error", exc_info=True)
        mock_underlying.warning.assert_called_once_with("Test warning")

    def test_safe_logger_fallback(self, capsys):
        mock_underlying = MagicMock()
        mock_underlying.error.side_effect = Exception("Logger crashed")
        mock_underlying.warning.side_effect = Exception("Logger crashed")

        logger = SafeLogger(mock_underlying)

        logger.error("Should fallback")
        logger.warning("Should fallback warning")

        captured = capsys.readouterr()
        assert "LOG ERROR FAILED: Should fallback" in captured.out
        assert "LOG WARNING FAILED: Should fallback warning" in captured.out

    def test_safe_logger_disable_fallback(self, capsys):
        mock_underlying = MagicMock()
        mock_underlying.error.side_effect = Exception("Logger crashed")

        logger = SafeLogger(mock_underlying)
        logger.disable_fallback()

        logger.error("Should be silent")

        captured = capsys.readouterr()
        assert "LOG ERROR FAILED" not in captured.out

    def test_safe_log_exception_success(self):
        mock_logger = MagicMock()

        try:
            1 / 0
        except Exception as e:
            safe_log_exception(e, context="test_ctx", logger=mock_logger)

        mock_logger.error.assert_called_once()
        args, kwargs = mock_logger.error.call_args
        assert "division by zero" in args[0]
        assert kwargs["extra"]["error_details"]["context"] == "test_ctx"

    def test_safe_log_exception_fallback(self, capsys):
        mock_logger = MagicMock()
        mock_logger.error.side_effect = Exception("Logger crashed")

        try:
            1 / 0
        except Exception as e:
            safe_log_exception(e, context="test_ctx", logger=mock_logger)

        captured = capsys.readouterr()
        assert "CRITICAL: Logging failed for exception" in captured.out

    def test_safe_log_exception_final_fallback(self, capsys):
        # Pass a completely broken object as logger to force the outer except
        class BrokenLogger:
            @property
            def error(self):
                raise Exception("Fatal logger error")

        broken_logger = BrokenLogger()

        try:
            1 / 0
        except Exception as e:
            safe_log_exception(e, logger=broken_logger)

        captured = capsys.readouterr()
        assert "CRITICAL: Logging failed for exception" in captured.out
