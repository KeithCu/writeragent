import pytest
import json
import socket
from unittest.mock import patch, MagicMock, mock_open
import ssl
from plugin.modules.http.client import (
    LlmClient,
    is_audio_unsupported_error,
    format_error_message,
    _format_http_error_response
)

def test_is_audio_unsupported_error():
    # Common messages indicating lack of audio support
    assert is_audio_unsupported_error("unsupported content type for input audio") is True
    # "unsupported modality" test based on function signature
    assert is_audio_unsupported_error("unsupported modality") is True
    assert is_audio_unsupported_error("audio not supported") is True
    assert is_audio_unsupported_error("modality not supported") is True

    # Specific API error bodies (passed via _format_http_error_response)
    assert is_audio_unsupported_error("model cannot process audio") is True
    assert is_audio_unsupported_error("No endpoints found that support input audio") is True

    # Just a general error
    assert is_audio_unsupported_error("Connection timed out") is False
    assert is_audio_unsupported_error("HTTP Error 401") is False

def test_format_error_message():
    import urllib.error
    import http.client

    # HTTP errors
    err = urllib.error.HTTPError("url", 401, "Unauthorized", {}, None)
    assert "Invalid API Key" in format_error_message(err)

    err = urllib.error.HTTPError("url", 403, "Forbidden", {}, None)
    assert "Forbidden" in format_error_message(err)

    # Socket / Connection errors
    err = urllib.error.URLError("Connection refused")
    assert "Connection Refused" in format_error_message(err)

    # Check timeout
    err = socket.timeout("timed out")
    assert "Timed Out" in format_error_message(err) or "timed out" in format_error_message(err).lower()

@patch("plugin.modules.http.client.sync_request")
def test_transcribe_audio_uses_sync_request_fallback(mock_sync):
    """
    Test that transcribe_audio uses the multipart/form-data fallback via sync_request
    when the model does not have native audio.
    """
    # Mock return value
    mock_sync.return_value = {"text": "Hello world from STT"}

    # Mock ctx
    ctx = MagicMock()

    # We must patch has_native_audio in the namespace where transcribe_audio calls it
    # Looking at the code: from plugin.framework.config import has_native_audio
    with patch("plugin.framework.config.has_native_audio", return_value=False):
        client = LlmClient({"endpoint": "http://test", "stt_model": "whisper-1"}, ctx)

        # Call with a dummy path using mock_open
        m = mock_open(read_data=b"dummy audio data")
        with patch("builtins.open", m):
            result = client.transcribe_audio("dummy.wav")

        assert result == "Hello world from STT"
        assert mock_sync.called
        args, kwargs = mock_sync.call_args

        # Assert url
        assert args[0] == "http://test/v1/audio/transcriptions"

        # Assert headers content type was set to multipart
        headers = kwargs.get("headers", {})
        content_type = headers.get("Content-Type", "")
        assert "multipart/form-data" in content_type

        # Assert body format
        boundary = content_type.split("boundary=")[1]
        body = kwargs.get("data", b"")
        assert boundary.encode("utf-8") in body
        assert b'name="file"; filename="dummy.wav"' in body
        assert b'name="model"' in body

@patch("plugin.modules.http.client.LlmClient.chat_completion_sync")
def test_transcribe_audio_uses_native_audio(mock_sync_chat):
    """
    Test that transcribe_audio calls the native chat pipeline when the STT model
    is recognized as supporting native audio.
    """
    mock_sync_chat.return_value = "Native multimodal transcript"
    ctx = MagicMock()

    with patch("plugin.framework.config.has_native_audio", return_value=True):
        client = LlmClient({"endpoint": "http://test", "stt_model": "gemini-flash"}, ctx)

        m = mock_open(read_data=b"dummy audio data")
        with patch("builtins.open", m):
            result = client.transcribe_audio("dummy.wav")

        assert result == "Native multimodal transcript"
        assert mock_sync_chat.called

def test_llm_client_chat_with_tools_normalizes():
    """
    Test that request_with_tools normalizes standard chat completion responses.
    """
    ctx = MagicMock()
    client = LlmClient({"endpoint": "http://test", "model": "test-model"}, ctx)

    # Mock HTTP response
    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.read.return_value = json.dumps({
        "choices": [{
            "message": {
                "role": "assistant",
                "content": "Sure, calling tool.",
                "tool_calls": [{"id": "1", "type": "function", "function": {"name": "hello"}}]
            },
            "finish_reason": "tool_calls"
        }]
    }).encode("utf-8")

    mock_conn = MagicMock()
    mock_conn.getresponse.return_value = mock_response

    with patch.object(client, "_get_connection", return_value=mock_conn):
        result = client.request_with_tools([{"role": "user", "content": "Hi"}])

        assert result["role"] == "assistant"
        assert result["content"] == "Sure, calling tool."
        assert len(result["tool_calls"]) == 1
        assert result["tool_calls"][0]["function"]["name"] == "hello"

def test_llm_client_chat_with_tools_normalizes_done_reason():
    """
    Test that request_with_tools extracts finish_reason from the top-level
    done_reason when finish_reason is missing from choices (e.g. some local models).
    """
    ctx = MagicMock()
    client = LlmClient({"endpoint": "http://test", "model": "test-model"}, ctx)

    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.read.return_value = json.dumps({
        "done_reason": "stop",
        "message": {
            "role": "assistant",
            "content": "Done reasoning."
        }
    }).encode("utf-8")

    mock_conn = MagicMock()
    mock_conn.getresponse.return_value = mock_response

    with patch.object(client, "_get_connection", return_value=mock_conn):
        result = client.request_with_tools([{"role": "user", "content": "Hi"}])

        assert result["finish_reason"] == "stop"
        assert result["content"] == "Done reasoning."

def test_format_error_message_edge_cases():
    """
    Test error mapping edge cases for TLS and custom JSON error bodies.
    """
    # SSLError is mapped to a friendly message
    err = ssl.SSLError("cert error")
    assert "TLS/SSL Error:" in format_error_message(err)
    assert "cert error" in format_error_message(err)

    # test JSON decoding in _format_http_error_response
    # Valid JSON with error message object
    json_err_1 = '{"error": {"message": "Custom auth error"}}'
    msg_1 = _format_http_error_response(401, "Unauthorized", json_err_1)
    assert "Custom auth error" in msg_1
    assert "HTTP Error 401" in msg_1

    # Valid JSON but missing standard error field (fallback to snippet)
    json_err_2 = '{"foo": "bar"}'
    msg_2 = _format_http_error_response(401, "Unauthorized", json_err_2)
    assert '{"foo": "bar"}' in msg_2

    # Broken JSON fallback to snippet
    broken_json = '{ "broken json'
    msg_3 = _format_http_error_response(400, "Bad Request", broken_json)
    assert '{ "broken json' in msg_3
