import socket
import json
from unittest.mock import MagicMock, patch

import pytest

from plugin.framework.client.auth import AuthError
from plugin.framework.client.llm_client import LlmClient, strip_leaked_chat_template_control_tokens
from plugin.framework.client.request_controls import reset_host_pacing_for_tests
from plugin.framework.errors import NetworkError, format_error_message
from plugin.tests.testing_utils import MockContext, create_mock_http_response


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


@pytest.fixture(autouse=True)
def _fast_retry_waits():
    """Backoff must not sleep in unit tests; still assert call sites separately."""
    reset_host_pacing_for_tests()
    with (
        patch("plugin.framework.client.llm_client.wait_abortable", return_value=True) as llm_wait,
        patch("plugin.framework.client.http_transport.wait_abortable", return_value=True) as transport_wait,
        patch("plugin.framework.client.http_transport.wait_host_gap", return_value=True),
    ):
        yield {"llm": llm_wait, "transport": transport_wait}


def test_headers_and_config_injection(client):
    from plugin.framework.constants import APP_REFERER, APP_TITLE, USER_AGENT

    headers = client._headers()
    assert headers["Authorization"] == "Bearer sk-test-key"
    assert headers["User-Agent"] == USER_AGENT
    assert headers["Content-Type"] == "application/json"
    # OpenAI provider must not receive OpenRouter specific headers
    assert "HTTP-Referer" not in headers
    assert "X-Title" not in headers

    # OpenRouter endpoint must receive OpenRouter identification headers
    client.config["endpoint"] = "https://openrouter.ai/api"
    or_headers = client._headers()
    assert or_headers["User-Agent"] == USER_AGENT
    assert or_headers["HTTP-Referer"] == APP_REFERER
    assert or_headers["X-Title"] == APP_TITLE

    client.config["endpoint"] = "https://api.openai.com"
    assert client._endpoint() == "https://api.openai.com"
    assert client._api_path() == "/v1"

    # Test fallback OpenWebUI path
    client.config["is_openwebui"] = True
    assert client._api_path() == "/api"

    # Z.ai bare host uses /api/paas/v4 (not bare /v4)
    client.config["is_openwebui"] = False
    client.config["endpoint"] = "https://api.z.ai"
    assert client._api_path() == "/api/paas/v4"


def test_make_chat_request_logs_body_model(caplog, client):
    import logging

    from plugin.framework.client import llm_client as llm_mod
    from tests.strip_bundle import module_source_contains

    if not module_source_contains(llm_mod, "Chat Request body:"):
        pytest.skip("log.debug stripped in release bundle")
    caplog.set_level(logging.DEBUG, logger="plugin.framework.client.llm_client")
    client.config["endpoint"] = "https://api.z.ai/api/paas"
    client.config["model"] = "glm-5.2"
    client.config["api_key"] = "test-key"
    client.make_chat_request([{"role": "user", "content": "hi"}], 100, tools=[{"type": "function"}], stream=True)
    joined = "\n".join(r.message for r in caplog.records)
    assert "Chat Request body: model='glm-5.2'" in joined
    assert "full_url='https://api.z.ai/api/paas/v4/chat/completions'" in joined


def test_make_chat_request_omits_temperature_when_unset():
    """Settings default -1 means get_api_config leaves the key out; do not invent 0.5."""
    client = LlmClient(
        {"endpoint": "https://api.openai.com", "api_key": "sk-test", "model": "gpt-4o"},
        MockContext(),
    )
    _m, _p, body, _h = client.make_chat_request(
        [{"role": "user", "content": "hi"}], max_tokens=50
    )
    data = json.loads(body.decode("utf-8"))
    assert "temperature" not in data


def test_make_chat_request_includes_explicit_temperature(client):
    _m, _p, body, _h = client.make_chat_request(
        [{"role": "user", "content": "hi"}], max_tokens=50
    )
    data = json.loads(body.decode("utf-8"))
    assert data["temperature"] == pytest.approx(0.7)


def test_make_chat_request_sends_zero_temperature():
    client = LlmClient(
        {
            "endpoint": "https://api.openai.com",
            "api_key": "sk-test",
            "model": "gpt-4o",
            "temperature": 0.0,
        },
        MockContext(),
    )
    _m, _p, body, _h = client.make_chat_request(
        [{"role": "user", "content": "hi"}], max_tokens=50
    )
    data = json.loads(body.decode("utf-8"))
    assert data["temperature"] == 0.0


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
    assert client_no_key._get_provider() == "ollama"


def test_hosted_empty_api_key_raises_before_http():
    """Missing hosted keys must raise AuthError, not look like custom/401."""
    ctx = MockContext()
    for endpoint, provider in (
        ("https://api.openai.com", "openai"),
        ("https://openrouter.ai/api", "openrouter"),
    ):
        for api_key in ("", "   "):
            client = LlmClient({"endpoint": endpoint, "api_key": api_key, "model": "x"}, ctx)
            with pytest.raises(AuthError) as exc_info:
                client._resolve_auth()
            assert exc_info.value.code == "missing_api_key"
            assert exc_info.value.provider == provider
            with pytest.raises(AuthError):
                client._get_provider()
            with pytest.raises(AuthError):
                client._headers()
            with pytest.raises(AuthError):
                client.make_chat_request([{"role": "user", "content": "hi"}], 8)
            msg = format_error_message(exc_info.value)
            assert "Invalid API Key" not in msg
            assert "No API key configured" in msg


def test_custom_empty_api_key_omits_auth_headers():
    client = LlmClient({"endpoint": "http://127.0.0.1:8080/v1", "api_key": ""}, MockContext())
    assert client._get_provider() == "custom"
    assert "Authorization" not in client._headers()
    assert "x-api-key" not in client._headers()


