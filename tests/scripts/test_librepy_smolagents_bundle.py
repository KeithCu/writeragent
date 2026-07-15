# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""LibrePy bundle: smolagents slim init and =PY formula namespace checks."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.librepy_bundle_paths import LIBREPY_SMOLAGENTS_INIT, slim_librepy_smolagents_init  # noqa: E402

_ODS_SAMPLE = _REPO_ROOT / "testnewnamespace.ods"
_BUNDLE = _REPO_ROOT / "build" / "bundle-librepy"


def test_librepy_smolagents_init_does_not_import_agents():
    init_path = _BUNDLE / "plugin" / "contrib" / "smolagents" / "__init__.py"
    if not init_path.is_file():
        locales = _REPO_ROOT / "build" / "generated" / "locales"
        # build_librepy_oxt requires compiled locales; locales need gettext.
        if not locales.is_dir() and (shutil.which("xgettext") is None or shutil.which("msgfmt") is None):
            pytest.skip("LibrePy bundle missing and gettext unavailable to build it")
        subprocess.run(
            [sys.executable, str(_REPO_ROOT / "scripts" / "build_librepy_oxt.py")],
            cwd=_REPO_ROOT,
            check=True,
        )
    text = init_path.read_text(encoding="utf-8")
    assert "from .agents import" not in text
    assert "local_python_executor only" in text


def test_slim_librepy_smolagents_init_replaces_init(tmp_path):
    pkg = tmp_path / "plugin" / "contrib" / "smolagents"
    pkg.mkdir(parents=True)
    init_path = pkg / "__init__.py"
    init_path.write_text("from .agents import *\n", encoding="utf-8")
    slim_librepy_smolagents_init(str(tmp_path / "plugin"))
    assert init_path.read_text(encoding="utf-8") == LIBREPY_SMOLAGENTS_INIT


def test_librepy_bundle_includes_sandbox_cache():
    from scripts.librepy_bundle_paths import collect_librepy_plugin_paths

    paths = collect_librepy_plugin_paths(str(_REPO_ROOT))
    assert "plugin/scripting/sandbox_cache.py" in paths


def test_testnewnamespace_ods_uses_writeragent_pythonfunction_prefix():
    if not _ODS_SAMPLE.is_file():
        return
    with zipfile.ZipFile(_ODS_SAMPLE) as zf:
        content = zf.read("content.xml").decode("utf-8")
    assert "ORG.EXTENSION.WRITERAGENT.PYTHONFUNCTION.PY" in content.upper()
    assert "ORG.EXTENSION.LIBREPY.PYTHONFUNCTION" not in content.upper()
