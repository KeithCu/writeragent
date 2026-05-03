import socket
import json
from unittest.mock import MagicMock, patch

import pytest

from plugin.modules.http.client import LlmClient, strip_leaked_chat_template_control_tokens
from plugin.tests.testing_utils import MockContext


@pytest.fixture
def mock_ctx():
    return MockContext()


@pytest.fixture
def default_config():
    return {
        "endpoint": "https://api.openai.com",
        "api_key": "sk-test-key",
        "model": "gpt-4o",
        "temperature": 0.7,
        "request_timeout": 60,
    }


@pytest.fixture
def client(default_config, mock_ctx):
    return LlmClient(default_config, mock_ctx)


def test_headers_and_config_injection(client):
    headers = client._headers()
    assert headers["Authorization"] == "Bearer sk-test-key"
    assert "HTTP-Referer" in headers
    assert "X-Title" in headers
    assert headers["Content-Type"] == "application/json"

    assert client._endpoint() == "https://api.openai.com"
    assert client._api_path() == "/v1"

    # Test fallback OpenWebUI path
    client.config["is_openwebui"] = True
    assert client._api_path() == "/api"


def test_custom_endpoint_and_key():
    config = {
        "endpoint": "http://localhost:11434",
        "api_key": "ollama",
    }
    client = LlmClient(config, MockContext())
    assert client._endpoint() == "http://localhost:11434"
    assert client._headers()["Authorization"] == "Bearer ollama"

    # Empty api_key means no Authorization header
    config_no_key = {
        "endpoint": "http://localhost:11434",
    }
    client_no_key = LlmClient(config_no_key, MockContext())
    assert "Authorization" not in client_no_key._headers()


def test_persistent_connections(client):
    with (
        patch("http.client.HTTPSConnection") as mock_https,
        patch("http.client.HTTPConnection") as mock_http,
        patch("plugin.modules.http.client.get_unverified_ssl_context") as mock_ssl,
    ):
        conn1 = client._get_connection()
        conn2 = client._get_connection()

        assert conn1 is conn2
        mock_https.assert_called_once_with(
            "api.openai.com", 443, context=mock_ssl.return_value, timeout=60
        )

        client._close_connection()
        conn1.close.assert_called_once()
        assert client._persistent_conn is None

        # Re-opening opens a new one
        conn3 = client._get_connection()
        assert mock_https.call_count == 2

        # Test change of endpoint scheme
        client.config["endpoint"] = "http://localhost:11434"
        conn4 = client._get_connection()
        assert conn4 is not conn3
        mock_http.assert_called_once_with("localhost", 11434, timeout=60)


def test_stream_request_with_tools_text_and_tool(client):
    mock_responses = [
        b'data: {"choices": [{"delta": {"role": "assistant", "content": "Let me compute "}}]}\n\n',
        b'data: {"choices": [{"delta": {"content": "that."}}]}\n\n',
        b'data: {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_123", "type": "function", "function": {"name": "get_weather", "arguments": "{\\"loc"}}]}}]}\n\n',
        b'data: {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "ation\\": \\"NYC\\"}"}}]}}]}\n\n',
        b'data: {"choices": [{"finish_reason": "tool_calls", "delta": {}}]}\n\n',
        b"data: [DONE]\n\n",
    ]

    with patch("http.client.HTTPSConnection") as mock_https:
        mock_conn = MagicMock()
        mock_https.return_value = mock_conn

        mock_response = MagicMock()
        mock_response.status = 200
        # Mocking the iterator behavior of the response object
        mock_response.__iter__.return_value = iter(mock_responses)
        mock_conn.getresponse.return_value = mock_response

        messages = [{"role": "user", "content": "What is the weather?"}]
        tools = [{"type": "function", "function": {"name": "get_weather"}}]

        append_callback_args = []

        def append_callback(text):
            append_callback_args.append(text)

        result = client.stream_request_with_tools(
            messages=messages,
            max_tokens=100,
            tools=tools,
            append_callback=append_callback,
        )

        assert append_callback_args == ["Let me compute ", "that."]
        assert result["role"] == "assistant"
        assert result["content"] == "Let me compute that."
        assert len(result["tool_calls"]) == 1
        assert result["tool_calls"][0]["function"]["name"] == "get_weather"
        assert result["tool_calls"][0]["function"]["arguments"] == '{"location": "NYC"}'
        assert result["finish_reason"] == "tool_calls"


