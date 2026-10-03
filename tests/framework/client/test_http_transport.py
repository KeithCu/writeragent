import http.client
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


def test_endpoint_parts_bad_port_is_network_error():
    for url in ("http://localhost:1a34", "https://localhost:70000"):
        transport = LlmHttpTransport(lambda url=url: url, lambda: 5)
        with pytest.raises(NetworkError) as raised:
            transport._endpoint_parts()
        assert raised.value.code == "INVALID_URL"
        assert not isinstance(raised.value, ValueError)


def test_endpoint_parts_malformed_bracket_url_is_network_error():
    """``current_host`` / ``_endpoint_parts`` must not leak ``ValueError``.

    What was wrong: ``urlparse`` raises before ``_explicit_port`` for an
    unmatched bracket, so ``LlmHttpTransport.current_host`` escaped.
    """
    for url in ("http://[::1", "http://[::1]extra", "http://[]/v1"):
        transport = LlmHttpTransport(lambda url=url: url, lambda: 5)
        with pytest.raises(NetworkError) as raised:
            transport._endpoint_parts()
        assert raised.value.code == "INVALID_URL"
        assert not isinstance(raised.value, ValueError)
        with pytest.raises(NetworkError) as raised_host:
            transport.current_host()
        assert raised_host.value.code == "INVALID_URL"
        assert not isinstance(raised_host.value, ValueError)


def test_exchange_malformed_bracket_redirect_is_network_error():
    """``Location: http://[::1`` is ``NetworkError``, not a raw ``ValueError``.

    What was wrong: ``urljoin`` raises ``ValueError`` for that Location
    before ``_explicit_port``, so it skipped ``exchange``'s NetworkError catch.
    """
    transport = LlmHttpTransport(lambda: "https://example.invalid", lambda: 5)

    def sender(method, path, body, headers, *, stop_checker=None, status_callback=None):
        response = MagicMock()
        response.status = 302
        response.reason = "Found"
        response.read.return_value = b""
        response.getheader.side_effect = lambda name, default=None: "http://[::1" if str(name).lower() == "location" else default
        return response

    with pytest.raises(NetworkError) as raised:
        transport.exchange("GET", "/start", None, {}, sender=sender, parse_json=False)
    assert raised.value.code == "INVALID_URL"
    assert not isinstance(raised.value, ValueError)


def test_exchange_stops_after_bounded_redirects():
    transport = LlmHttpTransport(lambda: "https://example.invalid", lambda: 5)
    calls = {"n": 0}

    def sender(method, path, body, headers, *, stop_checker=None, status_callback=None):
        calls["n"] += 1
        response = MagicMock()
        response.status = 302
        response.reason = "Found"
        response.read.return_value = b"go"
        response.getheader.side_effect = lambda name, default=None: "/next" if str(name).lower() == "location" else default
        return response

    with pytest.raises(NetworkError):
        transport.exchange("GET", "/start", None, {}, sender=sender, parse_json=False)
    # One original response plus _MAX_REDIRECTS followed hops, then the next 302 errors.
    assert calls["n"] == 6


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


def test_transport_send_stop_already_set_does_not_open_or_send():
    """Stop latched before DNS must not connect and must not send the prompt."""
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
    mock_conn.connect.assert_not_called()
    mock_conn.request.assert_not_called()


def test_transport_stop_during_connect_does_not_wait_or_send():
    """Stop during DNS/connect must abort the caller. Waiting out connect is a no-op Stop."""
    import threading
    import time

    transport = LlmHttpTransport(lambda: "https://api.openai.com", lambda: 120)
    mock_conn = MagicMock()
    mock_conn.sock = None
    started = threading.Event()
    release = threading.Event()

    def _connect() -> None:
        started.set()
        release.wait(10)
        mock_conn.sock = MagicMock()

    mock_conn.connect.side_effect = _connect
    stop = {"on": False}

    def _stop_later() -> None:
        assert started.wait(2)
        stop["on"] = True

    threading.Thread(target=_stop_later, daemon=True).start()
    t0 = time.monotonic()
    try:
        with pytest.raises(NetworkError) as err:
            transport.send(
                "POST",
                "/v1/chat/completions",
                b"{}",
                headers={"Content-Type": "application/json"},
                connection_getter=lambda: mock_conn,
                stop_checker=lambda: stop["on"],
            )
        assert err.value.code == "STOPPED"
        assert time.monotonic() - t0 < 2
        mock_conn.request.assert_not_called()
    finally:
        release.set()


