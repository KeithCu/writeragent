# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""deal / Hypothesis / CrossHair verification for response_normalizers wire helpers."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from plugin.framework.client.response_normalizers import (
    _CHAT_TEMPLATE_CONTROL_TOKEN_RE,
    _DATA_URI_IMAGE_RE,
    extract_and_strip_images_from_message,
    normalize_multimodal_messages,
    strip_leaked_chat_template_control_tokens,
)
from tests.harness.crosshair import assert_crosshair_clean
from tests.harness.vhs_budget import vhs_max_examples

_CROSSHAIR_TARGETS = (
    "plugin.framework.client.response_normalizers.strip_leaked_chat_template_control_tokens",
    "plugin.framework.client.response_normalizers.extract_and_strip_images_from_message",
)


@given(text=st.text(max_size=80))
@settings(max_examples=vhs_max_examples(80, 800), deadline=None)
def test_hypothesis_strip_control_tokens_removes_all_matches(text: str) -> None:
    out = strip_leaked_chat_template_control_tokens(text)
    assert isinstance(out, str)
    assert _CHAT_TEMPLATE_CONTROL_TOKEN_RE.search(out) is None
    assert len(out) <= len(text)


@given(
    prefix=st.text(max_size=20).filter(lambda s: "data:image" not in s),
    ext=st.sampled_from(["png", "jpeg", "webp"]),
    b64=st.text(alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=", min_size=1, max_size=40),
    # Suffix must start outside the base64 alphabet so the greedy URI regex stops at b64.
    suffix=st.text(max_size=20).map(lambda s: "!" + s).filter(lambda s: "data:image" not in s),
)
@settings(max_examples=vhs_max_examples(40, 400), deadline=None)
def test_hypothesis_extract_images_from_string_content(prefix: str, ext: str, b64: str, suffix: str) -> None:
    uri = f"data:image/{ext};base64,{b64}"
    msg = {"role": "user", "content": f"{prefix}{uri}{suffix}"}
    extracted = extract_and_strip_images_from_message(msg)
    assert len(extracted) == 1
    assert extracted[0]["mime_type"] == f"image/{ext}"
    assert extracted[0]["data"] == "".join(b64.split())
    assert isinstance(msg["content"], str)
    assert _DATA_URI_IMAGE_RE.search(msg["content"]) is None
    assert "[Image Ref]" in msg["content"]


def _image_url_count(messages: list) -> int:
    n = 0
    for m in messages:
        c = m.get("content")
        if isinstance(c, list):
            n += sum(1 for p in c if isinstance(p, dict) and p.get("type") == "image_url")
    return n


@given(
    ext=st.sampled_from(["png", "jpeg"]),
    b64=st.text(alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/", min_size=4, max_size=24),
    provider=st.sampled_from(["openai", "anthropic", "google"]),
)
@settings(max_examples=vhs_max_examples(30, 300), deadline=None)
def test_hypothesis_normalize_multimodal_preserves_images(ext: str, b64: str, provider: str) -> None:
    # Trailing "!" is outside the URI alphabet so the greedy base64 group stops (same as extract oracle).
    uri = f"data:image/{ext};base64,{b64}"
    messages = [
        {"role": "user", "content": "look"},
        {"role": "assistant", "content": f"here {uri}!"},
    ]
    normalize_multimodal_messages(messages, provider)
    assert _image_url_count(messages) >= 1
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            assert _DATA_URI_IMAGE_RE.search(c) is None


@pytest.mark.slow
@pytest.mark.parametrize("target", _CROSSHAIR_TARGETS)
def test_crosshair_response_normalizers_fqn_if_available(target: str) -> None:
    assert_crosshair_clean(target)