def test_stream_request_with_tools_http_error(client):
    with patch("http.client.HTTPSConnection") as mock_https:
        mock_conn = MagicMock()
        mock_https.return_value = mock_conn

        mock_response = MagicMock()
        mock_response.status = 401
        mock_response.reason = "Unauthorized"
        mock_response.read.return_value = b'{"error": {"message": "Invalid API key"}}'
        mock_conn.getresponse.return_value = mock_response

        with pytest.raises(
            Exception, match="HTTP Error 401 from AI Provider: Unauthorized. Invalid API key"
        ):
            client.stream_request_with_tools(
                messages=[{"role": "user", "content": "Hi"}], max_tokens=100
            )


def test_stream_request_with_tools_connection_error(client):
    with patch("http.client.HTTPSConnection") as mock_https:
        mock_conn = MagicMock()
        mock_https.return_value = mock_conn

        # Simulate socket.timeout
        mock_conn.request.side_effect = socket.timeout("timed out")

        with pytest.raises(Exception, match="Request Timed Out"):
            client.stream_request_with_tools(
                messages=[{"role": "user", "content": "Hi"}], max_tokens=100
            )


def test_stream_request_with_tools_fallback_parser(client):
    mock_responses = [
        b'data: {"choices": [{"delta": {"role": "assistant", "content": "I will get the weather."}}]}\n\n',
        b'data: {"choices": [{"delta": {"content": "<tool_call>{\\"name\\": \\"get_weather\\"}</tool_call>"}}]}\n\n',
        b"data: [DONE]\n\n",
    ]

    with (
        patch("http.client.HTTPSConnection") as mock_https,
        patch(
            "plugin.contrib.tool_call_parsers.get_parser_for_model"
        ) as mock_get_parser,
    ):
        mock_conn = MagicMock()
        mock_https.return_value = mock_conn

        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.__iter__.return_value = iter(mock_responses)
        mock_conn.getresponse.return_value = mock_response

        # Mock the tool call parser
        mock_parser = MagicMock()
        # Return stripped content and mocked tool calls
        parsed_tool_calls = [
            {"type": "function", "function": {"name": "get_weather", "arguments": "{}"}}
        ]
        mock_parser.parse.return_value = ("I will get the weather.", parsed_tool_calls)
        mock_get_parser.return_value = mock_parser

        result = client.stream_request_with_tools(
            messages=[{"role": "user", "content": "weather in NYC"}], max_tokens=100
        )

        # Ensure the parser was invoked with the full concatenated string
        mock_parser.parse.assert_called_once_with(
            'I will get the weather.<tool_call>{"name": "get_weather"}</tool_call>'
        )

        # Ensure the fallback output correctly sets tool_calls and updates the finish reason
        assert result["content"] == "I will get the weather."
        assert len(result["tool_calls"]) == 1
        assert result["tool_calls"][0]["function"]["name"] == "get_weather"
        assert result["finish_reason"] == "tool_calls"


def test_make_chat_request_system_content_can_be_list():
    """
    Regression test for: AttributeError: 'list' object has no attribute 'startswith'
    triggered when date-injection logic assumes system message content is a string.
    """
    ctx = MockContext()
    client = LlmClient({"endpoint": "http://test", "model": "test-model"}, ctx)

    structured_system_content = [
        {"type": "text", "text": "Existing structured system content"}
    ]
    messages = [
        {"role": "system", "content": structured_system_content},
        {"role": "user", "content": "Hi"},
    ]

    method, path, body, headers = client.make_chat_request(messages, max_tokens=50)

    assert method == "POST"
    assert path.endswith("/chat/completions")
    assert headers["Content-Type"] == "application/json"

    decoded = json.loads(body.decode("utf-8"))
    assert decoded["messages"][0]["content"] == structured_system_content


