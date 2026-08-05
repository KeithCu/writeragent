# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""deal / Hypothesis / CrossHair (FQN) for sandbox.scrub_subprocess_env."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from plugin.scripting.sandbox import scrub_subprocess_env

_CROSSHAIR_ERROR_RE = re.compile(r": error:")
_CROSSHAIR_TARGET = "plugin.scripting.sandbox.scrub_subprocess_env"

_BLOCKED_SUBSTR = ("KEY", "TOKEN", "SECRET", "PASSWORD", "AUTH", "CREDENTIAL")
_BLOCKED_EXACT = {"PYTHONHOME", "PYTHONPATH", "LD_LIBRARY_PATH"}


def _find_crosshair() -> str | None:
    crosshair_path = shutil.which("crosshair")
    if crosshair_path:
        return crosshair_path
    venv_bin_ch = Path(".venv/bin/crosshair")
    if venv_bin_ch.exists():
        return str(venv_bin_ch)
    return None


def _assert_scrubbed(out: dict[str, str]) -> None:
    assert isinstance(out, dict)
    for k, v in out.items():
        assert isinstance(k, str) and isinstance(v, str)
        ku = k.upper()
        assert ku not in _BLOCKED_EXACT
        assert not any(s in ku for s in _BLOCKED_SUBSTR)


def test_none_and_empty_return_empty() -> None:
    assert scrub_subprocess_env(None) == {}
    assert scrub_subprocess_env({}) == {}


def test_drops_secrets_and_lo_overrides() -> None:
    out = scrub_subprocess_env(
        {
            "PATH": "/usr/bin",
            "API_KEY": "secret",
            "OPENAI_TOKEN": "t",
            "PYTHONHOME": "/lo",
            "PYTHONPATH": "/lo/lib",
            "LD_LIBRARY_PATH": "/lo",
            "HOME": "/home/u",
        }
    )
    _assert_scrubbed(out)
    assert out["PATH"] == "/usr/bin"
    assert out["HOME"] == "/home/u"
    assert "API_KEY" not in out
    assert "PYTHONHOME" not in out
    assert out["PYTHONIOENCODING"] == "utf-8"
    assert out["PYTHONUTF8"] == "1"
    assert out["PYTHONDONTWRITEBYTECODE"] == "1"


@given(
    base=st.dictionaries(
        keys=st.sampled_from(
            [
                "PATH",
                "HOME",
                "LANG",
                "API_KEY",
                "SECRET_TOKEN",
                "PASSWORD",
                "AUTH_HEADER",
                "CREDENTIALS",
                "PYTHONHOME",
                "PYTHONPATH",
                "LD_LIBRARY_PATH",
                "MY_VAR",
            ]
        ),
        values=st.text(max_size=20),
        max_size=8,
    )
)
@settings(max_examples=80)
def test_hypothesis_scrub_invariants(base: dict[str, str]) -> None:
    out = scrub_subprocess_env(base)
    _assert_scrubbed(out)
    if base:
        assert out.get("PYTHONIOENCODING") == "utf-8"
        assert out.get("PYTHONUTF8") == "1"
        assert out.get("PYTHONDONTWRITEBYTECODE") == "1"


@pytest.mark.slow
def test_crosshair_scrub_subprocess_env_fqn_if_available() -> None:
    crosshair_path = _find_crosshair()
    if not crosshair_path:
        pytest.skip("CrossHair concolic execution engine is not installed.")
    result = subprocess.run(
        [crosshair_path, "check", "-v", "--report_all", _CROSSHAIR_TARGET],
        capture_output=True,
        text=True,
        timeout=300,
    )
    combined = f"{result.stdout}\n{result.stderr}".strip()
    print(f"CrossHair output:\n{combined}")
    errors = [line for line in combined.splitlines() if _CROSSHAIR_ERROR_RE.search(line)]
    assert not errors, "CrossHair counterexamples found:\n" + "\n".join(errors)
    if result.returncode == 2:
        pytest.fail(f"CrossHair internal error (exit 2):\n{combined}")
