# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import os

from plugin.scripting.editor_launcher import _ASSETS_DIR


def test_monaco_index_html_lives_under_assets_not_scripting_dir():
    """pywebview treats relative URLs as relative to dirname(sys.argv[0]) (scripting/)."""
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    scripting_dir = os.path.join(repo_root, "plugin", "scripting")
    wrong = os.path.join(scripting_dir, "index.html")
    right = os.path.join(_ASSETS_DIR, "index.html")
    assert not os.path.isfile(wrong)
    assert os.path.isfile(right)