def test_transport_connect_timeout_does_not_wait_for_read_budget(monkeypatch):
    """A hung connect must use the connect budget, not Settings request_timeout."""
    import threading
    import time

    monkeypatch.setattr("plugin.framework.constants.LLM_CONNECT_TIMEOUT_SEC", 0.2)
    transport = LlmHttpTransport(lambda: "https://api.openai.com", lambda: 30)
    mock_conn = MagicMock()
    mock_conn.sock = None
    release = threading.Event()

    def _connect() -> None:
        release.wait(10)
        mock_conn.sock = MagicMock()

    mock_conn.connect.side_effect = _connect
    t0 = time.monotonic()
    try:
        with pytest.raises(TimeoutError):
            transport.send(
                "POST",
                "/v1/chat/completions",
                b"{}",
                headers={"User-Agent": "test"},
                connection_getter=lambda: mock_conn,
            )
        elapsed = time.monotonic() - t0
        assert elapsed < 2
        mock_conn.request.assert_not_called()
    finally:
        release.set()


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


def test_exchange_truncated_json_is_not_a_finished_reply():
    """A cut-off provider body must not be repaired into choices/content."""
    from plugin.framework.json_utils import safe_json_loads

    raw = b'{"choices":[{"message":{"content":"hel'
    repaired = safe_json_loads(raw)
    assert isinstance(repaired, dict)
    assert repaired["choices"][0]["message"]["content"] == "hel"

    transport = LlmHttpTransport(lambda: "https://api.openai.com", lambda: 30)
    response = MagicMock()
    response.status = 200
    response.read.return_value = raw
    response.getheader.return_value = None
    with patch.object(transport, "send", return_value=response):
        with pytest.raises(NetworkError) as err:
            transport.exchange("POST", "/v1/chat/completions", b"{}", {"Content-Type": "application/json"}, parse_json=True)
    assert err.value.code == "BAD_RESPONSE"
    assert "hel" not in str(err.value)


def _exchange_sender(script: list):
    """Return a sender that walks ``script`` and fails if the budget runs away.

    Each item is an exception (raised instead of a response) or a response mock.
    """
    calls = {"n": 0}
    steps = list(script)

    def _sender(method, path, body, headers, *, stop_checker=None, status_callback=None):
        calls["n"] += 1
        if calls["n"] > len(steps):
            raise AssertionError("exchange kept sending after the script ended")
        step = steps[calls["n"] - 1]
        if isinstance(step, BaseException):
            raise step
        return step

    return _sender, calls


def _status_response(status: int, *, read_error: BaseException | None = None, body: bytes = b"", content_type: str | None = None):
    response = MagicMock()
    response.status = status
    response.reason = "error" if status != 200 else "OK"
    if read_error is not None:
        response.read.side_effect = read_error
    else:
        response.read.return_value = body
    response.getheader.return_value = content_type
    return response


