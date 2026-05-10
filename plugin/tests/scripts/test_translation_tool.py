import pytest
from unittest.mock import patch, MagicMock
from plugin.modules.chatbot.translation_tool import _sse_iter
from plugin.framework.constants import USER_AGENT, APP_REFERER, APP_TITLE


def test_sse_iter_yields_payloads():
    """Test that _sse_iter correctly yields payloads until [DONE]."""
    mock_bytes = [
        b"data: payload1\n\n",
        b"data: payload2\n\n",
        b"data: {}\n\n",
        b"data: [DONE]\n\n",
        b"data: payload_after_done\n\n" # Should not be yielded
    ]

    with patch("urllib.request.Request") as mock_req_cls, \
         patch("urllib.request.urlopen") as mock_urlopen:

        # Setup mock stream
        mock_stream = MagicMock()
        mock_urlopen.return_value.__enter__.return_value = mock_stream
        mock_stream.__iter__.return_value = iter(mock_bytes)

        results = list(_sse_iter("http://test.url"))

        assert results == ["payload1", "payload2", "{}"]

        mock_req_cls.assert_called_once()
        mock_urlopen.assert_called_once()


def test_sse_iter_headers():
    """Test that default headers are injected and custom headers are preserved."""
    with patch("urllib.request.Request") as mock_req_cls, \
         patch("urllib.request.urlopen") as mock_urlopen:

        mock_stream = MagicMock()
        mock_urlopen.return_value.__enter__.return_value = mock_stream
        mock_stream.__iter__.return_value = iter([])

        # 1. No headers provided
        list(_sse_iter("http://test.url"))
        call_args = mock_req_cls.call_args[1]
        assert call_args["headers"] == {
            "User-Agent": USER_AGENT,
            "HTTP-Referer": APP_REFERER,
            "X-Title": APP_TITLE
        }

        # 2. Some custom headers provided
        custom_headers = {"Custom-Header": "value", "User-Agent": "Custom-Agent"}
        mock_req_cls.reset_mock()
        list(_sse_iter("http://test.url", headers=custom_headers))

        call_args = mock_req_cls.call_args[1]
        assert call_args["headers"] == {
            "Custom-Header": "value",
            "User-Agent": "Custom-Agent", # Preserved
            "HTTP-Referer": APP_REFERER,
            "X-Title": APP_TITLE
        }


def test_sse_iter_timeout_and_data():
    """Test that timeout and data are correctly passed."""
    with patch("urllib.request.Request") as mock_req_cls, \
         patch("urllib.request.urlopen") as mock_urlopen:

        mock_stream = MagicMock()
        mock_urlopen.return_value.__enter__.return_value = mock_stream
        mock_stream.__iter__.return_value = iter([])

        test_data = b"test_data"
        test_timeout = 42

        list(_sse_iter("http://test.url", data=test_data, timeout=test_timeout))

        # Check data was passed to Request
        req_kwargs = mock_req_cls.call_args[1]
        assert req_kwargs["data"] == test_data

        # Check timeout was passed to urlopen
        # Note: urlopen(req, timeout=...) -> arg 0 is req, kwargs has timeout
        urlopen_kwargs = mock_urlopen.call_args[1]
        assert urlopen_kwargs["timeout"] == test_timeout
