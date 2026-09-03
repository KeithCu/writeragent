# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Structural tests for LibrePy extension-core/Addons.xcu menu Context parity."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

_OOR_NS = "http://openoffice.org/2001/registry"
_OOR_NAME = "{%s}name" % _OOR_NS

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ADDONS_XCU = _REPO_ROOT / "extension-core" / "Addons.xcu"

_FULL_CONTEXT = (
    "com.sun.star.sheet.SpreadsheetDocument,"
    "com.sun.star.text.GlobalDocument,"
    "com.sun.star.text.TextDocument,"
    "com.sun.star.text.WebDocument,"
    "com.sun.star.drawing.DrawingDocument,"
    "com.sun.star.presentation.PresentationDocument"
)

_CALC_SVC = "com.sun.star.sheet.SpreadsheetDocument"
_WRITER_SVC = "com.sun.star.text.TextDocument"


def _prop_text(node: ET.Element, prop_name: str) -> str | None:
    for prop in node.findall("prop"):
        if prop.get(_OOR_NAME) == prop_name:
            value = prop.find("value")
            if value is not None and value.text:
                return value.text.strip()
    return None


def _find_menubar(root: ET.Element) -> ET.Element:
    for node in root.iter("node"):
        if node.get(_OOR_NAME) == "org.extension.librepy.menubar":
            return node
    raise AssertionError("org.extension.librepy.menubar not found")


def _submenu_items(menubar: ET.Element) -> dict[str, ET.Element]:
    submenu = None
    for node in menubar.findall("node"):
        if node.get(_OOR_NAME) == "Submenu":
            submenu = node
            break
    assert submenu is not None, "Submenu node missing under LibrePy menubar"

    by_url: dict[str, ET.Element] = {}
    for item in submenu.findall("node"):
        url = _prop_text(item, "URL")
        if url:
            by_url[url] = item
    return by_url


def test_librepy_menubar_has_full_context():
    root = ET.parse(_ADDONS_XCU).getroot()
    menubar = _find_menubar(root)
    assert _prop_text(menubar, "Context") == _FULL_CONTEXT


def test_librepy_shared_items_have_explicit_full_context():
    root = ET.parse(_ADDONS_XCU).getroot()
    items = _submenu_items(_find_menubar(root))
    shared_urls = (
        "org.extension.librepy:main.settings",
        "org.extension.librepy:main.report_bug",
        "org.extension.librepy:scripting.run_python_dialog",
        "org.extension.librepy:vision.open_settings",
        "org.extension.librepy:scripting.reset_python_session",
    )
    for url in shared_urls:
        assert url in items, f"missing menu item {url}"
        assert _prop_text(items[url], "Context") == _FULL_CONTEXT, url


def test_librepy_calc_items_include_spreadsheet_context():
    root = ET.parse(_ADDONS_XCU).getroot()
    items = _submenu_items(_find_menubar(root))
    calc_urls = (
        "org.extension.librepy:main.settings",
        "org.extension.librepy:scripting.run_python_dialog",
        "org.extension.librepy:scripting.reset_python_session",
        "org.extension.librepy:scripting.edit_python_cell",
    )
    for url in calc_urls:
        assert url in items, f"missing menu item {url}"
        ctx = _prop_text(items[url], "Context")
        assert ctx is not None, f"{url} missing Context"
        assert _CALC_SVC in ctx, url


def test_librepy_edit_python_cell_is_calc_only():
    root = ET.parse(_ADDONS_XCU).getroot()
    items = _submenu_items(_find_menubar(root))
    url = "org.extension.librepy:scripting.edit_python_cell"
    assert url in items
    ctx = _prop_text(items[url], "Context")
    assert ctx == _CALC_SVC
    assert _WRITER_SVC not in (ctx or "")


def test_librepy_writer_only_items():
    root = ET.parse(_ADDONS_XCU).getroot()
    items = _submenu_items(_find_menubar(root))
    for url in (
        "org.extension.librepy:textanalytics.open_dialog",
        "org.extension.librepy:writer.insert_latex_dialog",
    ):
        assert url in items, f"missing menu item {url}"
        ctx = _prop_text(items[url], "Context")
        assert ctx == _WRITER_SVC, url
        assert _CALC_SVC not in (ctx or "")


