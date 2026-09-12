# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Live UNO: header/footer region membership and refuse-off-while-content-remains.

Missing XText identity (bare ``!=`` on distinct PyUNO wrappers) was the disaster
for wipe: ``_scan_region_content`` skipped the logo and ``setString`` looked
safe. The logo test locks get/scan metadata — a false miss is still wrong — and
does not require refuse-on-set. The remaining tests pin
``page_set_style_properties`` refusing header/footer off while content remains.
"""

from __future__ import annotations

import os

from plugin.framework.uno_context import uno_same
from plugin.testing_runner import native_test
from plugin.tests.testing_utils import (
    TestingFactory,
    skip_windows_leftover_hidden_load,
    with_native_doc,
)
from plugin.writer.images.image_tools import insert_image_into_header_footer
from plugin.writer.page import (
    PageGetHeaderFooterText,
    PageSetHeaderFooterText,
    PageSetStyleProperties,
    _scan_region_content,
    resolve_page_style,
)


def _header_logo_path() -> str:
    """Return logo_32.png in the dev tree or a remapped release bundle."""
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    for rel in ("extension/assets/logo_32.png", "assets/logo_32.png"):
        path = os.path.join(root, *rel.split("/"))
        if os.path.isfile(path):
            return path
    return os.path.join(root, "extension", "assets", "logo_32.png")


def _tool_ctx(doc, ctx):
    # GHA 34689136372: leftover Hidden `_blank` in xtext_to_content
    # hung page-header get. This file is the next PageGetHeaderFooterText
    # victim.
    skip_windows_leftover_hidden_load("page_header Hidden _blank xtext_to_content")
    from plugin.framework.tool import ToolContext

    services = None
    try:
        from plugin.main import get_services

        services = get_services()
    except Exception:
        services = None
    return ToolContext(doc, ctx, "writer", services, "test")


def _style_name(doc):
    styles = doc.getStyleFamilies().getByName("PageStyles")
    if styles.hasByName("Standard"):
        return "Standard"
    return styles.getElementNames()[0]


def _style(doc):
    name = _style_name(doc)
    return doc.getStyleFamilies().getByName("PageStyles").getByName(name), name


def _set_text(doc, ctx, region, content, **kwargs):
    return PageSetHeaderFooterText().execute(
        _tool_ctx(doc, ctx), style=_style_name(doc), region=region, content=content, **kwargs,
    )


def _set_props(doc, ctx, **kwargs):
    return PageSetStyleProperties().execute(
        _tool_ctx(doc, ctx), style=_style_name(doc), **kwargs,
    )


@native_test
@with_native_doc("writer")
def test_header_logo_visible_to_scan_and_get(ctx, doc):
    path = _header_logo_path()
    assert os.path.isfile(path), "fixture image missing: %s" % path

    placed = insert_image_into_header_footer(
        doc, path, "header", width_mm=15, height_mm=15, title="HEADER_LOGO", ctx=ctx)
    graphic = placed["graphic"]
    assert graphic is not None, "failed to insert AS_CHARACTER graphic into the header"
    try:
        graphic.setName("HEADER_LOGO")
    except Exception:
        pass
    try:
        expected_name = str(graphic.getName()) or None
    except Exception:
        expected_name = None

    style, _resolved = resolve_page_style(doc, "Standard")
    header_text = style.getPropertyValue("HeaderText")
    scan = _scan_region_content(doc, header_text)
    draw_count = 0
    try:
        draw_count = int(doc.getDrawPage().getCount())
    except Exception:
        pass
    assert scan["images"], (
        "scan missed the header logo (XText identity). images=%r draw_page_count=%s. "
        "A false miss is wrong for get/metadata; historically it also made wipe look safe."
        % (scan["images"], draw_count)
    )
    if expected_name:
        assert expected_name in scan["images"], scan["images"]

    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    res = PageGetHeaderFooterText().execute(tool_ctx, region="header")
    assert res["status"] == "ok", res
    assert res.get("images"), "page_get missed the header logo: %r" % (res,)
    if expected_name:
        assert expected_name in res["images"], res["images"]

    # Membership: the shape's anchor text is the header XText even when wrappers differ.
    shape = doc.getDrawPage().getByIndex(0)
    anchor_text = shape.getAnchor().getText()
    assert uno_same(anchor_text, header_text), (
        "uno_same must treat the shape anchor XText as the page-style HeaderText "
        "(is=%s ===%s)" % (anchor_text is header_text, anchor_text == header_text)
    )


@native_test
@with_native_doc("writer")
def test_disable_header_refuses_while_text_remains(ctx, doc):
    applied = _set_text(doc, ctx, "header", "<p>Keep this header</p>")
    assert applied["status"] == "ok"
    style, _name = _style(doc)
    assert style.getPropertyValue("HeaderIsOn") is True
    before = style.getPropertyValue("HeaderText").getString()

    res = _set_props(doc, ctx, header_is_on=False)
    assert res["status"] == "error"
    assert "page_set_header_footer_text" in res["message"]
    assert style.getPropertyValue("HeaderIsOn") is True
    after = style.getPropertyValue("HeaderText").getString()
    assert "Keep this header" in after
    assert after == before


@native_test
@with_native_doc("writer")
def test_disable_header_ok_after_clear(ctx, doc):
    applied = _set_text(doc, ctx, "header", "<p>Temporary header</p>")
    assert applied["status"] == "ok"
    cleared = _set_text(doc, ctx, "header", "")
    assert cleared["status"] == "ok"

    res = _set_props(doc, ctx, header_is_on=False)
    assert res["status"] == "ok", res
    style, _name = _style(doc)
    assert style.getPropertyValue("HeaderIsOn") is False


@native_test
@with_native_doc("writer")
def test_disable_footer_refuses_while_text_remains(ctx, doc):
    applied = _set_text(doc, ctx, "footer", "<p>Keep this footer</p>")
    assert applied["status"] == "ok"
    style, _name = _style(doc)
    res = _set_props(doc, ctx, footer_is_on=False)
    assert res["status"] == "error"
    assert "footer" in res["message"]
    assert style.getPropertyValue("FooterIsOn") is True
    assert "Keep this footer" in style.getPropertyValue("FooterText").getString()


@native_test
@with_native_doc("writer")
def test_enable_header_allowed_with_content(ctx, doc):
    applied = _set_text(doc, ctx, "header", "<p>Already on</p>")
    assert applied["status"] == "ok"
    res = _set_props(doc, ctx, header_is_on=True)
    assert res["status"] == "ok"
    style, _name = _style(doc)
    assert style.getPropertyValue("HeaderIsOn") is True
    assert "Already on" in style.getPropertyValue("HeaderText").getString()


@native_test
@with_native_doc("writer")
def test_disable_empty_header_allowed(ctx, doc):
    style, _name = _style(doc)
    style.setPropertyValue("HeaderIsOn", True)
    header = style.getPropertyValue("HeaderText")
    header.setString("")
    res = _set_props(doc, ctx, header_is_on=False)
    assert res["status"] == "ok", res
    assert style.getPropertyValue("HeaderIsOn") is False


@native_test
@with_native_doc("writer")
def test_disable_header_refuses_while_table_remains(ctx, doc):
    style, _name = _style(doc)
    style.setPropertyValue("HeaderIsOn", True)
    header = style.getPropertyValue("HeaderText")
    header.setString("")
    tbl = doc.createInstance("com.sun.star.text.TextTable")
    tbl.initialize(1, 2)
    header.insertTextContent(header.createTextCursor(), tbl, False)
    tbl.getCellByName("A1").setString("Logo cell")
    tbl.getCellByName("B1").setString("Address cell")

    res = _set_props(doc, ctx, header_is_on=False)
    assert res["status"] == "error"
    assert style.getPropertyValue("HeaderIsOn") is True
    # getString() on a header table is empty / not the cell text — the table
    # itself must still be there (the refuse is what keeps it).
    header = style.getPropertyValue("HeaderText")
    found_table = False
    enum = header.createEnumeration()
    while enum.hasMoreElements() is True:
        el = enum.nextElement()
        try:
            if el.supportsService("com.sun.star.text.TextTable") is True:
                assert el.getCellByName("A1").getString() == "Logo cell"
                found_table = True
                break
        except Exception:
            continue
    assert found_table, "letterhead table must remain after refused disable"


@native_test
@with_native_doc("writer")
def test_disable_header_refuses_first_page_letterhead(ctx, doc):
    style, _name = _style(doc)
    style.setPropertyValue("HeaderIsOn", True)
    style.setPropertyValue("FirstIsShared", False)
    shared = _set_text(doc, ctx, "header", "")
    first = _set_text(doc, ctx, "header_first", "<p>First-page letterhead</p>")
    assert shared["status"] == "ok" and first["status"] == "ok"

    res = _set_props(doc, ctx, header_is_on=False)
    assert res["status"] == "error"
    assert "header_first" in res["message"]
    assert style.getPropertyValue("HeaderIsOn") is True
    assert "First-page letterhead" in style.getPropertyValue("HeaderTextFirst").getString()