def test_exchange_error_body_read_failure_costs_one_retry():
    """A reset while reading a non-200 body is one attempt, so the third try still runs."""
    import http.client

    transport = LlmHttpTransport(lambda: "https://api.openai.com", lambda: 30)
    read_error = http.client.IncompleteRead(b"partial", 40)
    ok = _status_response(200, body=b'{"ok": true}', content_type="application/json")
    sender, calls = _exchange_sender([
        _status_response(500, read_error=read_error),
        _status_response(500, read_error=read_error),
        ok,
    ])
    attempts: list[int] = []

    def _delay(*, attempt: int = 1, **_kwargs):
        attempts.append(attempt)
        return 0.0

    with (
        patch("plugin.framework.client.http_transport.wait_abortable", return_value=True),
        patch("plugin.framework.client.http_transport.backoff_delay_sec", side_effect=_delay),
    ):
        result = transport.exchange("GET", "/v1/models", None, {}, sender=sender, parse_json=True)
    assert result.parsed == {"ok": True}
    assert calls["n"] == 3
    assert attempts == [1, 2]


def test_exchange_error_body_read_failures_stop_after_three_attempts():
    """Three failed error-body reads exhaust the budget. They must not stop after two."""
    import http.client

    transport = LlmHttpTransport(lambda: "https://api.openai.com", lambda: 30)
    read_error = ConnectionResetError("reset while reading error body")
    sender, calls = _exchange_sender([
        _status_response(503, read_error=read_error),
        _status_response(503, read_error=http.client.IncompleteRead(b"", 8)),
        _status_response(503, read_error=read_error),
    ])
    with (
        patch("plugin.framework.client.http_transport.wait_abortable", return_value=True),
        patch("plugin.framework.client.http_transport.backoff_delay_sec", return_value=0.0),
    ):
        with pytest.raises(NetworkError) as err:
            transport.exchange("GET", "/v1/models", None, {}, sender=sender)
    assert err.value.code == "CONNECTION_ERROR"
    assert calls["n"] == 3


def test_exchange_connection_error_before_status_still_costs_one_retry():
    """A failure before any status is unchanged: each send still spends one attempt."""
    transport = LlmHttpTransport(lambda: "https://api.openai.com", lambda: 30)
    ok = _status_response(200, body=b"audio", content_type="audio/mpeg")
    sender, calls = _exchange_sender([
        TimeoutError("timed out"),
        ConnectionResetError("reset"),
        ok,
    ])
    attempts: list[int] = []

    def _delay(*, attempt: int = 1, **_kwargs):
        attempts.append(attempt)
        return 0.0

    with (
        patch("plugin.framework.client.http_transport.wait_abortable", return_value=True),
        patch("plugin.framework.client.http_transport.backoff_delay_sec", side_effect=_delay),
    ):
        result = transport.exchange("POST", "/v1/audio/speech", b"{}", {}, sender=sender, parse_json=False)
    assert result.body == b"audio"
    assert calls["n"] == 3
    assert attempts == [1, 2]


def _open_socket():
    """A real socket so send() treats the connection as an already-open keep-alive."""
    import socket

    return socket.socket(socket.AF_INET, socket.SOCK_STREAM)


@pytest.mark.parametrize(
    "exc",
    [
        http.client.RemoteDisconnected("closed"),
        BrokenPipeError("pipe"),
        ConnectionResetError("reset"),
    ],
)
def test_reused_socket_failure_before_response_resends_once(exc):
    """An already-open socket that dies before response bytes is resent once.

    The resend must not remember a host gap or show Provider busy. A second
    failure is not swallowed.
    """
    transport = LlmHttpTransport(lambda: "https://api.openai.com", lambda: 30)
    transport._pacer.min_interval_sec = 0
    sockets = []
    conns = []
    ok = MagicMock()
    ok.status = 200

    def getter():
        conn = MagicMock()
        if len(conns) == 0:
            sock = _open_socket()
            sockets.append(sock)
            conn.sock = sock
            conn.request.side_effect = exc
        else:
            conn.sock = None
            conn.getresponse.return_value = ok
        conns.append(conn)
        return conn

    statuses: list[str] = []
    try:
        with patch("plugin.framework.client.http_transport.remember_host_gap") as remember:
            response = transport.send(
                "POST",
                "/v1/chat/completions",
                b'{"model":"gpt-4o"}',
                {"Content-Type": "application/json"},
                connection_getter=getter,
                status_callback=statuses.append,
            )
        assert response is ok
        assert len(conns) == 2
        assert statuses == []
        remember.assert_not_called()
    finally:
        for sock in sockets:
            sock.close()


