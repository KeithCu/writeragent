import json
import unittest
from unittest.mock import MagicMock, patch

from plugin.modules.http.client import LlmClient, _normalize_message_content
from plugin.framework.streaming_deltas import accumulate_delta

def _make_sse_lines(*chunks, done=True):
    """Build SSE byte lines from chunk dicts. Used to mock response stream."""
    lines = []
    for c in chunks:
        if isinstance(c, bytes):
            lines.append(c)
        else:
            lines.append(b"data: " + json.dumps(c).encode() + b"\n")
    if done:
        lines.append(b"data: [DONE]\n")
    return lines

def _mock_connection_with_sse_lines(sse_lines):
    """Return a mock HTTPConnection that getresponse() yields sse_lines when iterated."""
    conn = MagicMock()
    response = MagicMock()
    response.status = 200
    response.reason = "OK"
    response.getheader.return_value = None
    response.read.return_value = b""
    response.__iter__ = lambda self: iter(sse_lines)
    conn.getresponse.return_value = response
    return conn

class TestStreamingFuzz(unittest.TestCase):
    def setUp(self):
        self.ctx = MagicMock()
        self.config = {
            "endpoint": "http://127.0.0.1:5000",
            "model": "test",
            "request_timeout": 60,
        }

    @patch("plugin.modules.http.client.debug_log")
    @patch("plugin.modules.http.client.init_logging")
    def test_malformed_json_sse_payload(self, mock_init_logging, mock_debug_log):
        """Ensure that garbled JSON lines in the SSE stream are gracefully skipped."""
        lines = [
            b"data: {\"choices\": [{\"delta\": {\"content\": \"hello \"}}]}\n",
            b"data: {GARBAGE JSON[[[}\n",
            b"data: {\"choices\": [{\"delta\": {\"content\": \"world\"}}]}\n",
        ]
        lines = _make_sse_lines(*lines, done=True)

        client = LlmClient(self.config, self.ctx)
        client._get_connection = lambda: _mock_connection_with_sse_lines(lines)

        content_parts = []
        client.stream_request(
            "POST", "/v1/chat/completions", b"{}", {},
            content_parts.append,
        )
        # The garbled chunk should be skipped, and the rest parsed.
        self.assertEqual(content_parts, ["hello ", "world"])

    @patch("plugin.modules.http.client.debug_log")
    @patch("plugin.modules.http.client.init_logging")
    def test_truncated_tool_call_arguments(self, mock_init_logging, mock_debug_log):
        """Ensure that truncated tool call arguments (due to AI stop mid-stream) are returned as a truncated JSON string."""
        chunks = [
            {
                "choices": [{
                    "delta": {
                        "role": "assistant",
                        "tool_calls": [
                            {"index": 0, "id": "call_1", "type": "function", "function": {"name": "foo", "arguments": "{\""}},
                        ],
                    },
                }],
            },
            {
                "choices": [{
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "function": {"arguments": "arg1\": "}},
                        ],
                    },
                }],
            },
            # AI abruptly stops here
            {
                "choices": [{
                    "delta": {},
                    "finish_reason": "length"
                }],
            }
        ]

        lines = _make_sse_lines(*chunks, done=True)

        client = LlmClient(self.config, self.ctx)
        client._get_connection = lambda: _mock_connection_with_sse_lines(lines)

        result = client.stream_request_with_tools(
            [{"role": "user", "content": "hi"}],
            max_tokens=100,
            tools=[{"type": "function", "function": {"name": "foo", "description": "x"}}],
        )

        self.assertEqual(result["finish_reason"], "length")
        self.assertIsNotNone(result.get("tool_calls"))
        self.assertEqual(len(result["tool_calls"]), 1)

        fn = result["tool_calls"][0].get("function") or {}
        # The string should be correctly concatenated, even if it's invalid JSON
        self.assertEqual(fn["arguments"], "{\"arg1\": ")

        # Mocking the UI thread's parsing fallback logic in panel.py
        func_args_str = fn["arguments"]
        try:
            func_args = json.loads(func_args_str)
        except (json.JSONDecodeError, TypeError):
            try:
                import ast
                func_args = ast.literal_eval(func_args_str)
                if not isinstance(func_args, dict):
                    func_args = {}
            except Exception:
                func_args = {}

        # Proves it recovers gracefully to an empty dict
        self.assertEqual(func_args, {})

    @patch("plugin.modules.http.client.debug_log")
    @patch("plugin.modules.http.client.init_logging")
    def test_unexpected_schema_structures(self, mock_init_logging, mock_debug_log):
        """Ensure feeding structurally invalid delta dictionaries raises a clean Exception that doesn't cause a fatal error but gets caught."""
        chunks = [
            {
                "choices": [{
                    "delta": {
                        "role": "assistant",
                        "tool_calls": [
                            {"index": 0, "id": "call_1", "type": "function", "function": {"name": "foo", "arguments": ""}},
                        ],
                    },
                }],
            },
            {
                "choices": [{
                    "delta": {
                        "tool_calls": [
                            # Intentionally malformed: 'index' is a string instead of an int
                            {"index": "INVALID_INDEX", "function": {"arguments": "{}"}},
                        ],
                    },
                }],
            }
        ]

        lines = _make_sse_lines(*chunks, done=True)

        client = LlmClient(self.config, self.ctx)
        client._get_connection = lambda: _mock_connection_with_sse_lines(lines)

        with self.assertRaises(Exception) as ctx:
            client.stream_request_with_tools(
                [{"role": "user", "content": "hi"}],
                max_tokens=100,
                tools=[{"type": "function", "function": {"name": "foo", "description": "x"}}],
            )

        # The error is formatted by format_error_message, verify it caught the TypeError from accumulate_delta
        self.assertTrue("Unexpected, list delta entry `index` value is not an integer" in str(ctx.exception))

    @patch("plugin.modules.http.client.debug_log")
    @patch("plugin.modules.http.client.init_logging")
    def test_ui_thread_graceful_recovery(self, mock_init_logging, mock_debug_log):
        """Simulate an error in the worker thread to ensure run_stream_drain_loop correctly drains it without breaking processEventsToIdle() loop."""
        from plugin.framework.async_stream import run_stream_drain_loop
        import queue
        import time

        class DummyToolkit:
            def processEventsToIdle(self):
                pass

        q = queue.Queue()
        toolkit = DummyToolkit()
        job_done = [False]
        errors = []

        def apply_chunk(t, is_thinking):
            pass

        def on_stream_done(i):
            return True

        def on_error(e):
            errors.append(e)

        def noop():
            pass

        # Put items in the queue mimicking a thread exception that was put on the queue
        q.put(("chunk", "hello"))
        q.put(("chunk", "world"))
        q.put(("error", ValueError("worker thread crashed due to JSON error")))

        # Test loop recovers
        run_stream_drain_loop(
            q, toolkit, job_done, apply_chunk,
            on_stream_done=on_stream_done, on_stopped=noop, on_error=on_error
        )

        self.assertTrue(job_done[0])
        self.assertEqual(len(errors), 1)
        self.assertTrue(isinstance(errors[0], ValueError))

if __name__ == "__main__":
    unittest.main()
