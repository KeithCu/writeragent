import ssl
from unittest.mock import MagicMock, patch

import pytest

from plugin.framework.client.http_transport import LlmHttpTransport
from plugin.framework.client.request_controls import reset_host_pacing_for_tests, reset_local_unverified_hosts_for_tests
from plugin.framework.errors import NetworkError


@pytest.fixture(autouse=True)
def _reset_host_pacing():
    reset_host_pacing_for_tests()
    reset_local_unverified_hosts_for_tests()
    yield
    reset_host_pacing_for_tests()
    reset_local_unverified_hosts_for_tests()


def test_transport_reuses_connection_and_reopens_on_endpoint_change():
    from plugin.framework.constants import LLM_CONNECT_TIMEOUT_SEC

    endpoint = {"url": "https://api.openai.com"}
    transport = LlmHttpTransport(lambda: endpoint["url"], lambda: 60)

    with (
        patch("http.client.HTTPSConnection") as mock_https,
        patch("http.client.HTTPConnection") as mock_http,
        patch("plugin.framework.client.http_transport.get_verified_ssl_context") as mock_ssl,
    ):
        conn1 = transport.get_connection()
        conn2 = transport.get_connection()

        assert conn1 is conn2
        mock_https.assert_called_once_with(
            "api.openai.com", 443, context=mock_ssl.return_value, timeout=LLM_CONNECT_TIMEOUT_SEC
        )

        endpoint["url"] = "http://localhost:11434"
        conn3 = transport.get_connection()

        assert conn3 is not conn1
        conn1.close.assert_called_once()
        mock_http.assert_called_once_with("localhost", 11434, timeout=LLM_CONNECT_TIMEOUT_SEC)


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
            retries_left=0,
            retry_log_message="retry",
        )


def test_transport_stop_checker_suppresses_retry():
    transport = LlmHttpTransport(lambda: "https://api.openai.com", lambda: 60)
    transport._persistent_conn = MagicMock()

    action = transport.handle_connection_error(
        OSError("closed by stop"),
        path="/v1/chat/completions",
        retries_left=1,
        retry_log_message="retry",
        stop_checker=lambda: True,
    )

    assert action == "stop"


def test_transport_connection_retry_waits_with_backoff():
    transport = LlmHttpTransport(lambda: "https://api.openai.com", lambda: 60)
    statuses: list[str] = []
    with patch("plugin.framework.client.http_transport.wait_abortable", return_value=True) as wait:
        action = transport.handle_connection_error(
            OSError("reset"),
            path="/v1/chat/completions",
            retries_left=1,
            retry_log_message="retry",
            status_callback=statuses.append,
        )
    assert action == "retry"
    wait.assert_called_once()
    assert len(statuses) == 1
    assert "retrying" in statuses[0].lower()


def test_transport_stop_before_retry_does_not_emit_status():
    transport = LlmHttpTransport(lambda: "https://api.openai.com", lambda: 60)
    statuses: list[str] = []
    action = transport.handle_connection_error(
        OSError("closed by stop"),
        path="/v1/chat/completions",
        retries_left=1,
        retry_log_message="retry",
        stop_checker=lambda: True,
        status_callback=statuses.append,
    )
    assert action == "stop"
    assert statuses == []


def test_transport_connection_retry_stop_during_wait():
    transport = LlmHttpTransport(lambda: "https://api.openai.com", lambda: 60)
    with patch("plugin.framework.client.http_transport.wait_abortable", return_value=False):
        action = transport.handle_connection_error(
            OSError("reset"),
            path="/v1/chat/completions",
            retries_left=1,
            retry_log_message="retry",
        )
    assert action == "stop"


def test_transport_send_uses_free_pacing_key_for_openrouter_free():
    transport = LlmHttpTransport(lambda: "https://openrouter.ai/api/v1", lambda: 60)
    mock_conn = MagicMock()
    with patch("plugin.framework.client.http_transport.wait_host_gap", return_value=True) as wait:
        transport.send(
            "POST",
            "/api/v1/chat/completions",
            b'{"model": "openrouter/free"}',
            headers={"Content-Type": "application/json"},
            connection_getter=lambda: mock_conn,
        )
    wait.assert_called_once()
    assert wait.call_args[0][0] == "openrouter.ai:free"


def test_transport_send_stop_after_connect_does_not_send_body():
    """Stop that lands while sock is still None must not send the prompt."""
    transport = LlmHttpTransport(lambda: "https://api.openai.com", lambda: 60)
    mock_conn = MagicMock()
    mock_conn.sock = None
    with pytest.raises(NetworkError) as err:
        transport.send(
            "POST",
            "/v1/chat/completions",
            b"{}",
            headers={"Content-Type": "application/json"},
            connection_getter=lambda: mock_conn,
            stop_checker=lambda: True,
        )
    assert err.value.code == "STOPPED"
    mock_conn.connect.assert_called_once()
    mock_conn.request.assert_not_called()
    mock_conn.close.assert_called_once()


