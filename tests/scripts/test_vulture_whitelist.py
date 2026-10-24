# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for repo-local Vulture whitelist and pyproject configuration."""

from __future__ import annotations

import ast
from pathlib import Path
import tomllib

PROJECT_ROOT = Path(__file__).resolve().parents[2]
WHITELIST_PATH = PROJECT_ROOT / "vulture_whitelist.py"
PYPROJECT_PATH = PROJECT_ROOT / "pyproject.toml"
MAKEFILE_PATH = PROJECT_ROOT / "Makefile"


def test_vulture_whitelist_syntax_and_structure() -> None:
    """Verify vulture_whitelist.py exists, parses cleanly, and defines mock helper."""
    assert WHITELIST_PATH.is_file(), "vulture_whitelist.py must exist in repo root"

    content = WHITELIST_PATH.read_text(encoding="utf-8")
    tree = ast.parse(content, filename=str(WHITELIST_PATH))
    assert tree.body, "vulture_whitelist.py must not be empty"

    # Verify mock class or attribute assignments are present
    class_names = [n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
    assert "_WhitelistMock" in class_names, "_WhitelistMock must be defined in vulture_whitelist.py"


def test_pyproject_vulture_configuration() -> None:
    """Verify pyproject.toml defines [tool.vulture] with required paths and exclusions."""
    assert PYPROJECT_PATH.is_file()

    with open(PYPROJECT_PATH, "rb") as f:
        data = tomllib.load(f)

    vulture_cfg = data.get("tool", {}).get("vulture", {})
    assert vulture_cfg, "pyproject.toml must contain [tool.vulture]"

    paths = vulture_cfg.get("paths", [])
    assert "plugin" in paths, "vulture paths must include 'plugin'"
    assert "vulture_whitelist.py" in paths, "vulture paths must include 'vulture_whitelist.py'"

    exclude = vulture_cfg.get("exclude", [])
    assert any("writeragent_api.py" in pattern for pattern in exclude), (
        "vulture exclude must omit writeragent_api.py"
    )


def test_makefile_vulture_target() -> None:
    """Verify Makefile wires in vulture target."""
    text = MAKEFILE_PATH.read_text(encoding="utf-8")
    assert "vulture:" in text, "Makefile must define a vulture target"