def test_reused_socket_remote_disconnected_on_getresponse_resends_once():
    """RemoteDisconnected from getresponse() is still before any response bytes."""
    import http.client

    transport = LlmHttpTransport(lambda: "https://api.openai.com", lambda: 30)
    transport._pacer.min_interval_sec = 0
    sock = _open_socket()
    stale = MagicMock()
    stale.sock = sock
    stale.getresponse.side_effect = http.client.RemoteDisconnected("closed")
    ok = MagicMock()
    ok.status = 200
    fresh = MagicMock()
    fresh.sock = None
    fresh.getresponse.return_value = ok
    conns = [stale, fresh]

    def getter():
        return conns.pop(0)

    statuses: list[str] = []
    try:
        with patch("plugin.framework.client.http_transport.remember_host_gap") as remember:
            response = transport.send(
                "POST",
                "/v1/chat/completions",
                b'{"model":"gpt-4o"}',
                {"User-Agent": "test"},
                connection_getter=getter,
                status_callback=statuses.append,
            )
        assert response is ok
        assert statuses == []
        remember.assert_not_called()
        stale.getresponse.assert_called_once()
        fresh.request.assert_called_once()
    finally:
        sock.close()


def test_stale_resend_does_not_loop():
    """The free resend happens once. The next same failure leaves send()."""
    import http.client

    transport = LlmHttpTransport(lambda: "https://api.openai.com", lambda: 30)
    transport._pacer.min_interval_sec = 0
    sockets = []
    conns = []

    def getter():
        if len(conns) >= 2:
            raise AssertionError("resent more than once")
        conn = MagicMock()
        sock = _open_socket()
        sockets.append(sock)
        conn.sock = sock
        conn.request.side_effect = http.client.RemoteDisconnected("closed")
        conns.append(conn)
        return conn

    statuses: list[str] = []
    try:
        with patch("plugin.framework.client.http_transport.remember_host_gap") as remember:
            with pytest.raises(http.client.RemoteDisconnected):
                transport.send(
                    "POST",
                    "/v1/chat/completions",
                    b'{"model":"gpt-4o"}',
                    {"Content-Type": "application/json"},
                    connection_getter=getter,
                    status_callback=statuses.append,
                )
        assert len(conns) == 2
        assert statuses == []
        remember.assert_not_called()
    finally:
        for sock in sockets:
            sock.close()


def test_fresh_socket_reset_is_not_a_free_resend():
    """A reset while opening a new socket still belongs to the retry budget."""
    transport = LlmHttpTransport(lambda: "https://api.openai.com", lambda: 30)
    transport._pacer.min_interval_sec = 0
    conn = MagicMock()
    conn.sock = None
    conn.request.side_effect = ConnectionResetError("reset")
    with pytest.raises(ConnectionResetError):
        transport.send(
            "POST",
            "/v1/chat/completions",
            b"{}",
            {"User-Agent": "test"},
            connection_getter=lambda: conn,
        )
    assert conn.request.call_count == 1


def test_reused_socket_timeout_is_not_a_free_resend():
    """Only RemoteDisconnected, BrokenPipe, and ConnectionReset get the free resend."""
    transport = LlmHttpTransport(lambda: "https://api.openai.com", lambda: 30)
    transport._pacer.min_interval_sec = 0
    sock = _open_socket()
    conn = MagicMock()
    conn.sock = sock
    conn.request.side_effect = TimeoutError("timed out")
    try:
        with pytest.raises(TimeoutError):
            transport.send(
                "POST",
                "/v1/chat/completions",
                b"{}",
                {"User-Agent": "test"},
                connection_getter=lambda: conn,
            )
        assert conn.request.call_count == 1
    finally:
        sock.close()


