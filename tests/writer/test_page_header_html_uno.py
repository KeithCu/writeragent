# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Live UNO: page_get / page_set header-footer HTML roundtrip.

Logos, fields, and tables set without force; get still lists scan extras.
"""

from __future__ import annotations

import base64
import os
import tempfile

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import skip_windows_leftover_hidden_load, with_native_doc
from plugin.writer.page import PageGetHeaderFooterText, PageSetHeaderFooterText

# Known-good 1x1 PNG (not the truncated IDAT that libpng rejects).
_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _tool_ctx(doc, ctx):
    # GHA 34689136372: leftover writer reuse then
    # html_export._open_hidden_writer Hidden `_blank` hung 30s in
    # test_plain_header_footer_html_roundtrip (_get → xtext_to_content).
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


def _get(doc, ctx, region, **kwargs):
    return PageGetHeaderFooterText().execute(
        _tool_ctx(doc, ctx), style=_style_name(doc), region=region, **kwargs,
    )


def _set(doc, ctx, region, content, **kwargs):
    return PageSetHeaderFooterText().execute(
        _tool_ctx(doc, ctx), style=_style_name(doc), region=region, content=content, **kwargs,
    )


def _region_text(doc, region):
    from plugin.writer.page import _REGION_PROPS

    style, _name = _style(doc)
    _is_on, text_prop = _REGION_PROPS[region]
    return style.getPropertyValue(text_prop)


def _field_contents(doc, region):
    from plugin.writer.page import _scan_region_content

    scan = _scan_region_content(doc, _region_text(doc, region))
    return [f.get("content", "") for f in scan.get("fields", [])]


@native_test
@with_native_doc("writer")
def test_plain_header_footer_html_roundtrip(ctx, doc):
    set_h = _set(doc, ctx, "header", "<p>Plain header line</p>", auto_height=True)
    set_f = _set(doc, ctx, "footer", "<p>Plain footer line</p>")
    assert set_h["status"] == "ok" and set_h.get("format") == "html"
    assert set_f["status"] == "ok"

    got_h = _get(doc, ctx, "header")
    got_f = _get(doc, ctx, "footer")
    assert got_h["status"] == "ok" and got_h.get("format") == "html"
    assert "Plain header line" in got_h["content"]
    assert "Plain footer line" in got_f["content"]

    again = _set(doc, ctx, "header", got_h["content"])
    assert again["status"] == "ok"
    got2 = _get(doc, ctx, "header")
    assert "Plain header line" in got2["content"]


@native_test
@with_native_doc("writer")
def test_page_number_field_survives_html_roundtrip(ctx, doc):
    style, _name = _style(doc)
    style.setPropertyValue("FooterIsOn", True)
    footer = style.getPropertyValue("FooterText")
    footer.setString("Confidential | ")
    cur = footer.createTextCursor()
    cur.gotoEnd(False)
    field = doc.createInstance("com.sun.star.text.textfield.PageNumber")
    try:
        from com.sun.star.text.PageNumberType import CURRENT

        field.setPropertyValue("PageNumberType", CURRENT)
        field.setPropertyValue("NumberingType", 4)
    except Exception:
        pass
    footer.insertTextContent(cur, field, False)

    got = _get(doc, ctx, "footer")
    assert got["status"] == "ok"
    assert "Confidential" in got["content"]
    assert 'title="page-number"' in got["content"] or "WA-FIELD:page-number" in got["content"]
    assert got.get("fields"), "get must list the live field, not only HTML: %r" % got

    applied = _set(doc, ctx, "footer", got["content"])
    assert applied["status"] == "ok"
    got2 = _get(doc, ctx, "footer")
    assert "Confidential" in got2["content"]
    contents = _field_contents(doc, "footer")
    assert contents, "page-number field must survive apply of the exported HTML: %r / %r" % (
        contents, got2,
    )
    assert any("page" in c.lower() for c in contents), contents


@native_test
@with_native_doc("writer")
def test_apply_field_html_creates_live_field(ctx, doc):
    """apply→get: HTML that represents a field (body export shape) becomes a UNO field."""
    html = '<p>Page <span title="page-number"/> of report</p>'
    applied = _set(doc, ctx, "header", html)
    assert applied["status"] == "ok"
    got = _get(doc, ctx, "header")
    assert "Page" in got["content"]
    assert got.get("fields") or 'title="page-number"' in got["content"]
    contents = _field_contents(doc, "header")
    assert contents, "import of <span title=\"page-number\"/> must restore a field: %r" % got


@native_test
@with_native_doc("writer")
def test_apply_letterhead_field_html_creates_live_fields(ctx, doc):
    """apply→get: letterhead XHTML titles restore as live UNO fields."""
    html = (
        '<p><span title="chapter"/> | <span title="author-name"/> | '
        '<span title="file-name"/> | <span title="title"/> | '
        '<span title="subject"/></p>'
    )
    applied = _set(doc, ctx, "header", html)
    assert applied["status"] == "ok"
    contents = _field_contents(doc, "header")
    assert len(contents) >= 5, (
        "chapter/author-name/file-name/title/subject must restore: %r" % contents
    )


@native_test
@with_native_doc("writer")
def test_table_letterhead_html_roundtrip(ctx, doc):
    style, _name = _style(doc)
    style.setPropertyValue("HeaderIsOn", True)
    header = style.getPropertyValue("HeaderText")
    header.setString("")
    tbl = doc.createInstance("com.sun.star.text.TextTable")
    tbl.initialize(1, 2)
    header.insertTextContent(header.createTextCursor(), tbl, False)
    tbl.getCellByName("A1").setString("Logo cell")
    tbl.getCellByName("B1").setString("Address cell")

    got = _get(doc, ctx, "header")
    assert got["status"] == "ok"
    assert "<table" in got["content"].lower()
    assert "Logo cell" in got["content"]
    assert "Address cell" in got["content"]

    applied = _set(doc, ctx, "header", got["content"])
    assert applied["status"] == "ok"
    got2 = _get(doc, ctx, "header")
    assert "<table" in got2["content"].lower()
    assert "Logo cell" in got2["content"]
    assert "Address cell" in got2["content"]


@native_test
@with_native_doc("writer")
def test_apply_table_html_then_get(ctx, doc):
    html = "<table><tr><td>Left head</td><td>Right head</td></tr></table>"
    applied = _set(doc, ctx, "footer", html)
    assert applied["status"] == "ok"
    got = _get(doc, ctx, "footer")
    assert "Left head" in got["content"]
    assert "Right head" in got["content"]


@native_test
@with_native_doc("writer")
def test_as_character_logo_html_roundtrip(ctx, doc):
    from plugin.writer.images.image_tools import insert_image_into_header_footer

    fd, path = tempfile.mkstemp(suffix=".png")
    try:
        os.write(fd, _PNG_BYTES)
        os.close(fd)
        style, _name = _style(doc)
        style.setPropertyValue("HeaderIsOn", True)
        insert_image_into_header_footer(
            doc, path, "header", width_mm=12, height_mm=12, style_name=_style_name(doc), ctx=ctx,
        )
        got = _get(doc, ctx, "header", include_images=True)
        assert got["status"] == "ok"
        assert "<img" in got["content"].lower(), "logo must be in the HTML, not dropped: %r" % got["content"][:400]
        assert got.get("images"), "scan must still list the logo: %r" % got

        applied = _set(doc, ctx, "header", got["content"], auto_height=True)
        assert applied["status"] == "ok"
        got2 = _get(doc, ctx, "header", include_images=True)
        assert "<img" in got2["content"].lower(), "logo must survive apply of its HTML: %r" % got2["content"][:400]
    finally:
        try:
            os.unlink(path)
        except Exception:
            pass


@native_test
@with_native_doc("writer")
def test_first_page_header_is_its_own_html(ctx, doc):
    style, _name = _style(doc)
    # FirstIsShared is ignored while the header is off (property reads None).
    style.setPropertyValue("HeaderIsOn", True)
    style.setPropertyValue("FirstIsShared", False)
    set_shared = _set(doc, ctx, "header", "<p>Shared header copy</p>")
    set_first = _set(doc, ctx, "header_first", "<p>First-page letterhead</p>")
    assert set_shared["status"] == "ok"
    assert set_first["status"] == "ok"

    got_shared = _get(doc, ctx, "header")
    got_first = _get(doc, ctx, "header_first")
    assert "Shared header copy" in got_shared["content"]
    assert "First-page letterhead" in got_first["content"]
    assert "First-page letterhead" not in got_shared["content"]
    assert "Shared header copy" not in got_first["content"]

    applied = _set(doc, ctx, "header_first", got_first["content"])
    assert applied["status"] == "ok"
    got_first2 = _get(doc, ctx, "header_first")
    assert "First-page letterhead" in got_first2["content"]
    assert "Shared header copy" in _get(doc, ctx, "header")["content"]


@native_test
@with_native_doc("writer")
def test_first_page_footer_analogue(ctx, doc):
    style, _name = _style(doc)
    style.setPropertyValue("FooterIsOn", True)
    style.setPropertyValue("FirstIsShared", False)
    _set(doc, ctx, "footer", "<p>Shared footer copy</p>")
    _set(doc, ctx, "footer_first", "<p>First-page footer</p>")
    got_shared = _get(doc, ctx, "footer")
    got_first = _get(doc, ctx, "footer_first")
    assert "Shared footer copy" in got_shared["content"]
    assert "First-page footer" in got_first["content"]


@native_test
@with_native_doc("writer")
def test_search_still_reaches_header_after_html_set(ctx, doc):
    """Augusto's findFirst-into-headers reach must stay open (PR #365)."""
    token = "UniqueHeaderTokenXYZ4229"
    applied = _set(doc, ctx, "header", "<p>%s</p>" % token)
    assert applied["status"] == "ok"
    sd = doc.createSearchDescriptor()
    sd.SearchString = token
    found = doc.findFirst(sd)
    assert found is not None, "findFirst must still see header text"
    header = _region_text(doc, "header")
    from plugin.framework.uno_context import uno_same

    assert uno_same(found.getText(), header) or token in header.getString()
