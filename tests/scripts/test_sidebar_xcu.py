# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Structural tests for Sidebar.xcu deck icon dimensions.

LibreOffice's sidebar deck slot is 24 px and VCL draws URL-loaded bitmaps at
their own pixel size, so a larger asset overflows the tab bar (issue 3710
follow-up).
"""

from __future__ import annotations

import struct
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

_OOR_NS = "http://openoffice.org/2001/registry"
_OOR_NAME = f"{{{_OOR_NS}}}name"

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ASSETS_DIR = _REPO_ROOT / "extension" / "assets"

_SIDEBAR_XCUS = [
    pytest.param(
        _REPO_ROOT / "extension" / "registry" / "org" / "openoffice" / "Office" / "UI" / "Sidebar.xcu",
        "vnd.sun.star.extension://org.extension.writeragent/assets/",
        id="writeragent",
    ),
    pytest.param(
        _REPO_ROOT / "extension-core" / "registry" / "org" / "openoffice" / "Office" / "UI" / "Sidebar.xcu",
        "vnd.sun.star.extension://org.extension.librepy/assets/",
        id="librepy",
    ),
]


def _deck_icon_urls(xcu_path: Path) -> list[tuple[str, str]]:
    root = ET.parse(xcu_path).getroot()
    deck_list = next((n for n in root.iter("node") if n.get(_OOR_NAME) == "DeckList"), None)
    assert deck_list is not None, f"No DeckList in {xcu_path}"
    urls: list[tuple[str, str]] = []
    for deck in deck_list.findall("node"):
        deck_name = deck.get(_OOR_NAME) or ""
        for prop in deck.findall("prop"):
            if prop.get(_OOR_NAME) == "IconURL":
                value = prop.find("value")
                assert value is not None and value.text, f"{deck_name} missing IconURL value"
                urls.append((deck_name, value.text.strip()))
    return urls


def _png_size(path: Path) -> tuple[int, int]:
    header = path.read_bytes()[:24]
    assert header[:8] == b"\x89PNG\r\n\x1a\n", f"{path} is not a PNG"
    assert header[12:16] == b"IHDR", f"{path} missing IHDR chunk"
    return struct.unpack(">II", header[16:24])


@pytest.mark.parametrize(("xcu_path", "url_prefix"), _SIDEBAR_XCUS)
def test_sidebar_deck_icons_are_24x24(xcu_path: Path, url_prefix: str) -> None:
    urls = _deck_icon_urls(xcu_path)
    assert urls, f"No decks found in {xcu_path}"
    for deck_name, url in urls:
        assert url.startswith(url_prefix), f"{deck_name}: unexpected IconURL {url!r}"
        rel_asset = url[len(url_prefix) :]
        asset_path = _ASSETS_DIR / rel_asset
        assert asset_path.is_file(), f"{deck_name}: missing asset {asset_path}"
        width, height = _png_size(asset_path)
        assert (width, height) == (24, 24), f"{deck_name}: {asset_path} is {width}x{height}, expected 24x24"