def test_stream_request_with_tools_tls_retry():
    import ssl
    ctx = MockContext()
    # Using a local HTTPS endpoint triggers the verified/unverified retry logic
    client = LlmClient({"endpoint": "https://localhost:11434"}, ctx)

    mock_responses = [
        b'data: {"choices": [{"delta": {"role": "assistant", "content": "Success"}}]}\n\n',
        b"data: [DONE]\n\n",
    ]

    with patch("http.client.HTTPSConnection") as mock_https, \
         patch("plugin.modules.http.client.get_unverified_ssl_context") as mock_unverified_ssl:
        mock_unverified_ssl.return_value = "unverified_context"

        # We need two connection objects: one for the first try, one for the retry
        mock_conn1 = MagicMock()
        mock_conn2 = MagicMock()
        mock_https.side_effect = [mock_conn1, mock_conn2]

        # The first request raises an SSLCertVerificationError
        mock_conn1.request.side_effect = ssl.SSLCertVerificationError("self-signed certificate")

        # The second request succeeds and returns a mock response
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.__iter__.return_value = iter(mock_responses)
        mock_conn2.getresponse.return_value = mock_response

        messages = [{"role": "user", "content": "Hello"}]

        result = client.stream_request_with_tools(
            messages=messages,
            max_tokens=100
        )

        assert mock_https.call_count == 2

        # The first connection was created with the default (verified) context
        _, kwargs1 = mock_https.call_args_list[0]
        # The second connection was created with the unverified context
        _, kwargs2 = mock_https.call_args_list[1]
        assert kwargs2["context"] == "unverified_context"

        assert result["content"] == "Success"

def test_stream_request_with_tools_malformed_tool_arguments():
    ctx = MockContext()
    # Explicitly instantiate with an HTTPS endpoint so the HTTPSConnection mock is hit
    client = LlmClient({"endpoint": "https://api.openai.com", "model": "gpt-4"}, ctx)

    # This simulates a provider sending deltas that concatenate to a malformed
    # JSON string (missing closing brace/quote) inside the tool function arguments.
    mock_responses = [
        b'data: {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_123", "type": "function", "function": {"name": "get_weather", "arguments": "{\\"loc"}}]}}]}\n\n',
        b'data: {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "ation\\": \\"NY"}}]}}]}\n\n',
        b'data: {"choices": [{"finish_reason": "tool_calls", "delta": {}}]}\n\n',
        b"data: [DONE]\n\n",
    ]

    with patch("http.client.HTTPSConnection") as mock_https:
        mock_conn = MagicMock()
        mock_https.return_value = mock_conn

        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.__iter__.return_value = iter(mock_responses)
        mock_conn.getresponse.return_value = mock_response

        messages = [{"role": "user", "content": "What is the weather?"}]
        tools = [{"type": "function", "function": {"name": "get_weather"}}]

        result = client.stream_request_with_tools(
            messages=messages,
            max_tokens=100,
            tools=tools,
        )

        assert len(result["tool_calls"]) == 1
        # It shouldn't crash trying to parse it as JSON, it should just emit
        # the literal concatenated string so downstream layers handle it.
        assert result["tool_calls"][0]["function"]["arguments"] == '{"location": "NY'
        assert result["finish_reason"] == "tool_calls"

def test_make_chat_request_mixed_structured_blocks():
    """
    Ensure make_chat_request properly serializes a list of structured message
    parts (e.g., text, input_audio, image_url) as the user message content.
    """
    ctx = MockContext()
    client = LlmClient({"endpoint": "http://test", "model": "test-model"}, ctx)

    mixed_user_content = [
        {"type": "text", "text": "What is this?"},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,12345"}}
    ]

    messages = [
        {"role": "user", "content": mixed_user_content}
    ]

    method, path, body, headers = client.make_chat_request(messages, max_tokens=100)

    decoded = json.loads(body.decode("utf-8"))

    # We expect length 2: the auto-injected system message for the date,
    # and the user message containing our mixed block list.
    assert len(decoded["messages"]) == 2
    assert decoded["messages"][0]["role"] == "system"
    assert decoded["messages"][1]["role"] == "user"
    assert decoded["messages"][1]["content"] == mixed_user_content


def test_make_chat_request_includes_dev_build_prefix_when_enabled():
    from plugin.framework.constants import LLM_DEV_BUILD_SYSTEM_PREFIX

    ctx = MockContext()
    client = LlmClient({"endpoint": "http://test", "model": "test-model"}, ctx)
    messages = [{"role": "user", "content": "Hi"}]
    with patch("plugin.framework.constants.should_prepend_dev_llm_system_prefix", return_value=True):
        _m, _p, body, _h = client.make_chat_request(messages, max_tokens=50)
    data = json.loads(body.decode("utf-8"))
    system = data["messages"][0]["content"]
    assert system.startswith(LLM_DEV_BUILD_SYSTEM_PREFIX)
    assert "Today's date" in system


