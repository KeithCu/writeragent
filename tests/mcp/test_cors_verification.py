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
    normalize_cors_origin,
    normalize_origins_list,
    set_allow_private_origins,
    set_extra_allowed_origins,
)
from tests.harness.vhs_budget import vhs_max_examples

CROSSHAIR_MODULE = "plugin/mcp/cors.py"
_CROSSHAIR_ERROR_RE = re.compile(r": error:")

_VALID_ORIGINS = st.builds(
    lambda scheme, host, port, path: f"{scheme}://{host}{port}{path}",
    scheme=st.sampled_from(("http", "https", "HTTP", "HTTPS")),
    host=st.sampled_from(("localai.local", "127.0.0.1", "localhost", "App.Example.COM")),
    port=st.sampled_from(("", ":3000", ":8080")),
    path=st.sampled_from(("", "/", "/subpath")),
)


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
