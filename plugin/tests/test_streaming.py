# Tests for core/api.py streaming and edge cases.
# Edge-case behavior and test ideas adapted from LiteLLM (BerriAI/litellm);
# see inline comments and core/api.py LiteLLM references for source locations.
import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Import; tests patch core.logging and init_logging per test
from plugin.modules.http.client import LlmClient, _normalize_message_content


def _make_sse_lines(*chunks, done=True):
    """Build SSE byte lines from chunk dicts. Used to mock response stream."""
    lines = []
    for c in chunks:
        lines.append(b"data: " + json.dumps(c).encode() + b"\n")
    if done:
        lines.append(b"data: [DONE]\n")
    return lines


def _make_chat_chunk(content="", delta=None, finish_reason=None):
    """One choices[0] chunk for chat completions."""
    d = delta if delta is not None else {}
    if content:
        d = dict(d)
        d["content"] = content
    choice = {"delta": d}
    if finish_reason is not None:
        choice["finish_reason"] = finish_reason
    return {"choices": [choice]}


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


class TestStreamingBasic(unittest.TestCase):
    """Basic streaming behavior (LiteLLM-equivalent: SSE parsing, [DONE], comments)."""

    def setUp(self):
        self.ctx = MagicMock()
        self.config = {
            "endpoint": "http://127.0.0.1:5000",
            "model": "test",
            "request_timeout": 60,
        }

    @patch("plugin.modules.http.client.init_logging")
    def test_basic_streaming(self, mock_init_logging):
        """Happy path: two content chunks then [DONE]."""
        chunks = [
            _make_chat_chunk(content="Hello"),
            _make_chat_chunk(content=" world"),
        ]
        lines = _make_sse_lines(*chunks)
        client = LlmClient(self.config, self.ctx)
        client._get_connection = lambda: _mock_connection_with_sse_lines(lines)

        content_parts = []

        client.stream_request(
            "POST", "/v1/chat/completions", b"{}", {},
            content_parts.append,
        )
        self.assertEqual(content_parts, ["Hello", " world"])

    @patch("plugin.modules.http.client.init_logging")
    def test_sse_no_space_after_colon(self, mock_init_logging):
        """SSE line 'data:{...}' without space after colon is still parsed.
        LiteLLM: streaming_handler.py ~L1280 _strip_sse_data_from_chunk (Sagemaker format).
        """
        chunk = _make_chat_chunk(content="ok")
        # No space after "data:"
        lines = [b"data:" + json.dumps(chunk).encode() + b"\n", b"data: [DONE]\n"]
        client = LlmClient(self.config, self.ctx)
        client._get_connection = lambda: _mock_connection_with_sse_lines(lines)

        content_parts = []
        client.stream_request(
            "POST", "/v1/chat/completions", b"{}", {},
            content_parts.append,
        )
        self.assertEqual(content_parts, ["ok"])

    @patch("plugin.modules.http.client.init_logging")
    def test_sse_comment_lines_skipped(self, mock_init_logging):
        """Lines starting with ':' are skipped (OpenRouter heartbeats)."""
        chunks = [
            _make_chat_chunk(content="x"),
        ]
        lines = [b": OPENROUTER PROCESSING\n"]
        lines.extend(_make_sse_lines(*chunks))
        client = LlmClient(self.config, self.ctx)
        client._get_connection = lambda: _mock_connection_with_sse_lines(lines)

        content_parts = []
        client.stream_request(
            "POST", "/v1/chat/completions", b"{}", {},
            content_parts.append,
        )
        self.assertEqual(content_parts, ["x"])

    @patch("plugin.modules.http.client.init_logging")
    def test_malformed_json_skipped(self, mock_init_logging):
        """Garbled payload is skipped; subsequent chunks are processed."""
        lines = [
            b"data: not valid json\n",
            *_make_sse_lines(_make_chat_chunk(content="fine")),
        ]
        client = LlmClient(self.config, self.ctx)
        client._get_connection = lambda: _mock_connection_with_sse_lines(lines)

        content_parts = []
        client.stream_request(
            "POST", "/v1/chat/completions", b"{}", {},
            content_parts.append,
        )
        self.assertEqual(content_parts, ["fine"])


