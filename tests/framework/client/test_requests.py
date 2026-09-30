"""sync_request requires an explicit timeout (no silent 10s default)."""

import inspect
import logging
from io import BytesIO
from unittest.mock import patch
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
    assert secret in str(raised.value)
