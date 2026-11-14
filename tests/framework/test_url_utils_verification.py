# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""deal / CrossHair / Hypothesis verification for url_utils.

CrossHair marked slow (excluded from default ``make test``).
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from plugin.framework.url_utils import (
    get_api_version_suffix,
    get_url_hostname,
    normalize_endpoint_url,
)
from tests.harness.crosshair import assert_crosshair_clean

CROSSHAIR_MODULE = "plugin/framework/url_utils.py"

_urls = st.one_of(
    st.just(""),
    st.just("   "),
    st.just(None),
    st.from_regex(r"https://[a-z0-9.-]{1,20}\.com(/[a-z0-9]{0,8}){0,3}", fullmatch=True),
    st.sampled_from(
        [
            "https://api.example.com/v1",
            "https://api.example.com/v1/",
            "https://openrouter.ai/api/v1",
            "https://api.z.ai/v4",
            "https://api.z.ai/api/paas/v4",
            "http://localhost:3000/api",
            "http://localhost:3000/api/v1",
            "http://localhost:3000/v1",
            "http://localhost:3000",
        ]
    ),
)

# OpenWebUI paste shapes: bare host plus common mistaken suffixes.
_owu_urls = st.sampled_from(
    [
        "http://localhost:3000",
        "http://localhost:3000/",
        "http://localhost:3000/api",
        "http://localhost:3000/api/",
        "http://localhost:3000/api/v1",
        "http://localhost:3000/api/v1/",
        "http://localhost:3000/v1",
        "http://127.0.0.1:8080/api/v1",
        "https://owu.example.com/api",
    ]
)


def test_normalize_empty_and_non_str() -> None:
    assert normalize_endpoint_url("") == ""
    assert normalize_endpoint_url("  ") == ""
    assert normalize_endpoint_url(None) == ""  # type: ignore[arg-type]


def test_url_helpers_long_and_odd_do_not_precontract() -> None:
    from plugin.framework.deal_shim import DEAL_MAX_URL
    from plugin.framework.url_utils import get_url_hostname, is_pdf_url

    too_long = "https://example.com/" + ("a" * DEAL_MAX_URL)
    assert normalize_endpoint_url(too_long).startswith("https://example.com/")
    assert get_url_hostname(too_long) == "example.com"
    assert is_pdf_url(too_long) is False
    assert normalize_endpoint_url(1) == ""  # type: ignore[arg-type]


@given(url=_urls)
@settings(max_examples=80)
def test_hypothesis_normalize_idempotent(url) -> None:
    once = normalize_endpoint_url(url, is_openwebui=False)
    twice = normalize_endpoint_url(once, is_openwebui=False)
    assert twice == once
    assert isinstance(once, str)
    if not url or not isinstance(url, str) or not str(url).strip():
        assert once == ""


@given(url=_owu_urls)
@settings(max_examples=40)
def test_hypothesis_openwebui_normalize_idempotent(url: str) -> None:
    once = normalize_endpoint_url(url, is_openwebui=True)
    twice = normalize_endpoint_url(once, is_openwebui=True)
    assert twice == once
    assert not once.lower().endswith(("/api", "/api/v1", "/v1"))


@given(
    url=st.one_of(
        st.just(""),
        st.text(alphabet=st.characters(blacklist_categories=("Cs",), max_codepoint=127), max_size=40),
        st.sampled_from(["https://api.z.ai", "http://x"]),
    )
)
@settings(max_examples=50)
def test_hypothesis_api_suffix_shape(url: str) -> None:
    suffix = get_api_version_suffix(url)
    assert suffix.startswith("/")
    assert isinstance(get_url_hostname(url if url else "https://example.com"), str)


@pytest.mark.slow
def test_crosshair_url_utils_if_available() -> None:
    assert_crosshair_clean(CROSSHAIR_MODULE, timeout=600)