class TestStreamingFinishReasonError(unittest.TestCase):
    """finish_reason='error' should raise. LiteLLM: streaming_handler.py ~L736."""

    def setUp(self):
        self.ctx = MagicMock()
        self.config = {"endpoint": "http://127.0.0.1:5000", "model": "test", "request_timeout": 60}

    @patch("plugin.modules.http.client.init_logging")
    def test_finish_reason_error_raises(self, mock_init_logging):
        """Chunk with finish_reason='error' raises Exception."""
        chunk = _make_chat_chunk(content="", finish_reason="error")
        lines = _make_sse_lines(chunk)
        client = LlmClient(self.config, self.ctx)
        client._get_connection = lambda: _mock_connection_with_sse_lines(lines)

        with self.assertRaises(Exception) as ctx:
            client.stream_request(
                "POST", "/v1/chat/completions", b"{}", {},
                lambda t: None,
            )
        # API re-raises with format_error_message(); user sees friendly text
        self.assertIn("finish_reason=error", str(ctx.exception))


class TestStreamingRepeatedChunks(unittest.TestCase):
    """Repeated identical content chunks raise (infinite loop). LiteLLM: streaming_handler.py ~L198, issue #5158."""

    def setUp(self):
        self.ctx = MagicMock()
        self.config = {"endpoint": "http://127.0.0.1:5000", "model": "test", "request_timeout": 60}

    @patch("plugin.modules.http.client.init_logging")
    def test_repeated_chunks_raises(self, mock_init_logging):
        """Many identical content chunks raise Exception."""
        # Default REPEATED_STREAMING_CHUNK_LIMIT in api.py is 20
        chunks = [_make_chat_chunk(content="repeat") for _ in range(21)]
        lines = _make_sse_lines(*chunks)
        client = LlmClient(self.config, self.ctx)
        client._get_connection = lambda: _mock_connection_with_sse_lines(lines)

        with self.assertRaises(Exception) as ctx:
            client.stream_request(
                "POST", "/v1/chat/completions", b"{}", {},
                lambda t: None,
            )
        self.assertIn("repeating", str(ctx.exception).lower())


class TestNormalizeDelta(unittest.TestCase):
    """Mistral/Azure compat: None role, None tool type, None function.arguments.
    LiteLLM: streaming_handler.py ~L847 (role), ~L853 (type), ~L820 (arguments).
    """

    def setUp(self):
        self.ctx = MagicMock()
        self.config = {"endpoint": "http://127.0.0.1:5000", "model": "test", "request_timeout": 60}

    @patch("plugin.modules.http.client.init_logging")
    def test_tool_none_arguments_normalized(self, mock_init_logging):
        """Streamed tool_call with function.arguments=None becomes '' after accumulate_delta + normalize."""
        # One chunk with tool_calls and arguments None (Azure); after normalize, accumulate_delta gets ""
        chunks = [
            {
                "choices": [{
                    "delta": {
                        "role": "assistant",
                        "tool_calls": [
                            {"index": 0, "id": "call_1", "type": "function", "function": {"name": "foo", "arguments": None}},
                        ],
                    },
                }],
            },
            _make_chat_chunk(content="", finish_reason="stop"),
        ]
        lines = _make_sse_lines(*chunks)
        client = LlmClient(self.config, self.ctx)
        client._get_connection = lambda: _mock_connection_with_sse_lines(lines)

        result = client.stream_request_with_tools(
            [{"role": "user", "content": "hi"}],
            max_tokens=100,
            tools=[{"type": "function", "function": {"name": "foo", "description": "x"}}],
        )
        self.assertIsNotNone(result.get("tool_calls"))
        self.assertEqual(len(result["tool_calls"]), 1)
        fn = result["tool_calls"][0].get("function") or {}
        # Normalized from None to ""
        self.assertIn("arguments", fn)
        self.assertEqual(fn["arguments"], "")

    @patch("plugin.modules.http.client.init_logging")
    def test_tool_none_type_normalized(self, mock_init_logging):
        """Streamed tool_call with type=None becomes 'function' (Mistral)."""
        chunks = [
            {
                "choices": [{
                    "delta": {
                        "role": "assistant",
                        "tool_calls": [
                            {"index": 0, "id": "call_1", "type": None, "function": {"name": "bar", "arguments": ""}},
                        ],
                    },
                }],
            },
            _make_chat_chunk(content="", finish_reason="stop"),
        ]
        lines = _make_sse_lines(*chunks)
        client = LlmClient(self.config, self.ctx)
        client._get_connection = lambda: _mock_connection_with_sse_lines(lines)

        result = client.stream_request_with_tools(
            [{"role": "user", "content": "hi"}],
            max_tokens=100,
            tools=[{"type": "function", "function": {"name": "bar", "description": "y"}}],
        )
        self.assertIsNotNone(result.get("tool_calls"))
        self.assertEqual(len(result["tool_calls"]), 1)
        self.assertEqual(result["tool_calls"][0].get("type"), "function")

    @patch("plugin.modules.http.client.init_logging")
    def test_role_none_normalized(self, mock_init_logging):
        """Delta with role=None is normalized to 'assistant' (Mistral)."""
        chunks = [
            {
                "choices": [{
                    "delta": {"role": None, "content": "ok"},
                }],
            },
            _make_chat_chunk(content="", finish_reason="stop"),
        ]
        lines = _make_sse_lines(*chunks)
        client = LlmClient(self.config, self.ctx)
        client._get_connection = lambda: _mock_connection_with_sse_lines(lines)

        result = client.stream_request_with_tools(
            [{"role": "user", "content": "hi"}],
            max_tokens=100,
        )
        # Delta had role=None; _normalize_delta sets role='assistant' before accumulate_delta.
        self.assertEqual(_normalize_message_content(result.get("content")), "ok")


