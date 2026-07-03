import ssl
from unittest.mock import MagicMock, patch

import pytest

from plugin.framework.client.http_transport import LlmHttpTransport
from plugin.framework.errors import NetworkError


def test_transport_reuses_connection_and_reopens_on_endpoint_change():
    endpoint = {"url": "https://api.openai.com"}
    transport = LlmHttpTransport(lambda: endpoint["url"], lambda: 60)

    with (
        patch("http.client.HTTPSConnection") as mock_https,
        patch("http.client.HTTPConnection") as mock_http,
        patch("plugin.framework.client.http_transport.get_unverified_ssl_context") as mock_ssl,
    ):
        conn1 = transport.get_connection()
        conn2 = transport.get_connection()

        assert conn1 is conn2
        mock_https.assert_called_once_with("api.openai.com", 443, context=mock_ssl.return_value, timeout=60)

        endpoint["url"] = "http://localhost:11434"
        conn3 = transport.get_connection()

        assert conn3 is not conn1
        conn1.close.assert_called_once()
        mock_http.assert_called_once_with("localhost", 11434, timeout=60)


def test_transport_local_cert_fallback_reopens_with_unverified_context():
    transport = LlmHttpTransport(lambda: "https://localhost:11434", lambda: 60)

    with (
        patch("http.client.HTTPSConnection") as mock_https,
        patch("plugin.framework.client.http_transport.get_verified_ssl_context") as mock_verified_ssl,
        patch("plugin.framework.client.http_transport.get_unverified_ssl_context") as mock_unverified_ssl,
    ):
        transport.get_connection()
        assert mock_https.call_args_list[0].kwargs["context"] == mock_verified_ssl.return_value

        assert transport.enable_local_ssl_fallback(ssl.SSLCertVerificationError("self-signed certificate")) is True
        transport.get_connection()

        assert mock_https.call_args_list[1].kwargs["context"] == mock_unverified_ssl.return_value


def test_transport_non_local_cert_error_does_not_enable_local_fallback():
    transport = LlmHttpTransport(lambda: "https://api.openai.com", lambda: 60)

    with pytest.raises(NetworkError):
        transport.handle_connection_error(
            ssl.SSLCertVerificationError("self-signed certificate"),
            path="/v1/chat/completions",
            retry_available=False,
            retry_log_message="retry",
        )


def test_transport_stop_checker_suppresses_retry():
    transport = LlmHttpTransport(lambda: "https://api.openai.com", lambda: 60)
    transport._persistent_conn = MagicMock()

    action = transport.handle_connection_error(
        OSError("closed by stop"),
        path="/v1/chat/completions",
        retry_available=True,
        retry_log_message="retry",
        stop_checker=lambda: True,
    )

    assert action == "stop"