def test_persistent_connections(client):
    with (
        patch("http.client.HTTPSConnection") as mock_https,
        patch("http.client.HTTPConnection") as mock_http,
        patch("plugin.framework.client.http_transport.get_verified_ssl_context") as mock_ssl,
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


def test_stream_request_with_tools_logs_raw_indexes_before_accumulation(client, caplog):
    mock_responses = [
        b'data: {"id":"chunk-1","model":"gpt-oss","provider":"Cerebras","choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","type":"function","function":{"name":"lookup","arguments":"{\\"query\\":\\"part"}}]}}]}\n\n',
        b'data: {"id":"chunk-2","model":"gpt-oss","provider":"Cerebras","choices":[{"delta":{"tool_calls":[{"index":1,"id":"","type":"function","function":{"name":"","arguments":" two\\"}"}}]},"finish_reason":"tool_calls"}]}\n\n',
        b"data: [DONE]\n\n",
    ]

    with patch("http.client.HTTPSConnection") as mock_https:
        mock_conn = MagicMock()
        mock_https.return_value = mock_conn
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.__iter__.return_value = iter(mock_responses)
        mock_conn.getresponse.return_value = mock_response

        with caplog.at_level("DEBUG", logger="plugin.framework.client.llm_client"):
            result = client.stream_request_with_tools(
                messages=[{"role": "user", "content": "Look it up"}],
                max_tokens=100,
                tools=[{"type": "function", "function": {"name": "lookup"}}],
            )

    assert len(result["tool_calls"]) == 2

    from plugin.framework.client import llm_client as llm_mod
    from tests.strip_bundle import module_source_contains

    if not module_source_contains(llm_mod, "raw tool_call delta"):
        pytest.skip("log.debug stripped in release bundle")

    assert "raw tool_call delta" in caplog.text
    assert 'chunk_provider=\'Cerebras\'' in caplog.text
    assert '"index": 1' in caplog.text
    assert "accumulated tool_calls" in caplog.text


def test_stream_request_with_tools_preserves_reasoning_replay(client):
    mock_responses = [
        b'data: {"choices": [{"delta": {"reasoning_content": "Let me check "}}]}\n\n',
        b'data: {"choices": [{"delta": {"reasoning_content": "the weather."}}]}\n\n',
        b'data: {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": "{}"}}]}}]}\n\n',
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

        result = client.stream_request_with_tools(
            messages=[{"role": "user", "content": "Weather?"}],
            max_tokens=100,
            tools=[{"type": "function", "function": {"name": "get_weather"}}],
            append_callback=lambda t: None,
        )

        assert result["reasoning_content"] == "Let me check the weather."
        assert result["tool_calls"][0]["function"]["name"] == "get_weather"


def test_stream_request_with_tools_reasoning_replay_single_block(client):
    mock_responses = [
        b'data: {"choices": [{"delta": {"reasoning_details": [{"type": "reasoning.text", "format": "unknown", "index": 0}]}}]}\n\n',
        b'data: {"choices": [{"delta": {"reasoning_details": [{"type": "reasoning.text", "text": "Let me ", "index": 0}]}}]}\n\n',
        b'data: {"choices": [{"delta": {"reasoning_details": [{"type": "reasoning.text", "text": "think.", "index": 0}]}}]}\n\n',
        b'data: {"choices": [{"delta": {"content": "Done."}}]}\n\n',
        b'data: {"choices": [{"finish_reason": "stop", "delta": {}}]}\n\n',
        b"data: [DONE]\n\n",
    ]

    with patch("http.client.HTTPSConnection") as mock_https:
        mock_conn = MagicMock()
        mock_https.return_value = mock_conn
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.__iter__.return_value = iter(mock_responses)
        mock_conn.getresponse.return_value = mock_response

        result = client.stream_request_with_tools(
            messages=[{"role": "user", "content": "Think?"}],
            max_tokens=100,
            tools=None,
            append_callback=lambda t: None,
        )

        assert result["content"] == "Done."
        assert "reasoning" not in result
        assert len(result["reasoning_details"]) == 1
        assert result["reasoning_details"][0]["text"] == "Let me think."
        assert result["reasoning_details"][0]["format"] == "unknown"


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
    # System message content is now flattened to string if it only contains text
    assert isinstance(decoded["messages"][0]["content"], str)
    assert "Existing structured system content" in decoded["messages"][0]["content"]


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
         patch("plugin.framework.client.http_transport.get_unverified_ssl_context") as mock_unverified_ssl:
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


def test_stream_tls_retry_does_not_backoff(_fast_retry_waits):
    import ssl

    ctx = MockContext()
    client = LlmClient({"endpoint": "https://localhost:11434"}, ctx)
    mock_responses = [
        b'data: {"choices": [{"delta": {"role": "assistant", "content": "Success"}}]}\n\n',
        b"data: [DONE]\n\n",
    ]
    with patch("http.client.HTTPSConnection") as mock_https, patch(
        "plugin.framework.client.http_transport.get_unverified_ssl_context"
    ) as mock_unverified_ssl:
        mock_unverified_ssl.return_value = "unverified_context"
        mock_conn1 = MagicMock()
        mock_conn2 = MagicMock()
        mock_https.side_effect = [mock_conn1, mock_conn2]
        mock_conn1.request.side_effect = ssl.SSLCertVerificationError("self-signed certificate")
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.__iter__.return_value = iter(mock_responses)
        mock_conn2.getresponse.return_value = mock_response
        statuses: list[str] = []
        client.stream_request_with_tools(
            messages=[{"role": "user", "content": "Hello"}],
            max_tokens=100,
            status_callback=statuses.append,
        )
    _fast_retry_waits["transport"].assert_not_called()
    assert statuses == []


def test_stream_connection_error_after_content_does_not_retry():
    from plugin.framework.errors import NetworkError

    ctx = MockContext()
    client = LlmClient({"endpoint": "https://api.openai.com", "api_key": "sk-test", "model": "gpt-4"}, ctx)
    chunks: list[str] = []

    def iterate_then_drop(_response):
        yield '{"choices": [{"delta": {"content": "Hello"}}]}'
        raise ConnectionResetError("reset after tokens")

    with (
        patch("http.client.HTTPSConnection") as mock_https,
        patch("plugin.framework.client.llm_client.iterate_sse", side_effect=iterate_then_drop),
        patch("plugin.framework.client.http_transport.get_verified_ssl_context"),
    ):
        mock_conn = MagicMock()
        mock_https.return_value = mock_conn
        mock_response = MagicMock()
        mock_response.status = 200
        mock_conn.getresponse.return_value = mock_response

        with pytest.raises(NetworkError) as err:
            client.stream_chat_response(
                [{"role": "user", "content": "Hi"}],
                max_tokens=10,
                append_callback=chunks.append,
            )

    assert err.value.code == "CONNECTION_LOST"
    assert chunks == ["Hello"]
    mock_https.assert_called_once()


def test_request_with_tools_sync_tls_retry():
    import ssl
    ctx = MockContext()
    client = LlmClient({"endpoint": "https://localhost:11434", "model": "gpt-4"}, ctx)
    ok_json = json.dumps(
        {
            "choices": [
                {
                    "message": {"role": "assistant", "content": "Success"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {},
        }
    ).encode("utf-8")

    with patch("http.client.HTTPSConnection") as mock_https, \
         patch("plugin.framework.client.http_transport.get_unverified_ssl_context") as mock_unverified_ssl:
        mock_unverified_ssl.return_value = "unverified_context"
        mock_conn1 = MagicMock()
        mock_conn2 = MagicMock()
        mock_https.side_effect = [mock_conn1, mock_conn2]
        mock_conn1.request.side_effect = ssl.SSLCertVerificationError("self-signed certificate")
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.read.return_value = ok_json
        mock_conn2.getresponse.return_value = mock_response

        result = client.request_with_tools([{"role": "user", "content": "Hello"}], max_tokens=100)

    assert mock_https.call_count == 2
    assert mock_https.call_args_list[1].kwargs["context"] == "unverified_context"
    assert result["content"] == "Success"


def test_stream_request_with_tools_malformed_tool_arguments():
    ctx = MockContext()
    # Explicitly instantiate with an HTTPS endpoint so the HTTPSConnection mock is hit
    client = LlmClient({"endpoint": "https://api.openai.com", "api_key": "sk-test", "model": "gpt-4"}, ctx)

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
    from plugin.framework.client.response_normalizers import LLM_DEV_BUILD_SYSTEM_PREFIX

    ctx = MockContext()
    client = LlmClient({"endpoint": "http://test", "model": "test-model"}, ctx)
    messages = [{"role": "user", "content": "Hi"}]
    with patch("plugin.framework.client.response_normalizers.should_prepend_dev_llm_system_prefix", return_value=True):
        _m, _p, body, _h = client.make_chat_request(messages, max_tokens=50)
    data = json.loads(body.decode("utf-8"))
    system = data["messages"][0]["content"]
    assert LLM_DEV_BUILD_SYSTEM_PREFIX in system

    assert "Today's date" in system


def test_make_chat_request_skips_dev_build_prefix_when_disabled():
    from plugin.framework.client.response_normalizers import LLM_DEV_BUILD_SYSTEM_PREFIX

    ctx = MockContext()
    client = LlmClient({"endpoint": "http://test", "model": "test-model"}, ctx)
    messages = [{"role": "system", "content": "task-only prompt"}, {"role": "user", "content": "x"}]
    with patch("plugin.framework.client.response_normalizers.should_prepend_dev_llm_system_prefix", return_value=True):
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


def test_make_chat_request_does_not_duplicate_dev_prefix_on_repeated_calls():
    """Tool loops reuse the same messages list; date + dev-prefix injection must stay idempotent."""

    ctx = MockContext()
    client = LlmClient({"endpoint": "http://test", "model": "test-model"}, ctx)
    messages = [{"role": "system", "content": "Core instructions."}]
    with patch("plugin.framework.client.response_normalizers.should_prepend_dev_llm_system_prefix", return_value=True):
        _, _, json_data1, _ = client.make_chat_request(messages, max_tokens=50)
        _, _, json_data2, _ = client.make_chat_request(messages, max_tokens=50)
    
    import json
    data = json.loads(json_data1)
    sys_content = data["messages"][0]["content"]
    marker = "[WriterAgent development build]"
    assert sys_content.count(marker) == 1
    
    # Original messages array must NOT be mutated
    assert messages[0]["content"] == "Core instructions."


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
    with patch.object(client._transport, "send") as mock_send:
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = json.dumps(payload).encode("utf-8")
        mock_send.return_value = mock_resp

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


def test_stream_request_with_tools_paces_consecutive_requests(client):
    """Streaming sends use the same burst guard as sync sends."""
    mock_responses = [
        b'data: {"choices": [{"delta": {"role": "assistant", "content": "a"}}]}\n\n',
        b"data: [DONE]\n\n",
    ]
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
        mock_resp1 = MagicMock()
        mock_resp1.status = 200
        mock_resp1.__iter__.return_value = iter(mock_responses)
        mock_resp2 = MagicMock()
        mock_resp2.status = 200
        mock_resp2.__iter__.return_value = iter(mock_responses)
        mock_conn.getresponse.side_effect = [mock_resp1, mock_resp2]

        client.stream_request_with_tools([{"role": "user", "content": "x"}], max_tokens=10)
        client.stream_request_with_tools([{"role": "user", "content": "y"}], max_tokens=10)

    assert len(sleeps) == 1
    assert abs(sleeps[0] - 0.05) < 1e-9


def test_stream_request_with_tools_stop_checker_suppresses_connection_retry(client):
    """Stop already true before connect: do not open a socket (B13)."""
    with patch("http.client.HTTPSConnection") as mock_https:
        mock_conn = MagicMock()
        mock_https.return_value = mock_conn
        mock_conn.request.side_effect = socket.timeout("timed out")

        result = client.stream_request_with_tools(
            messages=[{"role": "user", "content": "Hi"}],
            max_tokens=100,
            stop_checker=lambda: True,
        )

    assert result["finish_reason"] == "stop"
    assert mock_https.call_count == 0


def test_stop_before_connect_does_not_send(client):
    """stop() with no socket yet must latch so the worker cannot reconnect."""
    with patch("http.client.HTTPSConnection") as mock_https:
        mock_https.return_value = MagicMock()
        client.stop()
        result = client.stream_request_with_tools(
            messages=[{"role": "user", "content": "Hi"}],
            max_tokens=100,
            stop_checker=lambda: True,
        )
    assert result["finish_reason"] == "stop"
    assert mock_https.call_count == 0
    assert client._stopped is True


def test_reused_client_sends_after_stop_when_checker_clear(client):
    """Panel reuses LlmClient; UI clears the latch at the start of the next send."""
    mock_responses = [
        b'data: {"choices": [{"delta": {"role": "assistant", "content": "ok"}}]}\n\n',
        b'data: {"choices": [{"finish_reason": "stop", "delta": {}}]}\n\n',
        b"data: [DONE]\n\n",
    ]
    with patch("http.client.HTTPSConnection") as mock_https:
        mock_conn = MagicMock()
        mock_https.return_value = mock_conn
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__iter__.return_value = iter(mock_responses)
        mock_conn.getresponse.return_value = mock_resp

        client.stop()
        assert client._stopped is True
        client.clear_stop()

        result = client.stream_request_with_tools(
            messages=[{"role": "user", "content": "Hi"}],
            max_tokens=100,
            stop_checker=lambda: False,
        )
    assert result["finish_reason"] == "stop"
    assert mock_https.call_count == 1
    assert client._stopped is False


def test_make_chat_request_coalesces_system_messages(client):
    messages = [
        {"role": "system", "content": "Base instructions."},
        {"role": "system", "content": "Document context."},
        {"role": "user", "content": "Hello."}
    ]
    
    # Run the request builder for Anthropic to see how the system message is serialized
    with patch("plugin.framework.client.llm_client.LlmClient._resolve_auth") as mock_auth:
        mock_auth.return_value = {"provider": "anthropic"}
        
        _, _, json_data, _ = client.make_chat_request(messages, stream=False)
        import json
        request_body = json.loads(json_data)
        
        # The system string should be a combination of the two
        # Plus the injected date prepended
        system_content = request_body.get("system")
        assert "Base instructions." in system_content
        assert "Document context." in system_content
        assert "Today's date is" in system_content
            
    # And check Google as well
    with patch("plugin.framework.client.llm_client.LlmClient._resolve_auth") as mock_auth:
        mock_auth.return_value = {"provider": "google"}
        
        _, _, json_data, _ = client.make_chat_request(messages, stream=False)
        request_body = json.loads(json_data)
        
        api_messages = request_body.get("messages", [])
        assert api_messages[0]["role"] == "system"
        combined_text = api_messages[0]["content"]
        assert "Base instructions." in combined_text
        assert "Document context." in combined_text
        assert "Today's date is" in combined_text

    # And check OpenAI-compatible path
    with patch("plugin.framework.client.llm_client.LlmClient._resolve_auth") as mock_auth:
        mock_auth.return_value = {"provider": "openai"}
        
        _, _, json_data, _ = client.make_chat_request(messages, stream=False)
        request_body = json.loads(json_data)
        
        api_messages = request_body.get("messages", [])
        assert api_messages[0]["role"] == "system"
        combined_text = api_messages[0]["content"]
        assert "Base instructions." in combined_text
        assert "Document context." in combined_text
        assert "Today's date is" in combined_text
        assert len(api_messages) == 2  # The merged system message + the user message
        assert api_messages[1]["role"] == "user"


def test_grok_shim(client):
    with (
        patch("plugin.framework.client.llm_client.LlmClient._resolve_auth") as mock_auth,
        patch("plugin.framework.client.llm_client.sync_request") as mock_sync
    ):
        mock_auth.return_value = {"provider": "xai"}
        mock_sync.return_value = {"data": []}

        # Test image request for Grok (should omit size)
        client.image_completion("Draw a cat", model="aurora", width=1024, height=1024)
        
        # Check the request body sent to sync_request
        _, kwargs = mock_sync.call_args
        body = json.loads(kwargs["data"])
        assert body["prompt"] == "Draw a cat"
        assert body["model"] == "aurora"
        assert "size" not in body


def test_ollama_shim_image(client):
    with (
        patch("plugin.framework.client.llm_client.LlmClient._resolve_auth") as mock_auth,
        patch("plugin.framework.client.llm_client.sync_request") as mock_sync
    ):
        mock_auth.return_value = {"provider": "ollama"}
        mock_sync.return_value = {"images": ["abc"]}

        # Test image request for Ollama
        client.image_completion("Draw a dog", model="flux", width=1024, height=1024)
        
        _, kwargs = mock_sync.call_args
        body = json.loads(kwargs["data"])
        assert body["prompt"] == "Draw a dog"
        assert body["model"] == "flux"
        assert body["stream"] is False

        # Test parsing
        shim = client._get_shim()
        # Native array format
        assert shim.parse_image_responses({"images": ["abc"]}) == ["abc"]
        # Native single string format
        assert shim.parse_image_responses({"image": "def"}) == ["def"]
        # Fallback to OpenAI style
        assert shim.parse_image_responses({"data": [{"b64_json": "ghi"}]}) == ["ghi"]


def test_openrouter_shim_image(client):
    client.config["endpoint"] = "https://openrouter.ai/api"
    with (
        patch("plugin.framework.client.llm_client.LlmClient._resolve_auth") as mock_auth,
        patch("plugin.framework.client.llm_client.sync_request") as mock_sync
    ):
        mock_auth.return_value = {"provider": "openrouter"}
        mock_sync.return_value = {"data": [{"b64_json": "xyz"}]}

        # Test image request for OpenRouter
        client.image_completion("Draw a galaxy", model="bytedance-seed/seedream-4.5", width=1024, height=1024)
        
        args, kwargs = mock_sync.call_args
        assert args[0] == "https://openrouter.ai/api/v1/images"
        
        body = json.loads(kwargs["data"])
        assert body["prompt"] == "Draw a galaxy"
        assert body["model"] == "bytedance-seed/seedream-4.5"
        assert body["size"] == "1024x1024"
        assert body["aspect_ratio"] == "1:1"
        assert body["n"] == 1
        assert body["output_format"] == "webp"


def test_is_image_only_model(client):
    from plugin.framework.client.model_fetcher import is_image_only_model, _model_output_modalities

    # Reset cache
    _model_output_modalities.clear()

    # If cache has modalities
    _model_output_modalities["flux"] = ["image"]
    _model_output_modalities["gemini-image"] = ["image", "text"]

    assert is_image_only_model("https://openrouter.ai/api", "flux") is True
    assert is_image_only_model("https://openrouter.ai/api", "gemini-image") is False

    # Fallback to name heuristic
    assert is_image_only_model("https://openrouter.ai/api", "nonexistent-flux") is True
    assert is_image_only_model("https://openrouter.ai/api", "nonexistent-gemini") is False




def test_anthropic_shim(client):
    # Clear the default model so we see the shim's default
    client.config["model"] = ""
    
    with patch("plugin.framework.client.llm_client.LlmClient._resolve_auth") as mock_auth:
        mock_auth.return_value = {"provider": "anthropic"}
        
        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Hello!"}
        ]
        
        method, path, body, headers = client.make_chat_request(messages, max_tokens=100)
        
        assert method == "POST"
        assert "/v1/messages" in path
        
        data = json.loads(body)
        assert data["model"] == "claude-3-5-sonnet-20241022"
        assert data["system"].endswith("You are a helpful assistant.")
        assert data["messages"] == [{"role": "user", "content": "Hello!"}]
        assert data["max_tokens"] == 100


def test_make_chat_request_coalesces_mixed_system_messages(client):
    """Ensure merging string and list-based system messages works without error."""
    messages = [
        {"role": "system", "content": "Base instructions."},
        {"role": "system", "content": [{"type": "text", "text": "Document context."}]},
        {"role": "user", "content": "Hello."}
    ]

    with patch("plugin.framework.client.llm_client.LlmClient._resolve_auth") as mock_auth:
        mock_auth.return_value = {"provider": "openai"}
        _, _, json_data, _ = client.make_chat_request(messages, stream=False)
        data = json.loads(json_data)

        sys_msg = data["messages"][0]
        assert sys_msg["role"] == "system"
        # System message content is now flattened to string if it only contains text
        assert isinstance(sys_msg["content"], str)

        # Verify both parts are present
        all_text = sys_msg["content"]
        assert "Base instructions." in all_text
        assert "Document context." in all_text
        assert "Today's date is" in all_text


def test_prepend_dev_build_prefix_supports_list_content():
    from plugin.framework.client.llm_client import _prepend_dev_build_system_prefix_to_messages
    from plugin.framework.client.response_normalizers import LLM_DEV_BUILD_SYSTEM_PREFIX

    messages = [
        {"role": "system", "content": [{"type": "text", "text": "Existing text."}]}
    ]

    with patch("plugin.framework.client.response_normalizers.should_prepend_dev_llm_system_prefix", return_value=True):
        _prepend_dev_build_system_prefix_to_messages(messages)

    content = messages[0]["content"]
    assert isinstance(content, list)
    assert content[0]["text"].startswith(LLM_DEV_BUILD_SYSTEM_PREFIX)
    assert "Existing text." in content[0]["text"]


def test_make_chat_request_flattens_system_message(client):
    """Ensure that list-based system messages are flattened to strings if they only contain text."""
    messages = [
        {"role": "system", "content": [{"type": "text", "text": "Part 1"}, {"type": "text", "text": "Part 2"}]},
        {"role": "user", "content": "Hi"}
    ]
    
    with patch("plugin.framework.client.llm_client.LlmClient._resolve_auth") as mock_auth:
        mock_auth.return_value = {"provider": "openai"}
        _, _, json_data, _ = client.make_chat_request(messages, stream=False)
        data = json.loads(json_data)
        
        sys_msg = data["messages"][0]
        assert sys_msg["role"] == "system"
        assert isinstance(sys_msg["content"], str)
        assert "Part 1" in sys_msg["content"]
        assert "Part 2" in sys_msg["content"]
        assert "Today's date is" in sys_msg["content"]


def test_parallel_tool_calls_config(client):
    """Verify that parallel_tool_calls is currently forced to False due to subagent parsing issues."""
    messages = [{"role": "user", "content": "Hi"}]
    tools = [{"type": "function", "function": {"name": "test_tool"}}]

    # Case 1: Default (should be False due to the current subagent FIXME)
    with patch("plugin.framework.client.llm_client.LlmClient._resolve_auth") as mock_auth:
        mock_auth.return_value = {"provider": "openai"}
        _, _, body, _ = client.make_chat_request(messages, tools=tools, stream=False)
        data = json.loads(body.decode("utf-8"))
        assert data["parallel_tool_calls"] is False

    # Case 2: Explicitly False
    client.config["parallel_tool_calls"] = False
    with patch("plugin.framework.client.llm_client.LlmClient._resolve_auth") as mock_auth:
        mock_auth.return_value = {"provider": "openai"}
        _, _, body, _ = client.make_chat_request(messages, tools=tools, stream=False)
        data = json.loads(body.decode("utf-8"))
        assert data["parallel_tool_calls"] is False


def test_normalize_multimodal_messages_openai(client):
    """Test that in OpenAI/Grok/Together, tool images are moved to the user message."""
    messages = [
        {"role": "user", "content": "Tell me about this document"},
        {"role": "assistant", "content": "Let me read it."},
        {"role": "tool", "tool_call_id": "call_123", "name": "get_document_content", "content": 'Here is the page: data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAUA'}
    ]
    
    from plugin.framework.client.llm_client import normalize_multimodal_messages
    normalize_multimodal_messages(messages, "openai")
    
    # Tool message content should have its image replaced with [Image Ref]
    assert messages[2]["content"] == "Here is the page: [Image Ref]"
    
    # Nearest preceding user message (messages[0]) should have the image appended
    assert isinstance(messages[0]["content"], list)
    assert messages[0]["content"][0] == {"type": "text", "text": "Tell me about this document"}
    assert messages[0]["content"][1]["type"] == "image_url"
    assert messages[0]["content"][1]["image_url"]["url"] == "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAUA"


def test_normalize_multimodal_messages_anthropic(client):
    """Test that in Anthropic, tool images are kept in-place."""
    messages = [
        {"role": "user", "content": "Tell me about this document"},
        {"role": "assistant", "content": "Let me read it."},
        {"role": "tool", "tool_call_id": "call_123", "name": "get_document_content", "content": 'Here is the page: data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAUA'}
    ]
    
    from plugin.framework.client.llm_client import normalize_multimodal_messages
    normalize_multimodal_messages(messages, "anthropic")
    
    # Tool message content should also be stripped of the raw base64 string,
    # but re-attached to the SAME message as a list of content parts
    assert isinstance(messages[2]["content"], list)
    assert messages[2]["content"][0] == {"type": "text", "text": "Here is the page: [Image Ref]"}
    assert messages[2]["content"][1]["type"] == "image_url"
    assert messages[2]["content"][1]["image_url"]["url"] == "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAUA"
    
    # User message (messages[0]) should NOT have the image attached
    assert messages[0]["content"] == "Tell me about this document"


def test_normalize_multimodal_messages_gemini(client):
    """Test that in Gemini, tool images are moved to the user message."""
    messages = [
        {"role": "user", "content": "Tell me about this document"},
        {"role": "assistant", "content": "Let me read it."},
        {"role": "tool", "tool_call_id": "call_123", "name": "get_document_content", "content": 'Here is the page: data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAUA'}
    ]
    
    from plugin.framework.client.llm_client import normalize_multimodal_messages
    normalize_multimodal_messages(messages, "google")
    
    assert messages[2]["content"] == "Here is the page: [Image Ref]"
    assert isinstance(messages[0]["content"], list)
    assert messages[0]["content"][0] == {"type": "text", "text": "Tell me about this document"}
    assert messages[0]["content"][1]["type"] == "image_url"
    assert messages[0]["content"][1]["image_url"]["url"] == "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAUA"


def test_gemini_shim_inline_data():
    """Verify that GoogleShim correctly translates image_url to inlineData part."""
    from unittest.mock import MagicMock, patch
    from plugin.framework.client.google_shim import GoogleShim
    from plugin.framework.client.llm_client import LlmClient
    
    ctx = MagicMock()
    client = LlmClient({"endpoint": "https://generativelanguage.googleapis.com", "model": "gemini-1.5-flash"}, ctx)
    shim = GoogleShim(client)
    
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Describe this"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAUA"}}
            ]
        }
    ]
    
    with patch("plugin.framework.client.llm_client.LlmClient._resolve_auth") as mock_auth:
        mock_auth.return_value = {"api_key": "fake_key", "provider": "google"}
        _, _, json_data, _ = shim.build_chat_request(messages, 100, 0.5, None, False, "gemini-1.5-flash", None)

        data = json.loads(json_data)
        content = data["messages"][0]["content"]
        assert len(content) == 2
        assert content[0] == {"type": "text", "text": "Describe this"}
        assert content[1] == {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAUA"},
        }


