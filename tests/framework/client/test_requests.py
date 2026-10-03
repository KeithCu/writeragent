"""sync_request is the shared transport, with an explicit read timeout."""

import inspect
import logging
from unittest.mock import MagicMock, patch

import pytest

from plugin.framework.client.requests import sync_request
from plugin.framework.client.request_controls import reset_host_pacing_for_tests, reset_local_unverified_hosts_for_tests
from plugin.framework.errors import NetworkError


@pytest.fixture(autouse=True)
def _reset_host_pacing():
    reset_host_pacing_for_tests()
    reset_local_unverified_hosts_for_tests()
    yield
    reset_host_pacing_for_tests()
    reset_local_unverified_hosts_for_tests()


def _response(status: int, body: bytes, reason: str = "error"):
    response = MagicMock()
    response.status = status
    response.reason = reason
    response.read.return_value = body
    response.getheader.return_value = None
    return response


def _patch_https(response_or_list):
    responses = list(response_or_list) if isinstance(response_or_list, list) else [response_or_list]

    def _factory(*_args, **_kwargs):
        conn = MagicMock()
        conn.getresponse.return_value = responses.pop(0)
        return conn

    return patch("http.client.HTTPSConnection", side_effect=_factory)


def test_sync_request_timeout_is_required_keyword():
    params = inspect.signature(sync_request).parameters
    timeout = params["timeout"]
    assert timeout.default is inspect.Parameter.empty
    assert timeout.kind is inspect.Parameter.KEYWORD_ONLY


def test_sync_request_without_timeout_raises_typeerror():
    with pytest.raises(TypeError, match="timeout"):
        sync_request("https://example.invalid")


def test_sync_request_log_omits_query_secret(caplog):
    secret = "sk-live-secret"
    url = f"https://api.example/v1/models?api_key={secret}"
    body = f"token {secret}".encode()
    with caplog.at_level(logging.DEBUG), _patch_https(_response(401, body, "Unauthorized")):
        with pytest.raises(NetworkError) as raised:
            sync_request(url, timeout=1)
    assert secret not in caplog.text
    assert secret not in str(raised.value)
    assert "<redacted>" in str(raised.value)
    stashed = raised.value.details.get("url", "")
    assert secret not in stashed
    assert "?" not in stashed


def test_sync_request_http_error_redacts_authorization_key(caplog):
    secret = "sk-catalog-secret"
    with caplog.at_level(logging.DEBUG), _patch_https(_response(401, f"rejected {secret}".encode(), "Unauthorized")):
        with pytest.raises(NetworkError) as raised:
            sync_request(
                "https://api.example/v1/models",
                headers={"Authorization": f"Bearer {secret}"},
                timeout=1,
            )
    assert secret not in caplog.text
    assert secret not in str(raised.value)
    assert "<redacted>" in str(raised.value)


def test_sync_request_retries_429_then_succeeds():
    busy = _response(429, b"busy", "Too Many Requests")
    ok = _response(200, b'{"data":[]}', "OK")
    with (
        patch("plugin.framework.client.http_transport.wait_abortable", return_value=True),
        patch("plugin.framework.client.http_transport.remember_host_gap") as remember,
        _patch_https([busy, ok]),
    ):
        result = sync_request("https://api.example/v1/models", timeout=1)
    assert result == {"data": []}
    remember.assert_called()
    assert remember.call_args[0][0] == "api.example"


def test_sync_request_connection_retry_remembers_host_gap():
    ok = _response(200, b'{"data":[]}', "OK")
    first = MagicMock()
    first.getresponse.side_effect = TimeoutError("timed out")
    second = MagicMock()
    second.getresponse.return_value = ok
    conns = [first, second]

    def _factory(*_args, **_kwargs):
        return conns.pop(0)

    with (
        patch("http.client.HTTPSConnection", side_effect=_factory),
        patch("plugin.framework.client.http_transport.wait_abortable", return_value=True),
        patch("plugin.framework.client.http_transport.remember_host_gap") as remember,
    ):
        result = sync_request("https://api.example/v1/models", timeout=1)
    assert result == {"data": []}
    remember.assert_called()
    assert remember.call_args[0][0] == "api.example"


