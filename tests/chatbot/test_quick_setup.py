"""Unit tests for quick_setup server detection and connection testing."""

from __future__ import annotations

import io
import json
import socket
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from plugin.chatbot.quick_setup import (
    LOCAL_SERVER_PROBES,
    PROVIDER_STARTERS,
    _check_port_open,
    probe_local_servers,
    check_endpoint_connection,
)


def test_provider_starters_structure():
    """Ensure all curated provider starters have required fields and valid URLs."""
    assert len(PROVIDER_STARTERS) >= 3
    for p in PROVIDER_STARTERS:
        assert "id" in p
        assert "name" in p
        assert "display_name" in p
        assert "url" in p
        assert p["url"].startswith("http://") or p["url"].startswith("https://")


def test_openrouter_starter_has_free_model():
    """Ensure OpenRouter starter includes openrouter/free as its leading model."""
    op = next((p for p in PROVIDER_STARTERS if p.get("id") == "openrouter"), None)
    assert op is not None
    assert op["models"][0] == "openrouter/free"


def test_local_server_probes_structure():
    """Ensure all probe definitions specify name, port, path, kind, and url."""
    assert len(LOCAL_SERVER_PROBES) >= 15
    for p in LOCAL_SERVER_PROBES:
        assert "name" in p
        assert isinstance(p["port"], int)
        assert p["path"].startswith("/")
        assert p["kind"] in ("ollama", "openai")


def test_check_port_open_real_listener():
    """Test _check_port_open with an active socket."""
    for attempt in range(3):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        try:
            assert _check_port_open("127.0.0.1", port, timeout_sec=0.5) is True
        finally:
            srv.close()

        # If another process bound the port immediately after srv.close(),
        # _check_port_open might still return True. Retry in that case.
        if _check_port_open("127.0.0.1", port, timeout_sec=0.1) is False:
            return
    pytest.fail(
        f"Failed to verify closed port after {attempt + 1} attempts due to port snatching"
    )


@patch("plugin.chatbot.quick_setup._check_port_open")
@patch("urllib.request.urlopen")
def test_probe_local_servers_ollama(mock_urlopen, mock_port_open):
    """Test detecting Ollama with model list."""
    def port_check(host, port, timeout_sec=0.15):
        return port == 11434

    mock_port_open.side_effect = port_check

    ollama_response = {
        "models": [
            {"name": "llama3.2:latest", "size": 2000000},
            {"name": "deepseek-r1:8b", "size": 5000000},
        ]
    }
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = json.dumps(ollama_response).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp
    mock_urlopen.return_value = mock_resp

    detected = probe_local_servers()
    assert len(detected) == 1
    assert detected[0]["name"] == "Ollama"
    assert detected[0]["models"] == ["llama3.2:latest", "deepseek-r1:8b"]
    assert detected[0]["url"] == "http://localhost:11434"


@patch("plugin.chatbot.quick_setup._check_port_open")
@patch("urllib.request.urlopen")
def test_probe_local_servers_lm_studio(mock_urlopen, mock_port_open):
    """Test detecting LM Studio (OpenAI format)."""
    def port_check(host, port, timeout_sec=0.15):
        return port == 1234

    mock_port_open.side_effect = port_check

    lm_response = {
        "data": [
            {"id": "qwen2.5-coder-7b-instruct"},
            {"id": "mistral-7b-instruct"},
        ]
    }
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = json.dumps(lm_response).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp
    mock_urlopen.return_value = mock_resp

    detected = probe_local_servers()
    assert len(detected) == 1
    assert detected[0]["name"] == "LM Studio"
    assert detected[0]["models"] == ["qwen2.5-coder-7b-instruct", "mistral-7b-instruct"]


@patch("urllib.request.urlopen")
def test_check_endpoint_connection_success(mock_urlopen):
    """Test check_endpoint_connection successful 200 response."""
    from plugin.framework.constants import USER_AGENT

    resp_data = {"data": [{"id": "gpt-4o"}, {"id": "gpt-4o-mini"}]}
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = json.dumps(resp_data).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp
    mock_urlopen.return_value = mock_resp

    ok, msg, models = check_endpoint_connection("https://api.openai.com", "sk-test")
    assert ok is True
    assert "✓ Connected successfully!" in msg
    assert models == ["gpt-4o", "gpt-4o-mini"]

    req = mock_urlopen.call_args[0][0]
    assert req.headers["User-agent"] == USER_AGENT or req.headers.get("User-Agent") == USER_AGENT
    assert "Http-referer" not in req.headers and "HTTP-Referer" not in req.headers


@patch("urllib.request.urlopen")
def test_check_endpoint_connection_openrouter_headers(mock_urlopen):
    """Test check_endpoint_connection sends OpenRouter identification headers."""
    from plugin.framework.constants import APP_REFERER, APP_TITLE, USER_AGENT

    resp_data = {"data": [{"id": "openai/gpt-oss-120b"}]}
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = json.dumps(resp_data).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp
    mock_urlopen.return_value = mock_resp

    ok, msg, models = check_endpoint_connection("https://openrouter.ai/api", "sk-or-test")
    assert ok is True

    req = mock_urlopen.call_args[0][0]
    assert req.headers.get("User-agent") == USER_AGENT or req.headers.get("User-Agent") == USER_AGENT
    assert req.headers.get("Http-referer") == APP_REFERER or req.headers.get("HTTP-Referer") == APP_REFERER
    assert req.headers.get("X-title") == APP_TITLE or req.headers.get("X-Title") == APP_TITLE



@patch("urllib.request.urlopen")
def test_check_endpoint_connection_auth_error(mock_urlopen):
    """Test check_endpoint_connection 401 Unauthorized."""
    mock_urlopen.side_effect = urllib.error.HTTPError(
        "https://api.openai.com/v1/models", 401, "Unauthorized", {}, io.BytesIO(b"{}")
    )

    ok, msg, models = check_endpoint_connection("https://api.openai.com", "bad-key")
    assert ok is False
    assert "401 Unauthorized" in msg
    assert models == []


@patch("urllib.request.urlopen")
def test_check_endpoint_connection_network_error(mock_urlopen):
    """Test check_endpoint_connection connection refused."""
    mock_urlopen.side_effect = urllib.error.URLError("Connection refused")

    ok, msg, models = check_endpoint_connection("http://127.0.0.1:9999")
    assert ok is False
    assert "Connection failed" in msg
    assert models == []


def test_check_endpoint_connection_empty_url():
    """Test check_endpoint_connection with empty URL."""
    ok, msg, models = check_endpoint_connection("")
    assert ok is False
    assert "cannot be empty" in msg
    assert models == []