class TestFinishReasonRemap(unittest.TestCase):
    """finish_reason='stop' with tool_calls present -> 'tool_calls'. LiteLLM: streaming_handler.py ~L970."""

    def setUp(self):
        self.ctx = MagicMock()
        self.config = {"endpoint": "http://127.0.0.1:5000", "model": "test", "request_timeout": 60}

    @patch("plugin.modules.http.client.init_logging")
    def test_finish_reason_stop_with_tool_calls_remapped(self, mock_init_logging):
        """When finish_reason is 'stop' but tool_calls exist, result has finish_reason='tool_calls'."""
        chunks = [
            {
                "choices": [{
                    "delta": {
                        "role": "assistant",
                        "tool_calls": [
                            {"index": 0, "id": "c1", "type": "function", "function": {"name": "f", "arguments": ""}},
                        ],
                    },
                }],
            },
            _make_chat_chunk(content="", finish_reason="stop"),
        ]
        lines = _make_sse_lines(*chunks)
        client = LlmClient(self.config, self.ctx)
        client._get_connection = lambda: _mock_connection_with_sse_lines(lines)

        result = client.stream_request_with_tools(
            [{"role": "user", "content": "hi"}],
            max_tokens=100,
            tools=[{"type": "function", "function": {"name": "f", "description": "d"}}],
        )
        self.assertEqual(result["finish_reason"], "tool_calls")
        self.assertIsNotNone(result.get("tool_calls"))


