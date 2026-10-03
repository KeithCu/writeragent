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
