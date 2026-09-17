"""sync_request requires an explicit timeout (no silent 10s default)."""

import inspect

import pytest

from plugin.framework.client.requests import sync_request


def test_sync_request_timeout_is_required_keyword():
    params = inspect.signature(sync_request).parameters
    timeout = params["timeout"]
    assert timeout.default is inspect.Parameter.empty
    assert timeout.kind is inspect.Parameter.KEYWORD_ONLY


def test_sync_request_without_timeout_raises_typeerror():
    with pytest.raises(TypeError, match="timeout"):
        sync_request("https://example.invalid")
