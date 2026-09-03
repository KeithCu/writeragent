# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Structural tests for WriterAgent extension/Addons.xcu menu order and Context."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

_OOR_NS = "http://openoffice.org/2001/registry"
_OOR_NAME = "{%s}name" % _OOR_NS

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ADDONS_XCU = _REPO_ROOT / "extension" / "Addons.xcu"

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

_PROTOCOL = "org.extension.writeragent:"


def _prop_text(node: ET.Element, prop_name: str) -> str | None:
    for prop in node.findall("prop"):
        if prop.get(_OOR_NAME) == prop_name:
            value = prop.find("value")
            if value is not None and value.text:
                return value.text.strip()
    return None


def _find_menubar(root: ET.Element) -> ET.Element:
    for node in root.iter("node"):
        if node.get(_OOR_NAME) == "org.extension.writeragent.menubar":
            return node
    raise AssertionError("org.extension.writeragent.menubar not found")


def _submenu_node(menubar: ET.Element) -> ET.Element:
    return next(n for n in menubar.findall("node") if n.get(_OOR_NAME) == "Submenu")


def _submenu_items(menubar: ET.Element) -> dict[str, ET.Element]:
    by_url: dict[str, ET.Element] = {}
    for item in _submenu_node(menubar).findall("node"):
        url = _prop_text(item, "URL")
        if url:
            by_url[url] = item
    return by_url


def _ordered_urls(menubar: ET.Element) -> list[str]:
    urls = [_prop_text(item, "URL") for item in _submenu_node(menubar).findall("node")]
    return [u for u in urls if u]


def _ordered_names(menubar: ET.Element) -> list[str]:
    return [item.get(_OOR_NAME) or "" for item in _submenu_node(menubar).findall("node")]


def _debug_submenu(menubar: ET.Element) -> ET.Element:
    debug = _submenu_items(menubar)[_PROTOCOL + "main.NoOp"]
    return next(n for n in debug.findall("node") if n.get(_OOR_NAME) == "Submenu")


def test_writeragent_menubar_has_full_context():
    root = ET.parse(_ADDONS_XCU).getroot()
    assert _prop_text(_find_menubar(root), "Context") == _FULL_CONTEXT


def test_writeragent_shared_items_have_explicit_full_context():
    root = ET.parse(_ADDONS_XCU).getroot()
    items = _submenu_items(_find_menubar(root))
    shared_urls = (
        _PROTOCOL + "chatbot.extend_selection",
        _PROTOCOL + "chatbot.edit_selection",
        _PROTOCOL + "scripting.run_python_dialog",
        _PROTOCOL + "embeddings.search_dialog",
        _PROTOCOL + "mcp.toggle_server",
        _PROTOCOL + "mcp.server_status",
        _PROTOCOL + "main.settings",
        _PROTOCOL + "vision.open_settings",
        _PROTOCOL + "scripting.reset_python_session",
        _PROTOCOL + "main.report_bug",
    )
    for url in shared_urls:
        assert url in items, f"missing menu item {url}"
        assert _prop_text(items[url], "Context") == _FULL_CONTEXT, url


def test_writeragent_calc_only_items():
    root = ET.parse(_ADDONS_XCU).getroot()
    items = _submenu_items(_find_menubar(root))
    for url in (
        _PROTOCOL + "scripting.edit_python_cell",
        _PROTOCOL + "calc.convert_spreadsheet_to_python",
    ):
        assert url in items, f"missing menu item {url}"
        ctx = _prop_text(items[url], "Context")
        assert ctx == _CALC_SVC, url
        assert _WRITER_SVC not in (ctx or "")


def test_writeragent_writer_only_top_level_items():
    root = ET.parse(_ADDONS_XCU).getroot()
    items = _submenu_items(_find_menubar(root))
    for url in (
        _PROTOCOL + "textanalytics.open_dialog",
        _PROTOCOL + "writer.insert_latex_dialog",
    ):
        assert url in items, f"missing menu item {url}"
        ctx = _prop_text(items[url], "Context")
        assert ctx == _WRITER_SVC, url
        assert _CALC_SVC not in (ctx or "")