def test_stream_request_with_tools_tracks_used_model(client, caplog):
    import logging
    from tests.strip_bundle import skip_if_release_build

    skip_if_release_build("log.info stripped in release bundle")

    mock_responses = [
        b'data: {"model": "deepseek/deepseek-r1:free", "choices": [{"delta": {"role": "assistant", "content": "Hello free world"}}]}\n\n',
        b'data: {"model": "deepseek/deepseek-r1:free", "choices": [{"finish_reason": "stop", "delta": {}}]}\n\n',
        b"data: [DONE]\n\n",
    ]

    with patch("http.client.HTTPSConnection") as mock_https:
        mock_conn = MagicMock()
        mock_https.return_value = mock_conn
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.__iter__.return_value = iter(mock_responses)
        mock_conn.getresponse.return_value = mock_response

        messages = [{"role": "user", "content": "Hi"}]

        with caplog.at_level(logging.INFO):
            result = client.stream_request_with_tools(
                messages=messages,
                max_tokens=100,
                model="openrouter/free",
            )

        assert result["content"] == "Hello free world"
        assert result["model"] == "deepseek/deepseek-r1:free"
        assert any("LLM response stream started" in rec.message and "deepseek/deepseek-r1:free" in rec.message for rec in caplog.records)
        assert any("LLM response stream finished" in rec.message and "deepseek/deepseek-r1:free" in rec.message for rec in caplog.records)


