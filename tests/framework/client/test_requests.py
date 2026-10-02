"""sync_request requires an explicit timeout (no silent 10s default)."""

import inspect
import logging
from io import BytesIO
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

import pytest

from plugin.framework.client.requests import sync_request
from plugin.framework.errors import NetworkError


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
    with caplog.at_level(logging.DEBUG), patch("plugin.framework.client.requests.urlopen", side_effect=err):
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
    with patch("plugin.framework.client.requests.urlopen", side_effect=err):
        with pytest.raises(NetworkError) as raised:
            sync_request(
                "https://api.example/v1/models",
                headers={"Authorization": f"Bearer {secret}"},
                timeout=1,
            )
    assert secret not in str(raised.value)
    assert "<redacted>" in str(raised.value)


def test_sync_request_retries_429_then_succeeds():
    busy = HTTPError("https://api.example/v1/models", 429, "Too Many Requests", hdrs=None, fp=BytesIO(b"busy"))
    ok = MagicMock()
    ok.getcode.return_value = 200
    ok.read.return_value = b'{"data":[]}'
    ok.__enter__.return_value = ok
    ok.__exit__.return_value = False
    with (
        patch("plugin.framework.client.requests.wait_abortable", return_value=True),
        patch("plugin.framework.client.requests.urlopen", side_effect=[busy, ok]),
    ):
        result = sync_request("https://api.example/v1/models", timeout=1)
    assert result == {"data": []}