def _submenu_node(menubar: ET.Element) -> ET.Element:
    return next(n for n in menubar.findall("node") if n.get(_OOR_NAME) == "Submenu")


def _ordered_urls(menubar: ET.Element) -> list[str]:
    urls = [_prop_text(item, "URL") for item in _submenu_node(menubar).findall("node")]
    return [u for u in urls if u]


def _ordered_names(menubar: ET.Element) -> list[str]:
    return [item.get(_OOR_NAME) or "" for item in _submenu_node(menubar).findall("node")]


def test_librepy_every_item_has_context():
    root = ET.parse(_ADDONS_XCU).getroot()
    for item in _submenu_node(_find_menubar(root)).findall("node"):
        name = item.get(_OOR_NAME)
        assert _prop_text(item, "Context"), f"{name} missing Context"


def test_librepy_node_names_are_sort_stable():
    root = ET.parse(_ADDONS_XCU).getroot()
    names = _ordered_names(_find_menubar(root))
    assert names == sorted(names)
    assert names[0].startswith("M0")


def test_librepy_notebook_run_is_not_a_menu_item():
    """Run All / From Here / Stop are hamburger-only when a notebook registry exists."""
    root = ET.parse(_ADDONS_XCU).getroot()
    items = _submenu_items(_find_menubar(root))
    for url in (
        "org.extension.librepy:notebook.run_all",
        "org.extension.librepy:notebook.run_from_here",
        "org.extension.librepy:notebook.stop",
    ):
        assert url not in items


def test_librepy_menu_order():
    root = ET.parse(_ADDONS_XCU).getroot()
    assert _ordered_urls(_find_menubar(root)) == [
        "org.extension.librepy:scripting.run_python_dialog",
        "org.extension.librepy:scripting.edit_python_cell",
        "org.extension.librepy:writer.insert_latex_dialog",
        "org.extension.librepy:textanalytics.open_dialog",
        "private:separator",
        "org.extension.librepy:main.settings",
        "org.extension.librepy:vision.open_settings",
        "org.extension.librepy:scripting.reset_python_session",
        "org.extension.librepy:main.report_bug",
    ]


def test_librepy_report_bug_is_last():
    root = ET.parse(_ADDONS_XCU).getroot()
    urls = _ordered_urls(_find_menubar(root))
    assert urls[-1] == "org.extension.librepy:main.report_bug"


def test_librepy_menu_order_matches_writeragent_python_cluster():
    wa_path = _REPO_ROOT / "extension" / "Addons.xcu"
    wa_root = ET.parse(wa_path).getroot()
    wa_menubar = None
    for node in wa_root.iter("node"):
        if node.get(_OOR_NAME) == "org.extension.writeragent.menubar":
            wa_menubar = node
            break
    assert wa_menubar is not None

    def _action(url: str) -> str:
        if url == "private:separator":
            return url
        return url.split(":", 1)[1]

    lp_actions = [
        _action(u) for u in _ordered_urls(_find_menubar(ET.parse(_ADDONS_XCU).getroot()))
        if u != "private:separator"
    ]
    wa_actions = [
        _action(u) for u in _ordered_urls(wa_menubar)
        if u != "private:separator"
    ]
    wa_shared = [a for a in wa_actions if a in set(lp_actions)]
    assert lp_actions == wa_shared


def test_librepy_images_section_points_at_python_assets():
    root = ET.parse(_ADDONS_XCU).getroot()
    images = None
    for node in root.iter("node"):
        if node.get(_OOR_NAME) == "Images":
            images = node
            break
    assert images is not None
    by_url: dict[str, str] = {}
    for item in images.findall("node"):
        url = _prop_text(item, "URL")
        small = None
        for child in item.iter("prop"):
            if child.get(_OOR_NAME) == "ImageSmallURL":
                value = child.find("value")
                if value is not None and value.text:
                    small = value.text.strip()
        if url and small:
            by_url[url] = small
    run_url = "org.extension.librepy:scripting.run_python_dialog"
    cell_url = "org.extension.librepy:scripting.edit_python_cell"
    settings_url = "org.extension.librepy:main.settings"
    assert by_url[run_url] == "%origin%/assets/python_32.png"
    assert by_url[cell_url] == "%origin%/assets/python_cell_32.png"
    assert by_url[settings_url] == "%origin%/assets/gear_32.png"
