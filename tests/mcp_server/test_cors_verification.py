# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""deal / CrossHair / Hypothesis verification for MCP CORS pure helpers."""

from __future__ import annotations


import pytest

from tests.harness.crosshair import assert_crosshair_clean
from hypothesis import given, settings
from hypothesis import strategies as st

from plugin.mcp_server.cors import (
    normalize_cors_origin,
    normalize_origins_list,
    set_allow_private_origins,
    set_extra_allowed_origins,
)
from tests.harness.vhs_budget import vhs_max_examples

CROSSHAIR_MODULE = "plugin/mcp_server/cors.py"
_VALID_ORIGINS = st.builds(
    lambda scheme, host, port, path: f"{scheme}://{host}{port}{path}",
    scheme=st.sampled_from(("http", "https", "HTTP", "HTTPS")),
    host=st.sampled_from(("localai.local", "127.0.0.1", "localhost", "App.Example.COM")),
    port=st.sampled_from(("", ":3000", ":8080")),
    path=st.sampled_from(("", "/", "/subpath")),
)


def setup_function() -> None:
    set_extra_allowed_origins([])
    set_allow_private_origins(True)


def teardown_function() -> None:
    set_extra_allowed_origins([])
    set_allow_private_origins(True)


@given(origins=st.lists(_VALID_ORIGINS, min_size=1, max_size=5))
@settings(max_examples=vhs_max_examples(60, 600), deadline=None)
def test_hypothesis_normalize_cors_origin_and_list(origins: list[str]) -> None:
    """Valid http/https origins normalize to non-None canonical strings and dedupe idempotently."""
    for origin in origins:
        result = normalize_cors_origin(origin)
        assert result is not None
        assert result.startswith(("http://", "https://"))
        assert not result.endswith("/")
        assert normalize_cors_origin(result) == result

    once = normalize_origins_list(origins)
    assert len(once) >= 1
    assert normalize_origins_list(once) == once
    assert len(once) == len(set(once))


@pytest.mark.slow
def test_crosshair_cors_if_available() -> None:
    assert_crosshair_clean(CROSSHAIR_MODULE, timeout=600)
