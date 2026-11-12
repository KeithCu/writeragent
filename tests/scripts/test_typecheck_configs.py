# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Keep the fast vs full Basedpyright configs aligned so make release cannot hang."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PYPROJECT = REPO / "pyproject.toml"
FULL_JSON = REPO / "pyrightconfig.full.json"
MAKEFILE = REPO / "Makefile"

# Keys that must match between [tool.basedpyright] and pyrightconfig.full.json
# except useLibraryCodeForTypes (false daily, true on make typecheck-full).
_SYNC_KEYS = (
    "typeCheckingMode",
    "pythonVersion",
    "venvPath",
    "venv",
    "include",
    "exclude",
)


def _basedpyright_tool() -> dict:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    return data["tool"]["basedpyright"]


def _full_config() -> dict:
    # Missing this file made `basedpyright -p pyrightconfig.full.json` fall back
    # to default include (the whole checkout, including .venv) and hang.
    assert FULL_JSON.is_file(), "pyrightconfig.full.json is required for make typecheck-full"
    return json.loads(FULL_JSON.read_text(encoding="utf-8"))


def test_daily_basedpyright_skips_library_source() -> None:
    assert _basedpyright_tool()["useLibraryCodeForTypes"] is False


def test_full_basedpyright_walks_library_source() -> None:
    assert _full_config()["useLibraryCodeForTypes"] is True


def test_full_config_mirrors_pyproject_scope_and_rules() -> None:
    daily = _basedpyright_tool()
    full = _full_config()
    for key in _SYNC_KEYS:
        assert full[key] == daily[key], key
    report_keys = [k for k in daily if k.startswith("report")]
    assert report_keys, "expected report* keys in pyproject [tool.basedpyright]"
    for key in report_keys:
        assert full[key] == daily[key], key


def test_pyspector_skipped_on_windows_cpython_313() -> None:
    """Default uv sync must not compile pyspector's Rust sdist on Windows 3.13.

    PyPI 0.2.x has no cp313 win_amd64 wheel. Linking the sdist needs
    kernel32.lib, which is missing when the Windows SDK is not installed.
    """
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    dev = data["dependency-groups"]["dev"]
    specs = [item for item in dev if isinstance(item, str) and item.startswith("pyspector")]
    assert specs == ["pyspector>=0.2.1; sys_platform != 'win32' or python_version < '3.13'"]
    text = MAKEFILE.read_text(encoding="utf-8")
    assert "pyspector not installed — skipped" in text


def test_makefile_typecheck_full_uses_committed_json() -> None:
    text = MAKEFILE.read_text(encoding="utf-8")
    assert "basedpyright-full-run" in text
    assert "-p pyrightconfig.full.json" in text
    # Missing json used to hang; fail fast instead of scanning .venv.
    assert "pyrightconfig.full.json missing" in text
    assert "\t@$(MAKE) typecheck-full\n" in text
