# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""LibrePy bundle: smolagents slim init and =PY formula namespace checks."""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.librepy_bundle_paths import LIBREPY_SMOLAGENTS_INIT, slim_librepy_smolagents_init  # noqa: E402


def test_slim_librepy_smolagents_init_replaces_init(tmp_path):
    pkg = tmp_path / "plugin" / "contrib" / "smolagents"
    pkg.mkdir(parents=True)
    init_path = pkg / "__init__.py"
    init_path.write_text("from .agents import *\n", encoding="utf-8")
    slim_librepy_smolagents_init(str(tmp_path / "plugin"))
    assert init_path.read_text(encoding="utf-8") == LIBREPY_SMOLAGENTS_INIT
