# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""deal / Hypothesis / CrossHair verification for response_normalizers wire helpers."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from plugin.framework.client.response_normalizers import (
    _CHAT_TEMPLATE_CONTROL_TOKEN_RE,
    _DATA_URI_IMAGE_RE,
    extract_and_strip_images_from_message,
    strip_leaked_chat_template_control_tokens,
)

_CROSSHAIR_ERROR_RE = re.compile(r": error:")
_CROSSHAIR_TARGETS = (
    "plugin.framework.client.response_normalizers.strip_leaked_chat_template_control_tokens",
    "plugin.framework.client.response_normalizers.extract_and_strip_images_from_message",
)


def _find_crosshair() -> str | None:
    crosshair_path = shutil.which("crosshair")
    if crosshair_path:
        return crosshair_path
    venv_bin_ch = Path(".venv/bin/crosshair")
    if venv_bin_ch.exists():
        return str(venv_bin_ch)
    return None


@given(text=st.text(max_size=80))
@settings(max_examples=80)
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
@settings(max_examples=40)
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


def test_strip_none_and_empty() -> None:
    assert strip_leaked_chat_template_control_tokens(None) == ""
    assert strip_leaked_chat_template_control_tokens("") == ""
    assert strip_leaked_chat_template_control_tokens("   ") == ""


def test_extract_structured_image_url_block() -> None:
    msg = {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "see"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc123"}},
        ],
    }
    extracted = extract_and_strip_images_from_message(msg, strip_structured_image_blocks=True)
    assert len(extracted) == 1
    assert extracted[0] == {"mime_type": "image/png", "data": "abc123"}
    assert msg["content"] == [
        {"type": "text", "text": "see"},
        {"type": "text", "text": "[Image Ref]"},
    ]


def test_extract_keeps_structured_block_when_not_stripping() -> None:
    block = {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc123"}}
    msg = {"role": "user", "content": [block]}
    extracted = extract_and_strip_images_from_message(msg, strip_structured_image_blocks=False)
    assert extracted == []
    assert msg["content"] == [block]


@pytest.mark.slow
@pytest.mark.parametrize("target", _CROSSHAIR_TARGETS)
def test_crosshair_response_normalizers_fqn_if_available(target: str) -> None:
    crosshair_path = _find_crosshair()
    if not crosshair_path:
        pytest.skip("CrossHair concolic execution engine is not installed.")
    result = subprocess.run(
        [crosshair_path, "check", "-v", "--report_all", target],
        capture_output=True,
        text=True,
        timeout=300,
    )
    combined = f"{result.stdout}\n{result.stderr}".strip()
    print(f"CrossHair output ({target}):\n{combined}")
    errors = [line for line in combined.splitlines() if _CROSSHAIR_ERROR_RE.search(line)]
    assert not errors, "CrossHair counterexamples found:\n" + "\n".join(errors)
    if result.returncode == 2:
        pytest.fail(f"CrossHair internal error (exit 2):\n{combined}")