def test_sync_request_redacts_x_goog_api_key(caplog):
    secret = "goog-secret-key-xyz"
    body = f'{{"error":{{"message":"bad {secret}"}}}}'.encode()
    with caplog.at_level(logging.DEBUG), _patch_https(_response(401, body, "Unauthorized")):
        with pytest.raises(NetworkError) as raised:
            sync_request(
                "https://generativelanguage.googleapis.com/v1",
                headers={"x-goog-api-key": secret},
                timeout=1,
            )
    assert secret not in caplog.text
    assert secret not in str(raised.value)
    assert "<redacted>" in str(raised.value)


def test_sync_request_follows_redirect_and_drops_cross_origin_auth():
    """A 302 from an image/CDN URL must be followed, not raised as HTTP_ERROR.

    302 switches POST to GET and drops the body. Secret headers do not
    travel to the other host.
    """
    seen = []
    redir = _response(302, b"", "Found")
    redir.getheader.side_effect = lambda name, default=None: (
        "https://cdn.example/file.bin" if str(name).lower() == "location" else default
    )
    ok = _response(200, b"file-bytes", "OK")
    conns = []

    def _factory(host, *_args, **_kwargs):
        conn = MagicMock()
        conns.append(host)
        conn.getresponse.return_value = redir if len(conns) == 1 else ok

        def _request(method, path, body=None, headers=None):
            seen.append((host, method, path, body, dict(headers or {})))

        conn.request.side_effect = _request
        return conn

    with patch("http.client.HTTPSConnection", side_effect=_factory):
        body = sync_request(
            "https://example.invalid/img",
            data=b'{"prompt":"x"}',
            headers={"Authorization": "Bearer sk-live-secret-value"},
            timeout=1,
            parse_json=False,
        )
    assert body == b"file-bytes"
    assert seen[0][0] == "example.invalid"
    assert seen[0][1] == "POST"
    assert seen[1][0] == "cdn.example"
    assert seen[1][1] == "GET"
    assert seen[1][2] == "/file.bin"
    assert seen[1][3] is None
    assert "Authorization" not in {key.lower() for key in seen[1][4]}
    assert "sk-live-secret-value" not in str(seen[1][4])


def test_sync_request_307_keeps_method_and_body():
    seen = []
    redir = _response(307, b"", "Temporary Redirect")
    redir.getheader.side_effect = lambda name, default=None: (
        "https://cdn.example/upload" if str(name).lower() == "location" else default
    )
    ok = _response(200, b"ok", "OK")
    conns: list[str] = []

    def _factory(host, *_args, **_kwargs):
        conn = MagicMock()
        conns.append(host)
        conn.getresponse.return_value = redir if len(conns) == 1 else ok

        def _request(method, path, body=None, headers=None):
            seen.append((method, path, body))

        conn.request.side_effect = _request
        return conn

    with patch("http.client.HTTPSConnection", side_effect=_factory):
        body = sync_request("https://example.invalid/upload", data=b"audio", timeout=1, parse_json=False)
    assert body == b"ok"
    assert seen[1] == ("POST", "/upload", b"audio")


def test_sync_request_bad_port_is_network_error():
    from plugin.framework.client.http_transport import origin_and_path, public_target

    for url in ("http://localhost:1a34/v1", "http://localhost:99999/v1"):
        with pytest.raises(NetworkError) as raised:
            origin_and_path(url)
        assert not isinstance(raised.value, ValueError)
        assert raised.value.code == "INVALID_URL"
        with pytest.raises(NetworkError) as raised_public:
            public_target(url)
        assert raised_public.value.code == "INVALID_URL"
        with pytest.raises(NetworkError) as raised_sync:
            sync_request(url, timeout=1)
        assert raised_sync.value.code == "INVALID_URL"


def test_sync_request_truncated_json_is_not_repaired():
    from plugin.framework.json_utils import safe_json_loads

    raw = b'{"data":[{"id":"gpt-secret-model"'
    repaired = safe_json_loads(raw)
    assert repaired["data"][0]["id"] == "gpt-secret-model"
    with _patch_https(_response(200, raw, "OK")):
        with pytest.raises(NetworkError) as raised:
            sync_request("https://api.example/v1/models", timeout=1)
    assert raised.value.code == "BAD_RESPONSE"
    assert "gpt-secret-model" not in str(raised.value)
