# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""deal / Hypothesis / CrossHair verification for html_stripper and auth helpers."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

import deal
import pytest

from plugin.framework.deal_shim import DEAL_MAX_HTML_CHUNK
from plugin.framework.html_stripper import StreamingHTMLStripper, strip_html_tags
from plugin.framework.client.auth import (
    build_auth_headers,
    provider_requires_api_key,
    provider_requires_slug_model_id,
)
from tests.strip_bundle import deal_pre_present


@given(text=st.text(alphabet=st.characters(blacklist_categories=("Cs",), max_codepoint=127), max_size=DEAL_MAX_HTML_CHUNK))
@settings(max_examples=100)
def test_hypothesis_strip_html_tags_returns_string(text: str) -> None:
    res = strip_html_tags(text)
    assert isinstance(res, str)
    # Formatted tags should be removed or shortened
    if "<p>" in text and "</p>" in text:
        assert len(res) < len(text)


def test_strip_html_tags_streaming_equivalence() -> None:
    full_text = "<div class='test'>Hello <b>World</b>! 3 < 5</div>"
    s1 = strip_html_tags(full_text)
    
    stripper = StreamingHTMLStripper()
    chunk1 = stripper.feed("<div class='test'>Hello ")
    chunk2 = stripper.feed("<b>World</b>! 3 < 5")
    chunk3 = stripper.feed("</div>")
    final = stripper.finalize()
    s2 = chunk1 + chunk2 + chunk3 + final
    
    assert s1 == s2
    assert "Hello World! 3 < 5" in s1


def test_strip_html_tags_overflow_pre_fails_closed() -> None:
    if not deal_pre_present(strip_html_tags):
        pytest.skip("@deal.pre stripped in release bundle")
    with pytest.raises(deal.PreContractError):
        strip_html_tags("x" * (DEAL_MAX_HTML_CHUNK + 1))
    # Pytest 512 still reaches the 256-char tag-flush path.
    assert "<" in strip_html_tags("<" + ("a" * 256))


@given(provider=st.one_of(st.sampled_from(["openrouter", "openai", "anthropic", "ollama", "custom", "unknown"]), st.none()))
def test_provider_requires_api_key_contracts(provider: str | None) -> None:
    assert isinstance(provider_requires_api_key(provider), bool)
    assert isinstance(provider_requires_slug_model_id(provider), bool)


@given(api_key=st.text(min_size=1, max_size=50).map(lambda s: s.strip()).filter(lambda s: len(s) > 0), style=st.sampled_from(["bearer", "x-api-key", "none"]))
def test_build_auth_headers_contracts(api_key: str, style: str) -> None:
    auth_info = {"api_key": api_key, "header_style": style, "headers": {"custom-header": "value"}}
    headers = build_auth_headers(auth_info)
    assert isinstance(headers, dict)
    if style == "bearer":
        assert headers.get("Authorization") == f"Bearer {api_key}"
    elif style == "x-api-key":
        assert headers.get("x-api-key") == api_key
    assert headers.get("custom-header") == "value"
