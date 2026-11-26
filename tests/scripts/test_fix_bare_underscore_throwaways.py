# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""``--check`` mode of the bare ``_`` throwaway fixer that ``make typecheck`` runs."""

from __future__ import annotations

from pathlib import Path

from scripts.fix_bare_underscore_throwaways import main

_THROWAWAY = "for _ in range(3):\n    pass\n"


def _module(root: Path, source: str) -> Path:
    path = root / "plugin" / "mod.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


def test_check_fails_on_a_bare_underscore_and_leaves_the_file(tmp_path: Path) -> None:
    path = _module(tmp_path, _THROWAWAY)
    assert main(["--check", "--root", str(tmp_path)]) == 1
    assert path.read_text(encoding="utf-8") == _THROWAWAY


def test_check_passes_on_gettext_calls_and_named_throwaways(tmp_path: Path) -> None:
    _module(tmp_path, 'from plugin.framework.i18n import _\nfor _unused in range(3):\n    print(_("x"))\n')
    assert main(["--check", "--root", str(tmp_path)]) == 0