def test_writeragent_jupyter_is_not_a_menu_item():
    """File → Open is the Jupyter path; no menubar or Debug item."""
    root = ET.parse(_ADDONS_XCU).getroot()
    menubar = _find_menubar(root)
    jupyter = _PROTOCOL + "scripting.import_ipynb"
    items = _submenu_items(menubar)
    assert jupyter not in items
    debug_urls = [
        _prop_text(item, "URL") for item in _debug_submenu(menubar).findall("node")
    ]
    assert jupyter not in debug_urls


def test_writeragent_notebook_run_is_not_a_menu_item():
    """Run All / From Here / Stop are hamburger-only when a notebook registry exists."""
    root = ET.parse(_ADDONS_XCU).getroot()
    items = _submenu_items(_find_menubar(root))
    debug_urls = [
        _prop_text(item, "URL") for item in _debug_submenu(_find_menubar(root)).findall("node")
    ]
    for url in (
        _PROTOCOL + "notebook.run_all",
        _PROTOCOL + "notebook.run_from_here",
        _PROTOCOL + "notebook.stop",
    ):
        assert url not in items
        assert url not in debug_urls


def test_writeragent_node_names_are_sort_stable():
    root = ET.parse(_ADDONS_XCU).getroot()
    names = _ordered_names(_find_menubar(root))
    assert names == sorted(names)
    debug_names = [
        item.get(_OOR_NAME) or ""
        for item in _debug_submenu(_find_menubar(root)).findall("node")
    ]
    assert debug_names == sorted(debug_names)


def test_writeragent_menu_order():
    root = ET.parse(_ADDONS_XCU).getroot()
    assert _ordered_urls(_find_menubar(root)) == [
        _PROTOCOL + "chatbot.extend_selection",
        _PROTOCOL + "chatbot.edit_selection",
        "private:separator",
        _PROTOCOL + "scripting.run_python_dialog",
        _PROTOCOL + "scripting.edit_python_cell",
        _PROTOCOL + "calc.convert_spreadsheet_to_python",
        _PROTOCOL + "embeddings.search_dialog",
        _PROTOCOL + "writer.insert_latex_dialog",
        _PROTOCOL + "textanalytics.open_dialog",
        "private:separator",
        _PROTOCOL + "main.settings",
        _PROTOCOL + "vision.open_settings",
        _PROTOCOL + "mcp.toggle_server",
        _PROTOCOL + "mcp.server_status",
        _PROTOCOL + "scripting.reset_python_session",
        _PROTOCOL + "main.NoOp",
        _PROTOCOL + "main.report_bug",
    ]


def test_writeragent_report_bug_is_last():
    root = ET.parse(_ADDONS_XCU).getroot()
    urls = _ordered_urls(_find_menubar(root))
    assert urls[-1] == _PROTOCOL + "main.report_bug"


def _has_image_identifier(node: ET.Element) -> bool:
    return any(prop.get(_OOR_NAME) == "ImageIdentifier" for prop in node.findall("prop"))


def test_writeragent_mcp_items_reserve_icon_slot():
    root = ET.parse(_ADDONS_XCU).getroot()
    items = _submenu_items(_find_menubar(root))
    assert _has_image_identifier(items[_PROTOCOL + "mcp.server_status"])
    assert not _has_image_identifier(items[_PROTOCOL + "mcp.toggle_server"])


def test_writeragent_toolbar_has_no_notebook_run_actions():
    root = ET.parse(_ADDONS_XCU).getroot()
    toolbar = None
    for node in root.iter("node"):
        if node.get(_OOR_NAME) == "org.extension.writeragent.toolbar":
            toolbar = node
            break
    assert toolbar is not None
    urls = []
    for item in toolbar.findall("node"):
        url = _prop_text(item, "URL")
        if url:
            urls.append(url)
    assert _PROTOCOL + "notebook.run_all" not in urls
    assert _PROTOCOL + "notebook.run_from_here" not in urls
    assert _PROTOCOL + "notebook.stop" not in urls


def test_writeragent_mcp_images_section_points_at_assets():
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
    status = _PROTOCOL + "mcp.server_status"
    assert status in by_url
    assert by_url[status].endswith("assets/stopped_16.png")
    assert by_url[status].startswith("%origin%/")
    assert _PROTOCOL + "mcp.toggle_server" not in by_url
    assert _PROTOCOL + "scripting.import_ipynb" not in by_url
    settings = _PROTOCOL + "main.settings"
    assert by_url[settings] == "%origin%/assets/gear_32.png"