def test_transport_send_stop_on_open_socket_does_not_reconnect():
    transport = LlmHttpTransport(lambda: "https://api.openai.com", lambda: 60)
    mock_conn = MagicMock()
    mock_conn.sock = MagicMock()
    with pytest.raises(NetworkError) as err:
        transport.send(
            "POST",
            "/v1/chat/completions",
            b"{}",
            headers={"User-Agent": "test"},
            connection_getter=lambda: mock_conn,
            stop_checker=lambda: True,
        )
    assert err.value.code == "STOPPED"
    mock_conn.connect.assert_not_called()
    mock_conn.request.assert_not_called()


def test_transport_send_stop_during_host_gap_raises_stopped():
    transport = LlmHttpTransport(lambda: "https://api.openai.com", lambda: 60)
    with patch("plugin.framework.client.http_transport.wait_host_gap", return_value=False):
        with pytest.raises(NetworkError) as err:
            transport.send("POST", "/v1/chat/completions", b"{}", headers={"Content-Type": "application/json"})
    assert err.value.code == "STOPPED"


def test_transport_send_applies_later_timeout_on_reused_socket():
    timeouts = {"n": 30}
    transport = LlmHttpTransport(lambda: "https://api.openai.com", lambda: timeouts["n"])
    mock_conn = MagicMock()
    mock_conn.sock = MagicMock()

    def _send() -> None:
        transport.send(
            "POST",
            "/v1/chat/completions",
            b"{}",
            headers={"User-Agent": "test"},
            connection_getter=lambda: mock_conn,
        )

    _send()
    assert mock_conn.timeout == 30
    mock_conn.sock.settimeout.assert_called_with(30)
    # Keep-alive socket: connect() must not run again (would replace the socket).
    mock_conn.connect.assert_not_called()

    timeouts["n"] = 5
    _send()
    assert mock_conn.timeout == 5
    mock_conn.sock.settimeout.assert_called_with(5)


def test_transport_send_connects_with_short_timeout_then_raises_read_timeout():
    from plugin.framework.constants import LLM_CONNECT_TIMEOUT_SEC

    transport = LlmHttpTransport(lambda: "https://api.openai.com", lambda: 120)
    mock_conn = MagicMock()
    mock_conn.sock = None
    sock_after = MagicMock()
    timeouts_seen: list[object] = []

    def _connect() -> None:
        timeouts_seen.append(mock_conn.timeout)
        mock_conn.sock = sock_after

    mock_conn.connect.side_effect = _connect
    transport.send(
        "POST",
        "/v1/chat/completions",
        b"{}",
        headers={"User-Agent": "test"},
        connection_getter=lambda: mock_conn,
    )
    assert timeouts_seen == [LLM_CONNECT_TIMEOUT_SEC]
    assert mock_conn.timeout == 120
    sock_after.settimeout.assert_called_with(120)


def test_transport_send_injects_user_agent():
    from plugin.framework.constants import USER_AGENT

    transport = LlmHttpTransport(lambda: "https://api.openai.com", lambda: 60)
    mock_conn = MagicMock()

    # When sending with no User-Agent
    transport.send(
        "POST",
        "/v1/chat/completions",
        b"{}",
        headers={"Content-Type": "application/json"},
        connection_getter=lambda: mock_conn,
    )

    mock_conn.request.assert_called_once()
    called_headers = mock_conn.request.call_args.kwargs["headers"]
    assert called_headers["User-Agent"] == USER_AGENT
    assert called_headers["Content-Type"] == "application/json"


def test_connection_error_backoff_uses_free_pacing_key_not_paid_host():
    """An OpenRouter ``:free`` failure must not stick a gap on the paid host key."""
    from plugin.framework.client.request_controls import _host_gap_sec

    transport = LlmHttpTransport(lambda: "https://openrouter.ai/api/v1", lambda: 30)
    with (
        patch("plugin.framework.client.http_transport.wait_abortable", return_value=True),
        patch("plugin.framework.client.http_transport.backoff_delay_sec", return_value=4.0),
    ):
        action = transport.handle_connection_error(
            OSError("reset"),
            path="/api/v1/chat/completions",
            retries_left=1,
            retry_log_message="retry",
            model="deepseek/deepseek-r1:free",
        )
    assert action == "retry"
    assert _host_gap_sec.get("openrouter.ai:free") == 4.0
    assert "openrouter.ai" not in _host_gap_sec


