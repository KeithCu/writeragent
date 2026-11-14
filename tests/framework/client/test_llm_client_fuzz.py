import pytest
import json
from unittest.mock import MagicMock

from plugin.framework.client.llm_client import LlmClient

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

class TestStreamingFuzz:
    def setup_method(self):
        self.ctx = MagicMock()
        self.config = {
            "endpoint": "http://127.0.0.1:5000",
            "model": "test",
            "request_timeout": 60,
        }

    def test_truncated_tool_call_arguments(self):
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
        client._transport.get_connection = lambda: _mock_connection_with_sse_lines(lines)

        result = client.stream_request_with_tools(
            [{"role": "user", "content": "hi"}],
            max_tokens=100,
            tools=[{"type": "function", "function": {"name": "foo", "description": "x"}}],
        )

        assert result["finish_reason"] == "length"
        assert result.get("tool_calls") is not None
        assert len(result["tool_calls"]) == 1

        fn = result["tool_calls"][0].get("function") or {}
        # The string should be correctly concatenated, even if it's invalid JSON
        assert fn["arguments"] == "{\"arg1\": "

    def test_unexpected_schema_structures(self):
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
        client._transport.get_connection = lambda: _mock_connection_with_sse_lines(lines)

        with pytest.raises(Exception) as ctx:
            client.stream_request_with_tools(
                [{"role": "user", "content": "hi"}],
                max_tokens=100,
                tools=[{"type": "function", "function": {"name": "foo", "description": "x"}}],
            )

        # The error is formatted by format_error_message, verify it caught the TypeError from accumulate_delta
        assert "Unexpected, list delta entry `index` value is not an integer" in str(ctx.value)


