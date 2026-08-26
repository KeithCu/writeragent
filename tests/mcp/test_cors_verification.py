# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""deal / CrossHair / Hypothesis verification for MCP CORS pure helpers."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from plugin.mcp.cors import (
    is_private_browser_origin,
    is_safe_origin,
    merge_allow_headers,
    normalize_cors_origin,
    normalize_origins_list,
    set_allow_private_origins,
    set_extra_allowed_origins,
)
from plugin.framework.deal_shim import DEAL_MAX_ORIGIN
from tests.strip_bundle import deal_pre_present
from tests.vhs_budget import vhs_max_examples
from scripts.crosshair_stream import cover_fqns_for_module

CROSSHAIR_MODULE = "plugin/mcp/cors.py"
_CROSSHAIR_ERROR_RE = re.compile(r": error:")

_origin_candidates = st.one_of(
    st.none(),
    st.just(""),
    st.just("   "),
    st.just("localai.local"),
    st.sampled_from(
        [
            "https://localai.local",
            "https://localai.local/",
            "http://127.0.0.1:3000",
            "http://localhost",
            "https://evil.com",
            "ftp://x.com",
        ]
    ),
    st.text(max_size=40),
)

# Public hosts that must never pass is_safe_origin when private/extra allowlists are off.
_PUBLIC_HOSTS = st.sampled_from(
    [
        "evil.com",
        "example.org",
        "attacker.net",
        "google.com",
        "cdn.example.com",
        "api.github.com",
    ]
)
_PUBLIC_SCHEMES = st.sampled_from(("http", "https"))
_PUBLIC_PORTS = st.one_of(st.just(""), st.sampled_from((":80", ":443", ":8080", ":3000")))


def _find_crosshair() -> str | None:
    crosshair_path = shutil.which("crosshair")
    if crosshair_path:
        return crosshair_path
    venv_bin_ch = Path(".venv/bin/crosshair")
    if venv_bin_ch.exists():
        return str(venv_bin_ch)
    return None


def setup_function() -> None:
    set_extra_allowed_origins([])
    set_allow_private_origins(True)


def teardown_function() -> None:
    set_extra_allowed_origins([])
    set_allow_private_origins(True)


@given(value=_origin_candidates)
@settings(max_examples=vhs_max_examples(80, 800), deadline=None)
def test_hypothesis_normalize_cors_origin_shape(value) -> None:
    result = normalize_cors_origin(value)
    if result is None:
        return
    assert result.startswith(("http://", "https://", "HTTP://", "HTTPS://")) or result.lower().startswith(("http://", "https://"))
    assert not result.endswith("/")


@given(
    value=st.one_of(
        st.none(),
        st.just("https://a.com/"),
        st.lists(st.sampled_from(["https://a.com", "https://a.com/", "https://b.com", 1, None]), max_size=5),
        st.just(123),
    )
)
@settings(max_examples=vhs_max_examples(60, 600), deadline=None)
def test_hypothesis_normalize_origins_list_idempotent(value) -> None:
    once = normalize_origins_list(value)
    twice = normalize_origins_list(once)
    assert twice == once
    assert len(once) == len(set(once))


@given(scheme=_PUBLIC_SCHEMES, host=_PUBLIC_HOSTS, port=_PUBLIC_PORTS)
@settings(max_examples=vhs_max_examples(40, 400), deadline=None)
def test_hypothesis_public_origins_unsafe_when_allowlists_off(scheme: str, host: str, port: str) -> None:
    """Phase 8 #2: arbitrary public web origins must not bypass CORS when private/extra are off."""
    set_extra_allowed_origins([])
    set_allow_private_origins(False)
    origin = f"{scheme}://{host}{port}"
    assert is_safe_origin(origin) is False
    assert is_safe_origin(origin + "/") is False


def test_localhost_safe_origin() -> None:
    assert is_safe_origin("http://localhost:3000")
    assert is_safe_origin("http://127.0.0.1")
    assert is_safe_origin("http://[::1]")
    assert is_safe_origin("http://[::1]:3000")
    assert is_private_browser_origin("http://192.168.1.50:3000") is True


def test_normalize_origins_list_stays_on_check_all() -> None:
    """Nested unique-length ensure is inverse_ensure; the FQN itself stays analyzed."""
    fqns = cover_fqns_for_module(Path("plugin/mcp/cors.py"), require_deal=True)
    assert any(f.endswith(".normalize_origins_list") for f in fqns)
    assert any(f.endswith(".is_safe_origin") for f in fqns)
    assert any(f.endswith(".is_private_browser_origin") for f in fqns)
    assert any(f.endswith(".merge_allow_headers") for f in fqns)


def test_cors_origin_overflow_pre_fails_closed() -> None:
    import deal

    if not deal_pre_present(is_safe_origin):
        pytest.skip("@deal.pre stripped in release bundle")
    too_long = "h" * (DEAL_MAX_ORIGIN + 1)
    with pytest.raises(deal.PreContractError):
        is_safe_origin(too_long)
    with pytest.raises(deal.PreContractError):
        normalize_cors_origin(too_long)
    with pytest.raises(deal.PreContractError):
        merge_allow_headers(too_long)
    with pytest.raises(deal.PreContractError):
        is_safe_origin("http://example.com/?x")
    with pytest.raises(deal.PreContractError):
        merge_allow_headers("Content-Type; X-Evil")
    # Pytest keeps the 256-char product domain.
    assert is_safe_origin("http://localhost:3000") is True
    assert "Content-Type" in merge_allow_headers("X-Custom")


@pytest.mark.slow
def test_crosshair_cors_if_available() -> None:
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
