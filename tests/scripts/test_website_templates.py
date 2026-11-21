# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify every url_for('static', filename=...) in website templates exists."""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATES_DIR = _REPO_ROOT / "website" / "templates"
_STATIC_DIR = _REPO_ROOT / "website" / "static"

_STATIC_RE = re.compile(r"""url_for\(\s*['"]static['"]\s*,\s*filename\s*=\s*['"]([^'"]+)['"]\s*\)""")
# Optional screenshots documented in website/README.md (not tracked in git).
_OPTIONAL_STATIC = {"images/Sonnet46Spreadsheet.png", "images/Sonnet46ArchDiagram.jpg"}


def test_website_template_static_files_exist() -> None:
    templates = sorted(_TEMPLATES_DIR.glob("*.html"))
    assert templates
    found = 0
    for tpl in templates:
        text = tpl.read_text(encoding="utf-8")
        for match in _STATIC_RE.finditer(text):
            rel = match.group(1)
            if rel in _OPTIONAL_STATIC:
                continue
            found += 1
            assert (_STATIC_DIR / rel).is_file(), f"{tpl.name} references missing static/{rel}"
    assert found > 0