def test_make_chat_request_skips_dev_build_prefix_when_disabled():
    from plugin.framework.constants import LLM_DEV_BUILD_SYSTEM_PREFIX

    ctx = MockContext()
    client = LlmClient({"endpoint": "http://test", "model": "test-model"}, ctx)
    messages = [{"role": "system", "content": "task-only prompt"}, {"role": "user", "content": "x"}]
    with patch("plugin.framework.constants.should_prepend_dev_llm_system_prefix", return_value=True):
        _m, _p, body, _h = client.make_chat_request(
            messages,
            max_tokens=50,
            prepend_dev_build_system_prefix=False,
        )
    data = json.loads(body.decode("utf-8"))
    prefix_first_line = LLM_DEV_BUILD_SYSTEM_PREFIX.split("\n", 1)[0]
    for m in data["messages"]:
        c = m.get("content")
        if isinstance(c, str):
            assert not c.startswith(prefix_first_line), c[:120]
    assert any(
        isinstance(m.get("content"), str) and "task-only prompt" in m["content"]
        for m in data["messages"]
    )


def test_strip_leaked_chat_template_control_tokens_removes_harmony_style():
    raw = (
        '<|channel|>final <|constrain|>commentary<|message|>{\n'
        '  "name": "reply_to_user",\n'
        '  "arguments": {\n'
        '    "answer": "Hi"\n'
        "  }\n"
        "}"
    )
    out = strip_leaked_chat_template_control_tokens(raw)
    assert "<|" not in out
    assert "reply_to_user" in out
    assert "Hi" in out


def test_strip_leaked_chat_template_control_tokens_plain_unchanged():
    assert strip_leaked_chat_template_control_tokens("Hello world") == "Hello world"


def test_strip_leaked_chat_template_control_tokens_empty():
    assert strip_leaked_chat_template_control_tokens("") == ""
    assert strip_leaked_chat_template_control_tokens(None) == ""


def test_strip_leaked_chat_template_control_tokens_llama_python_tag_still_parsable():
    """Stripping ``<|python_tag|>`` leaves JSON; llama3_json parser uses ``{`` anyway."""
    raw = '<|python_tag|>{"name": "x", "arguments": {}}'
    out = strip_leaked_chat_template_control_tokens(raw)
    assert "<|" not in out
    assert out.startswith('{"name"')


def test_request_with_tools_strips_leaked_control_tokens_in_sync_response(client):
    """End-to-end: content from OpenAI-style JSON is sanitized before return."""
    payload = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": '<|channel|>x<|message|>{"name": "n", "arguments": {}}',
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {},
    }
    with (
        patch.object(client, "_get_connection") as mock_get,
        patch("plugin.modules.http.client.get_unverified_ssl_context"),
    ):
        mock_conn = MagicMock()
        mock_get.return_value = mock_conn
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = json.dumps(payload).encode("utf-8")
        mock_conn.getresponse.return_value = mock_resp

        result = client.request_with_tools([{"role": "user", "content": "hi"}], max_tokens=10)
    assert "<|" not in (result.get("content") or "")
    assert "n" in (result.get("content") or "")


def test_request_with_tools_sync_paces_consecutive_requests(client):
    """Second sync call sleeps ~50ms when monotonic time has not advanced (burst guard)."""
    ok_json = json.dumps(
        {
            "choices": [
                {
                    "message": {"role": "assistant", "content": "a"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {},
        }
    ).encode("utf-8")
    sleeps: list[float] = []

    def track_sleep(dt: float) -> None:
        sleeps.append(dt)

    with (
        patch("http.client.HTTPSConnection") as mock_https,
        patch("time.sleep", side_effect=track_sleep),
        patch("time.monotonic", side_effect=[1000.0] * 8),
    ):
        mock_conn = MagicMock()
        mock_https.return_value = mock_conn
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = ok_json
        mock_conn.getresponse.return_value = mock_resp

        client.request_with_tools([{"role": "user", "content": "x"}], max_tokens=10)
        client.request_with_tools([{"role": "user", "content": "y"}], max_tokens=10)

    assert len(sleeps) == 1
    assert abs(sleeps[0] - 0.05) < 1e-9
