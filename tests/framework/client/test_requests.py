"""sync_request requires an explicit timeout (no silent 10s default)."""

import inspect
import logging
from io import BytesIO
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

import pytest

from plugin.framework.client.requests import sync_request
from plugin.framework.constants import LLM_CONNECT_TIMEOUT_SEC
from plugin.framework.errors import NetworkError


def _opener_patch(side_effect: object):
    """Patch the split-timeout opener; sync_request no longer uses urlopen."""
    opener = MagicMock()
    opener.open.side_effect = side_effect
    return patch("plugin.framework.client.requests._split_timeout_opener", return_value=opener), opener


def test_sync_request_timeout_is_required_keyword():
    params = inspect.signature(sync_request).parameters
    timeout = params["timeout"]
    assert timeout.default is inspect.Parameter.empty
    assert timeout.kind is inspect.Parameter.KEYWORD_ONLY


def test_sync_request_without_timeout_raises_typeerror():
    with pytest.raises(TypeError, match="timeout"):
        sync_request("https://example.invalid")


def test_sync_request_log_omits_query_and_body(caplog):
    secret = "sk-live-secret"
    url = f"https://api.example/v1/models?api_key={secret}"
    err = HTTPError(url, 401, "Unauthorized", hdrs=None, fp=BytesIO(f"token {secret}".encode()))
    opener_patch, _unused = _opener_patch(err)
    with caplog.at_level(logging.DEBUG), opener_patch:
        with pytest.raises(NetworkError) as raised:
            sync_request(url, timeout=1)
    assert secret not in caplog.text
    assert "api.example" in caplog.text
    # The provider body can echo the token; the stashed URL must not.
    stashed = raised.value.details.get("url", "")
    assert secret not in stashed
    assert "?" not in stashed
    assert stashed == "https://api.example/v1/models"
    assert secret in str(raised.value)


def test_sync_request_http_error_redacts_authorization_key():
    secret = "sk-catalog-secret"
    err = HTTPError(
        "https://api.example/v1/models",
        401,
        "Unauthorized",
        hdrs=None,
        fp=BytesIO(f"rejected {secret}".encode()),
    )
    opener_patch, _unused = _opener_patch(err)
    with opener_patch:
        with pytest.raises(NetworkError) as raised:
            sync_request(
                "https://api.example/v1/models",
                headers={"Authorization": f"Bearer {secret}"},
                timeout=1,
            )
    assert secret not in str(raised.value)
    assert "<redacted>" in str(raised.value)


def test_sync_request_retries_429_then_succeeds():
    body = BytesIO(b"busy")
    busy = HTTPError("https://api.example/v1/models", 429, "Too Many Requests", hdrs=None, fp=body)
    ok = MagicMock()
    ok.getcode.return_value = 200
    ok.read.return_value = b'{"data":[]}'
    ok.__enter__.return_value = ok
    ok.__exit__.return_value = False
    opener_patch, _unused = _opener_patch([busy, ok])
    with (
        patch("plugin.framework.client.requests.wait_abortable", return_value=True),
        patch("plugin.framework.client.requests.remember_host_gap") as remember,
        opener_patch,
    ):
        result = sync_request("https://api.example/v1/models", timeout=1)
    assert result == {"data": []}
    remember.assert_called_once()
    assert remember.call_args[0][0] == "api.example"
    assert body.closed


def test_sync_request_connection_retry_remembers_host_gap():
    ok = MagicMock()
    ok.getcode.return_value = 200
    ok.read.return_value = b'{"data":[]}'
    ok.__enter__.return_value = ok
    ok.__exit__.return_value = False
    opener_patch, _unused = _opener_patch([TimeoutError("timed out"), ok])
    with (
        patch("plugin.framework.client.requests.wait_abortable", return_value=True),
        patch("plugin.framework.client.requests.remember_host_gap") as remember,
        opener_patch,
    ):
        result = sync_request("https://api.example/v1/models", timeout=1)
    assert result == {"data": []}
    remember.assert_called_once()
    assert remember.call_args[0][0] == "api.example"


def test_sync_request_redacts_x_goog_api_key():
    secret = "goog-secret-key-xyz"
    body = BytesIO(f'{{"error":{{"message":"bad {secret}"}}}}'.encode())
    err = HTTPError("https://generativelanguage.googleapis.com/v1", 401, "Unauthorized", hdrs=None, fp=body)
    opener_patch, _unused = _opener_patch(err)
    with opener_patch:
        with pytest.raises(NetworkError) as raised:
            sync_request(
                "https://generativelanguage.googleapis.com/v1",
                headers={"x-goog-api-key": secret},
                timeout=1,
            )
    assert secret not in str(raised.value)
    assert "<redacted>" in str(raised.value)


def test_sync_request_uses_split_connect_and_read_timeouts():
    ok = MagicMock()
    ok.getcode.return_value = 200
    ok.read.return_value = b'{"ok":true}'
    ok.__enter__.return_value = ok
    ok.__exit__.return_value = False
    opener = MagicMock()
    opener.open.return_value = ok
    with patch("plugin.framework.client.requests._split_timeout_opener", return_value=opener) as split:
        sync_request("https://api.example/v1/models", timeout=90)
    assert split.call_args.kwargs["connect_timeout"] == float(LLM_CONNECT_TIMEOUT_SEC)
    assert split.call_args.kwargs["read_timeout"] == 90.0
