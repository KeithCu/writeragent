# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""deal / CrossHair / Hypothesis verification for url_utils.

CrossHair marked slow (excluded from default ``make test``).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from plugin.framework.url_utils import (
    get_api_version_suffix,
    get_url_hostname,
    normalize_endpoint_url,
)

CROSSHAIR_MODULE = "plugin/framework/url_utils.py"
_CROSSHAIR_ERROR_RE = re.compile(r": error:")

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


def _find_crosshair() -> str | None:
    crosshair_path = shutil.which("crosshair")
    if crosshair_path:
        return crosshair_path
    venv_bin_ch = Path(".venv/bin/crosshair")
    if venv_bin_ch.exists():
        return str(venv_bin_ch)
    return None


def test_normalize_empty_and_non_str() -> None:
    assert normalize_endpoint_url("") == ""
    assert normalize_endpoint_url("  ") == ""
    assert normalize_endpoint_url(None) == ""  # type: ignore[arg-type]


def test_url_helpers_overflow_pre_fails_closed() -> None:
    from plugin.framework.deal_shim import DEAL_MAX_URL
    from plugin.framework.url_utils import get_url_hostname, is_pdf_url
    from tests.strip_bundle import deal_pre_present

    import deal

    if not deal_pre_present(normalize_endpoint_url):
        pytest.skip("@deal.pre stripped in release bundle")
    too_long = "https://example.com/" + ("a" * DEAL_MAX_URL)
    with pytest.raises(deal.PreContractError):
        normalize_endpoint_url(too_long)
    with pytest.raises(deal.PreContractError):
        get_url_hostname(too_long)
    with pytest.raises(deal.PreContractError):
        is_pdf_url(too_long)
    with pytest.raises(deal.PreContractError):
        normalize_endpoint_url(1)  # type: ignore[arg-type]


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
    crosshair_path = _find_crosshair()
    if not crosshair_path:
        pytest.skip("CrossHair concolic execution engine is not installed.")

    result = subprocess.run(
        [crosshair_path, "check", "-v", "--report_all", CROSSHAIR_MODULE],
        capture_output=True,
        text=True,
        timeout=600,
    )
    combined = f"{result.stdout}\n{result.stderr}".strip()
    print(f"CrossHair output:\n{combined}")
    errors = [line for line in combined.splitlines() if _CROSSHAIR_ERROR_RE.search(line)]
    assert not errors, "CrossHair counterexamples found:\n" + "\n".join(errors)
    if result.returncode == 2:
        pytest.fail(f"CrossHair internal error (exit 2):\n{combined}")
