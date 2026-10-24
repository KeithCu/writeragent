"""Unit tests for quick_setup server detection and connection testing."""

from __future__ import annotations

import io
import json
import urllib.error
from unittest.mock import MagicMock, patch

from plugin.chatbot.quick_setup import check_endpoint_connection


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