def test_sync_request_with_tools_tracks_used_model(client, caplog):
    import logging
    from tests.strip_bundle import skip_if_release_build

    skip_if_release_build("log.info stripped in release bundle")

    sync_resp_body = json.dumps({
        "id": "gen-123",
        "model": "deepseek/deepseek-r1:free",
        "choices": [{"message": {"role": "assistant", "content": "Sync response"}, "finish_reason": "stop"}],
    }).encode("utf-8")

    with patch("http.client.HTTPSConnection") as mock_https:
        mock_conn = MagicMock()
        mock_https.return_value = mock_conn
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.read.return_value = sync_resp_body
        mock_conn.getresponse.return_value = mock_response

        messages = [{"role": "user", "content": "Hi"}]

        with caplog.at_level(logging.INFO):
            result = client.request_with_tools(
                messages=messages,
                max_tokens=100,
                stream=False,
                model="openrouter/free",
            )

        assert result["content"] == "Sync response"
        assert result["model"] == "deepseek/deepseek-r1:free"
        assert any("LLM sync response received" in rec.message and "deepseek/deepseek-r1:free" in rec.message for rec in caplog.records)


def test_sync_request_with_tools_429_does_not_error_log_prompt(client, caplog, _fast_retry_waits):
    import logging
    busy = create_mock_http_response(
        429, json_data={"error": {"message": "overloaded"}}, reason="Too Many Requests"
    )
    ok_json = json.dumps(
        {
            "choices": [
                {"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
            ],
            "usage": {},
        }
    ).encode("utf-8")
    ok = MagicMock()
    ok.status = 200
    ok.read.return_value = ok_json
    secret = "UNIQUE_PROMPT_BLOB_FOR_429_TEST"
    with patch("http.client.HTTPSConnection") as mock_https:
        _https_steps(mock_https, busy, ok)
        with caplog.at_level(logging.ERROR):
            result = client.request_with_tools(
                messages=[{"role": "user", "content": secret}],
                max_tokens=10,
                stream=False,
            )
    assert result["content"] == "ok"
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert secret not in joined
    assert "outgoing messages" not in joined


def _sse_content_lines(*parts: str) -> list[bytes]:
    lines = [f'data: {json.dumps({"choices": [{"delta": {"content": p}}]})}'.encode() for p in parts]
    lines.append(b"data: [DONE]")
    return lines


def _https_steps(mock_https, *steps):
    """Wire HTTPSConnection: each step is a response mock or an exception from request()."""
    conns = []
    for step in steps:
        conn = MagicMock()
        if isinstance(step, BaseException):
            conn.request.side_effect = step
        else:
            conn.getresponse.return_value = step
        conns.append(conn)
    mock_https.side_effect = conns
    return conns


def _busy_then_ok(status, reason):
    busy = create_mock_http_response(
        status,
        json_data={"error": {"message": "overloaded"}},
        reason=reason,
    )
    ok = create_mock_http_response(sse_lines=_sse_content_lines("Hello"))
    return busy, ok


@pytest.mark.parametrize("status,reason", [(503, "Service Unavailable"), (429, "Too Many Requests")])
def test_stream_http_429_and_503_retry_once(client, status, reason, _fast_retry_waits):
    busy, ok = _busy_then_ok(status, reason)
    with patch("http.client.HTTPSConnection") as mock_https:
        _https_steps(mock_https, busy, ok)
        result = client.stream_request_with_tools(
            messages=[{"role": "user", "content": "Hi"}],
            max_tokens=100,
        )
    assert result["content"] == "Hello"
    assert mock_https.call_count == 2
    _fast_retry_waits["llm"].assert_called_once()


def test_stream_http_429_emits_status_callback(client, _fast_retry_waits):
    statuses: list[str] = []
    busy, ok = _busy_then_ok(429, "Too Many Requests")
    with patch("http.client.HTTPSConnection") as mock_https:
        _https_steps(mock_https, busy, ok)
        client.stream_request_with_tools(
            messages=[{"role": "user", "content": "Hi"}],
            max_tokens=100,
            status_callback=statuses.append,
        )
    assert len(statuses) == 1
    assert "retrying" in statuses[0].lower()


def test_stream_http_500_does_not_retry(client, _fast_retry_waits):
    resp = create_mock_http_response(500, json_data={"error": {"message": "boom"}}, reason="Error")
    with patch("http.client.HTTPSConnection") as mock_https:
        _https_steps(mock_https, resp)
        with pytest.raises(NetworkError) as err:
            client.stream_request_with_tools(
                messages=[{"role": "user", "content": "Hi"}],
                max_tokens=100,
            )
    assert err.value.code == "HTTP_ERROR"
    assert err.value.details["status"] == 500
    assert mock_https.call_count == 1
    _fast_retry_waits["llm"].assert_not_called()


def _http_500_diag_body(*, stream, tools, prompt, model="gpt-4o"):
    payload = {
        "model": model,
        "stream": stream,
        "messages": [{"role": "user", "content": prompt}],
    }
    if tools is not None:
        payload["tools"] = tools
    return json.dumps(payload).encode("utf-8")


def _raise_http_error(client, status, reason, body, err_json, path="/v1/chat/completions"):
    resp = create_mock_http_response(status, json_data=err_json, reason=reason)
    with pytest.raises(NetworkError) as err:
        client._retry_or_raise_http_error(
            resp,
            body,
            path,
            retries_left=2,
            emitted_any=False,
            stop_checker=None,
        )
    return err


def _assert_http_500_diag_safe(joined, *, prompt, api_key, model, stream, n_messages, n_tools, payload_bytes, reason):
    assert "HTTP 500 request diagnostic:" in joined
    assert "provider=openai" in joined
    assert "request_model=%r" % model in joined
    assert "full_url='https://api.openai.com/v1/chat/completions'" in joined
    assert "status=500" in joined
    assert "reason=%r" % reason in joined
    assert "stream=%s" % stream in joined
    assert "messages=%s" % n_messages in joined
    assert "tools=%s" % n_tools in joined
    assert "payload_bytes=%s" % payload_bytes in joined
    assert prompt not in joined
    assert api_key not in joined
    assert "Authorization" not in joined
    assert "Bearer " not in joined


def test_http_500_logs_safe_request_diag_with_tools(client, caplog):
    import logging

    from plugin.framework.client.errors import _format_http_error_response

    prompt = "UNIQUE_PROMPT_BLOB_FOR_500_DIAG_TOOLS"
    api_key = "sk-UNIQUE-SECRET-KEY-500-DIAG"
    client.config["api_key"] = api_key
    tools = [
        {
            "type": "function",
            "function": {
                "name": "lookup_secret",
                "description": "SECRET_TOOL_SCHEMA_SHOULD_NOT_APPEAR",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    body = _http_500_diag_body(stream=True, tools=tools, prompt=prompt)
    err_json = {"error": {"message": "boom echoed %s" % api_key}}
    caplog.set_level(logging.ERROR, logger="plugin.framework.client.llm_client")
    err = _raise_http_error(client, 500, "Internal Server Error", body, err_json)
    assert err.value.code == "HTTP_ERROR"
    assert err.value.details["status"] == 500
    expected_user = _format_http_error_response(
        500, "Internal Server Error", json.dumps(err_json)
    )
    assert str(err.value) == expected_user
    joined = "\n".join(r.getMessage() for r in caplog.records)
    _assert_http_500_diag_safe(
        joined,
        prompt=prompt,
        api_key=api_key,
        model="gpt-4o",
        stream=True,
        n_messages=1,
        n_tools=1,
        payload_bytes=len(body),
        reason="Internal Server Error",
    )
    assert "SECRET_TOOL_SCHEMA_SHOULD_NOT_APPEAR" not in joined
    assert "lookup_secret" not in joined
    assert "Provider API Error 500:" in joined
    assert "boom echoed <redacted>" in joined
    assert joined.count("HTTP 500 request diagnostic:") == 1


def test_http_500_logs_safe_request_diag_without_tools(client, caplog):
    import logging

    prompt = "UNIQUE_PROMPT_BLOB_FOR_500_DIAG_NO_TOOLS"
    api_key = "sk-UNIQUE-SECRET-KEY-500-DIAG-NO-TOOLS"
    client.config["api_key"] = api_key
    body = _http_500_diag_body(stream=False, tools=None, prompt=prompt, model="llama3.2")
    caplog.set_level(logging.ERROR, logger="plugin.framework.client.llm_client")
    err = _raise_http_error(
        client, 500, "Internal Server Error", body, {"error": {"message": "boom"}}
    )
    assert err.value.code == "HTTP_ERROR"
    joined = "\n".join(r.getMessage() for r in caplog.records)
    _assert_http_500_diag_safe(
        joined,
        prompt=prompt,
        api_key=api_key,
        model="llama3.2",
        stream=False,
        n_messages=1,
        n_tools=0,
        payload_bytes=len(body),
        reason="Internal Server Error",
    )
    assert "Provider API Error 500:" in joined


def test_http_429_does_not_log_500_request_diag(client, caplog, _fast_retry_waits):
    import logging

    body = _http_500_diag_body(
        stream=False,
        tools=None,
        prompt="UNIQUE_PROMPT_BLOB_FOR_429_NO_500_DIAG",
    )
    resp = create_mock_http_response(
        429, json_data={"error": {"message": "overloaded"}}, reason="Too Many Requests"
    )
    caplog.set_level(logging.ERROR, logger="plugin.framework.client.llm_client")
    action = client._retry_or_raise_http_error(
        resp,
        body,
        "/v1/chat/completions",
        retries_left=2,
        emitted_any=False,
        stop_checker=None,
    )
    assert action == "retry"
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "HTTP 500 request diagnostic:" not in joined
    assert "UNIQUE_PROMPT_BLOB_FOR_429_NO_500_DIAG" not in joined


def test_stream_http_429_retries_until_max_attempts(client):
    busy = create_mock_http_response(429, json_data={"error": {"message": "overloaded"}}, reason="Too Many Requests")
    with patch("http.client.HTTPSConnection") as mock_https:
        _https_steps(mock_https, busy, busy, busy)
        with pytest.raises(NetworkError) as err:
            client.stream_request_with_tools(
                messages=[{"role": "user", "content": "Hi"}],
                max_tokens=100,
            )
    assert err.value.code == "HTTP_ERROR"
    assert err.value.details["status"] == 429
    assert mock_https.call_count == 3


def test_stream_http_429_succeeds_on_third_attempt(client, _fast_retry_waits):
    busy, ok = _busy_then_ok(429, "Too Many Requests")
    with patch("http.client.HTTPSConnection") as mock_https:
        _https_steps(mock_https, busy, busy, ok)
        result = client.stream_request_with_tools(
            messages=[{"role": "user", "content": "Hi"}],
            max_tokens=100,
        )
    assert result["content"] == "Hello"
    assert mock_https.call_count == 3
    assert _fast_retry_waits["llm"].call_count == 2


def test_stream_timeout_retries_wait_then_succeeds(client, _fast_retry_waits):
    ok = create_mock_http_response(sse_lines=_sse_content_lines("Hello"))
    with patch("http.client.HTTPSConnection") as mock_https:
        _https_steps(mock_https, socket.timeout("timed out"), ok)
        result = client.stream_request_with_tools(
            messages=[{"role": "user", "content": "Hi"}],
            max_tokens=100,
        )
    assert result["content"] == "Hello"
    _fast_retry_waits["transport"].assert_called_once()


def test_stream_retry_backoff_stop_skips_second_send(client, _fast_retry_waits):
    _fast_retry_waits["llm"].return_value = False
    busy = create_mock_http_response(429, json_data={"error": {"message": "overloaded"}}, reason="Too Many Requests")
    with patch("http.client.HTTPSConnection") as mock_https:
        _https_steps(mock_https, busy)
        result = client.stream_request_with_tools(
            messages=[{"role": "user", "content": "Hi"}],
            max_tokens=100,
        )
    assert result["finish_reason"] == "stop"
    assert mock_https.call_count == 1


def test_stream_stop_during_host_gap_is_clean_stop_not_error(client):
    """wait_host_gap abort raises STOPPED internally; streaming loop returns stop, not HTTP_ERROR."""
    with (
        patch("plugin.framework.client.http_transport.wait_host_gap", return_value=False),
        patch("http.client.HTTPSConnection") as mock_https,
    ):
        result = client.stream_request_with_tools(
            messages=[{"role": "user", "content": "Hi"}],
            max_tokens=100,
        )
    assert result["finish_reason"] == "stop"
    assert result["content"] == ""
    assert client._stopped is True
    mock_https.assert_not_called()


def test_stream_timeout_before_tokens_retries_once(client):
    """socket.timeout before any token: one fresh-connection retry, then success."""
    ok = create_mock_http_response(sse_lines=_sse_content_lines("Hello"))
    with patch("http.client.HTTPSConnection") as mock_https:
        _https_steps(mock_https, socket.timeout("timed out"), ok)
        result = client.stream_request_with_tools(
            messages=[{"role": "user", "content": "Hi"}],
            max_tokens=100,
        )
    assert result["content"] == "Hello"
    assert mock_https.call_count == 2


def test_stream_reset_before_tokens_retries_once(client):
    """Connection reset with no tokens yet: retry once on a fresh connection."""
    reset_resp = create_mock_http_response(iter_side_effect=ConnectionResetError("reset"))
    ok = create_mock_http_response(sse_lines=_sse_content_lines("Recovered"))
    with patch("http.client.HTTPSConnection") as mock_https:
        _https_steps(mock_https, reset_resp, ok)
        result = client.stream_request_with_tools(
            messages=[{"role": "user", "content": "Hi"}],
            max_tokens=100,
        )
    assert result["content"] == "Recovered"
    assert mock_https.call_count == 2


def test_stream_reset_after_content_is_connection_lost(client):
    """After the first content token, a reset is CONNECTION_LOST (retry would duplicate text)."""
    resp = create_mock_http_response(
        sse_lines=[f'data: {json.dumps({"choices": [{"delta": {"content": "Hello"}}]})}'.encode()],
        iter_side_effect=ConnectionResetError("reset after tokens"),
    )
    chunks: list[str] = []
    with patch("http.client.HTTPSConnection") as mock_https:
        _https_steps(mock_https, resp)
        with pytest.raises(NetworkError) as err:
            client.stream_chat_response(
                [{"role": "user", "content": "Hi"}],
                max_tokens=10,
                append_callback=chunks.append,
            )
    assert err.value.code == "CONNECTION_LOST"
    assert chunks == ["Hello"]
    assert mock_https.call_count == 1


def test_stream_malformed_and_truncated_chunks_are_skipped(client):
    """Bad JSON, JSON-but-not-object, unexpected schema: skip; later valid deltas still apply."""
    lines = [
        b": ping",
        b"data: not-json-at-all",
        b'data: {"choices": [{"delta": {"content": "hel',  # truncated JSON
        b"data: [1, 2, 3]",  # JSON array, not a chunk object
        b'data: {"choices": "nope"}',  # choices is a string
        b'data: {"choices": [null]}',  # non-dict choice
        b'data: {"choices": [{"delta": "nope"}]}',  # delta is a string
        b'data: {"choices": []}',  # empty choices (usage-only style)
        * _sse_content_lines("OK"),
    ]
    resp = create_mock_http_response(sse_lines=lines)
    with patch("http.client.HTTPSConnection") as mock_https:
        _https_steps(mock_https, resp)
        result = client.stream_request_with_tools(
            messages=[{"role": "user", "content": "Hi"}],
            max_tokens=100,
        )
    assert result["content"] == "OK"


def test_stream_truncated_tool_arguments_stay_literal(client):
    """Provider deltas that concatenate to invalid JSON stay a literal string (no crash)."""
    lines = [
        b'data: {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_123", "type": "function", "function": {"name": "get_weather", "arguments": "{\\"loc"}}]}}]}',
        b'data: {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "ation\\": \\"NY"}}]}}]}',
        b'data: {"choices": [{"finish_reason": "tool_calls", "delta": {}}]}',
        b"data: [DONE]",
    ]
    resp = create_mock_http_response(sse_lines=lines)
    with patch("http.client.HTTPSConnection") as mock_https:
        _https_steps(mock_https, resp)
        result = client.stream_request_with_tools(
            messages=[{"role": "user", "content": "Weather?"}],
            max_tokens=100,
            tools=[{"type": "function", "function": {"name": "get_weather"}}],
        )
    assert len(result["tool_calls"]) == 1
    assert result["tool_calls"][0]["function"]["arguments"] == '{"location": "NY'
    assert result["finish_reason"] == "tool_calls"