class TestStreamingComplexDeltas(unittest.TestCase):
    """Mixed content and tool_calls, and chunking irregularities."""

    def setUp(self):
        self.ctx = MagicMock()
        self.config = {"endpoint": "http://127.0.0.1:5000", "model": "test", "request_timeout": 60}

    @patch("plugin.modules.http.client.init_logging")
    def test_mixed_content_and_tool_calls_ordering(self, mock_init_logging):
        """Provider emits partial content before tool_calls; ensure both are accumulated correctly."""
        chunks = [
            {
                "choices": [{
                    "delta": {"role": "assistant", "content": "I will use "},
                }],
            },
            {
                "choices": [{
                    "delta": {"content": "the tool now."},
                }],
            },
            {
                "choices": [{
                    "delta": {
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
                            {"index": 0, "function": {"arguments": "arg\": \"val\"}"}},
                        ],
                    },
                }],
            },
            _make_chat_chunk(content="", finish_reason="tool_calls"),
        ]
        lines = _make_sse_lines(*chunks)
        client = LlmClient(self.config, self.ctx)
        client._get_connection = lambda: _mock_connection_with_sse_lines(lines)

        result = client.stream_request_with_tools(
            [{"role": "user", "content": "do something"}],
            max_tokens=100,
            tools=[{"type": "function", "function": {"name": "foo", "description": "x"}}],
        )

        self.assertEqual(result["content"], "I will use the tool now.")
        self.assertEqual(result["finish_reason"], "tool_calls")
        self.assertIsNotNone(result.get("tool_calls"))
        self.assertEqual(len(result["tool_calls"]), 1)
        self.assertEqual(result["tool_calls"][0]["function"]["name"], "foo")
        self.assertEqual(result["tool_calls"][0]["function"]["arguments"], '{"arg": "val"}')

    @patch("plugin.modules.http.client.init_logging")
    def test_tool_call_arguments_split_across_chunks(self, mock_init_logging):
        """Tool-call argument strings split mid-JSON across multiple SSE chunks concatenate correctly."""
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
                            {"index": 0, "function": {"arguments": "arg\": \"val\"}"}},
                        ],
                    },
                }],
            },
            _make_chat_chunk(content="", finish_reason="tool_calls"),
        ]
        lines = _make_sse_lines(*chunks)
        client = LlmClient(self.config, self.ctx)
        client._get_connection = lambda: _mock_connection_with_sse_lines(lines)

        result = client.stream_request_with_tools(
            [{"role": "user", "content": "do something"}],
            max_tokens=100,
            tools=[{"type": "function", "function": {"name": "foo", "description": "x"}}],
        )

        self.assertEqual(result["finish_reason"], "tool_calls")
        self.assertIsNotNone(result.get("tool_calls"))
        self.assertEqual(len(result["tool_calls"]), 1)
        self.assertEqual(result["tool_calls"][0]["function"]["name"], "foo")
        self.assertEqual(result["tool_calls"][0]["function"]["arguments"], '{"arg": "val"}')


def _mock_error_connection(status, reason, body):
    """Return a mock HTTPConnection that simulates a non-200 response with a body."""
    conn = MagicMock()
    response = MagicMock()
    response.status = status
    response.reason = reason
    response.getheader.return_value = None
    response.read.return_value = body
    conn.getresponse.return_value = response
    return conn


class TestStreamingHttpErrors(unittest.TestCase):
    """Test non-200 HTTP error envelope parsing."""

    def setUp(self):
        self.ctx = MagicMock()
        self.config = {"endpoint": "http://127.0.0.1:5000", "model": "test", "request_timeout": 60}

    @patch("plugin.modules.http.client.init_logging")
    def test_http_error_envelope_parsing(self, mock_init_logging):
        """Confirm HTTP error bodies are converted into friendly UI exception messages."""
        client = LlmClient(self.config, self.ctx)

        # Scenario 1: JSON body with detailed error
        json_error_body = b'{"error": {"message": "Model not found"}}'
        client._get_connection = lambda: _mock_error_connection(404, "Not Found", json_error_body)

        with self.assertRaises(Exception) as ctx:
            client.stream_request(
                "POST", "/v1/chat/completions", b"{}", {},
                lambda t: None,
            )
        # `format_error_message` operates on the string for generic Exception,
        # meaning it outputs what `_format_http_error_response` built, which is
        # "HTTP Error <status>: <reason>. <detail>"
        self.assertEqual(str(ctx.exception), "HTTP Error 404: Not Found. Model not found")

        # Scenario 2: Non-JSON body (e.g., HTML or plain text)
        plain_error_body = b"Internal Server Error: Database Down"
        client._get_connection = lambda: _mock_error_connection(500, "Internal Server Error", plain_error_body)

        with self.assertRaises(Exception) as ctx:
            client.stream_request(
                "POST", "/v1/chat/completions", b"{}", {},
                lambda t: None,
            )
        self.assertEqual(str(ctx.exception), "HTTP Error 500: Internal Server Error. Internal Server Error: Database Down")


if __name__ == "__main__":
    unittest.main()
