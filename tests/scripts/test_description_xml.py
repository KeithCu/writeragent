# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Structural tests for Extension Manager icons in description.xml.tpl."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from PIL import Image

_DESC_NS = "http://openoffice.org/extensions/description/2006"
_XLINK_HREF = "{http://www.w3.org/1999/xlink}href"

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ASSETS_DIR = _REPO_ROOT / "extension" / "assets"

_TEMPLATES = [
    pytest.param(_REPO_ROOT / "extension" / "description.xml.tpl", id="writeragent"),
    pytest.param(_REPO_ROOT / "extension-core" / "description.xml.tpl", id="librepy"),
    pytest.param(_REPO_ROOT / "extension-harper" / "description.xml.tpl", id="libreharper"),
]


@pytest.mark.parametrize("tpl_path", _TEMPLATES)
def test_extension_manager_icon_matches_drawrow_aspect(tpl_path: Path) -> None:
    root = ET.parse(tpl_path).getroot()
    default_el = root.find(f"{{{_DESC_NS}}}icon/{{{_DESC_NS}}}default")
    assert default_el is not None, f"Missing <icon><default> in {tpl_path}"
    href = default_el.get(_XLINK_HREF) or ""
    assert href.startswith("assets/"), f"Unexpected icon href {href!r} in {tpl_path}"

    icon_path = _ASSETS_DIR / href.removeprefix("assets/")
    assert icon_path.is_file(), f"Missing Extension Manager icon {icon_path}"

    with Image.open(icon_path) as im:
        w, h = im.size
        assert w * 42 == h * 47, f"{icon_path} size {im.size} does not match 47:42 ratio"
        assert w >= 47
        rgba = im.convert("RGBA")
        assert rgba.getpixel((0, 0))[3] == 0
