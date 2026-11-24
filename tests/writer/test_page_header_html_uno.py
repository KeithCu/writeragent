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
from plugin.tests.testing_utils import with_native_doc, writer_services_tool_context
from plugin.writer.page import PageGetHeaderFooterText, PageSetHeaderFooterText

# Known-good 1x1 PNG (not the truncated IDAT that libpng rejects).
_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _tool_ctx(doc, ctx):
    return writer_services_tool_context(doc, ctx)


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
        _tool_ctx(doc, ctx), style=_style_name(doc), region=region, **{"include_images": True, **kwargs},
    )


def _set(doc, ctx, region, content, **kwargs):
    return PageSetHeaderFooterText().execute(
        _tool_ctx(doc, ctx), style=_style_name(doc), region=region, content=content, **kwargs,
    )


def _region_text(doc, region):
    from plugin.writer.page import REGION_PROPS

    style, _name = _style(doc)
    _is_on, text_prop = REGION_PROPS[region]
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
    # findFirst only searches headers of in-use page styles. Windows pool
    # reuse can leave PageDescName='First Page' while we write Standard
    # (GHA 35466498641). Pin the body to Standard before the set.
    try:
        body = doc.getText().createTextCursor()
        body.gotoStart(False)
        body.gotoEnd(True)
        body.setPropertyValue("PageDescName", "Standard")
    except Exception:
        pass
    token = "UniqueHeaderTokenXYZ4229"
    applied = _set(doc, ctx, "header", "<p>%s</p>" % token)
    assert applied["status"] == "ok"
    # Windows findFirst can miss a just-written header until idle
    # (GHA 35466498641). Test-only — not in page_set_header_footer_text.
    from plugin.framework.uno_context import process_events_to_idle

    process_events_to_idle(ctx, force=True)
    sd = doc.createSearchDescriptor()
    sd.SearchString = token
    found = doc.findFirst(sd)
    assert found is not None, "findFirst must still see header text"
    header = _region_text(doc, "header")
    from plugin.framework.uno_context import uno_same

    assert uno_same(found.getText(), header) or token in header.getString()


@native_test
@with_native_doc("writer")
def test_apply_document_content_search_reaches_header_uno(ctx, doc):
    """Verify ApplyDocumentContent search reaches and updates header text in place while leaving body text untouched."""
    # findFirst only searches headers of in-use page styles. Pin the body to
    # Standard so the header style matches the active page style.
    try:
        body = doc.getText().createTextCursor()
        body.gotoStart(False)
        body.gotoEnd(True)
        body.setPropertyValue("PageDescName", "Standard")
    except Exception:
        pass
    token = "UniqueHeaderTokenXYZ4230"
    applied = _set(doc, ctx, "header", "<p>%s</p>" % token)
    assert applied["status"] == "ok"

    from plugin.framework.uno_context import process_events_to_idle

    process_events_to_idle(ctx, force=True)

    initial_body = doc.getText().getString()

    from plugin.writer.apply_document_content import ApplyDocumentContent

    res = ApplyDocumentContent().execute(
        _tool_ctx(doc, ctx),
        target="search",
        old_content=token,
        content="<p>New</p>",
    )
    assert res.get("status") == "ok", res
    assert "New" in _region_text(doc, "header").getString()
    assert token not in _region_text(doc, "header").getString()
    assert doc.getText().getString() == initial_body


@native_test
@with_native_doc("writer")
def test_set_header_first_does_not_write_shared_header_uno(ctx, doc):
    style, _name = _style(doc)
    style.setPropertyValue("HeaderIsOn", True)
    style.setPropertyValue("FirstIsShared", True)
    _set(doc, ctx, "header", "<p>Original shared header text</p>")

    res = _set(doc, ctx, "header_first", "<p>Different first page text</p>")
    assert res["status"] == "skipped"
    assert res.get("mirrored") is True

    got = _get(doc, ctx, "header")
    assert "Original shared header text" in got["content"]
    assert "Different first page text" not in got["content"]


@native_test
@with_native_doc("writer")
def test_replace_emoji_in_header_uno(ctx, doc):
    from plugin.writer.page import PageReplaceHeaderFooterText

    style, _name = _style(doc)
    style.setPropertyValue("HeaderIsOn", True)
    header_text = style.getPropertyValue("HeaderText")
    header_text.setString("A😀B")

    tool = PageReplaceHeaderFooterText()
    res = tool.execute(
        _tool_ctx(doc, ctx), style=_style_name(doc), region="header",
        old_content="😀", content="X", all_matches=False,
    )
    assert res["status"] == "ok", res
    assert header_text.getString() == "AXB"


def _setup_header_with_field_and_logo(doc, ctx):
    from plugin.writer.images.image_tools import insert_image_into_header_footer

    style, _name = _style(doc)
    style.setPropertyValue("HeaderIsOn", True)
    header = style.getPropertyValue("HeaderText")
    header.setString("Page ")

    cur = header.createTextCursor()
    cur.gotoEnd(False)
    field = doc.createInstance("com.sun.star.text.textfield.PageNumber")
    try:
        from com.sun.star.text.PageNumberType import CURRENT
        field.setPropertyValue("PageNumberType", CURRENT)
        field.setPropertyValue("NumberingType", 4)
    except Exception:
        pass
    header.insertTextContent(cur, field, False)

    fd, path = tempfile.mkstemp(suffix=".png")
    try:
        os.write(fd, _PNG_BYTES)
        os.close(fd)
        insert_image_into_header_footer(
            doc, path, "header", width_mm=10, height_mm=10, style_name=_style_name(doc), ctx=ctx,
        )
    finally:
        try:
            os.unlink(path)
        except Exception:
            pass
    return header


@native_test
@with_native_doc("writer")
def test_undo_rollback_restores_header_fields_and_logo(ctx, doc):
    from plugin.writer.html_import import insert_html_fragment_at_cursor
    from plugin.writer.page import _scan_region_content

    header = _setup_header_with_field_and_logo(doc, ctx)
    scan_before = _scan_region_content(doc, header)
    assert scan_before["fields"] and scan_before["images"]

    mgr = doc.getUndoManager()
    title = "test_undo_probe"
    mgr.enterUndoContext(title)
    cur = header.createTextCursor()
    cur.gotoStart(False)
    cur.gotoEnd(True)
    cur.setString("")
    insert_html_fragment_at_cursor(cur, "<p>replaced</p>", wrap=False)
    mgr.leaveUndoContext()

    titles = mgr.getAllUndoActionTitles()
    assert titles and titles[0] == title
    mgr.undo()

    scan_after = _scan_region_content(doc, header)
    assert len(scan_after["fields"]) == len(scan_before["fields"])
    assert len(scan_after["images"]) == len(scan_before["images"])


@native_test
@with_native_doc("writer")
def test_page_set_header_undo_rollback_on_import_failure(ctx, doc):
    from unittest.mock import patch
    from plugin.writer.page import _scan_region_content

    header = _setup_header_with_field_and_logo(doc, ctx)
    scan_before = _scan_region_content(doc, header)
    assert scan_before["fields"] and scan_before["images"]

    with patch(
        "plugin.writer.html_import.insert_html_fragment_at_cursor",
        side_effect=RuntimeError("injected import failure"),
    ):
        res = _set(doc, ctx, "header", "<p>failed import</p>")

    assert res["status"] == "error"
    scan_after = _scan_region_content(doc, header)
    assert len(scan_after["fields"]) == len(scan_before["fields"])
    assert len(scan_after["images"]) == len(scan_before["images"])


@native_test
@with_native_doc("writer")
def test_page_set_header_undo_rollback_on_field_restore_failure(ctx, doc):
    from unittest.mock import patch
    from plugin.writer.page import _scan_region_content

    header = _setup_header_with_field_and_logo(doc, ctx)
    scan_before = _scan_region_content(doc, header)
    assert scan_before["fields"] and scan_before["images"]

    with patch(
        "plugin.writer.field_restore.restore_field_placeholders",
        side_effect=RuntimeError("injected field restore failure"),
    ):
        res = _set(doc, ctx, "header", '<p>Page <span title="page-number"/></p>')

    assert res["status"] == "error"
    scan_after = _scan_region_content(doc, header)
    assert len(scan_after["fields"]) == len(scan_before["fields"])
    assert len(scan_after["images"]) == len(scan_before["images"])


@native_test
@with_native_doc("writer")
def test_header_trailing_paragraph_cleanup_single_undo_step_uno(ctx, doc):
    """Trailing empty paragraph cleanup is inside the undo context so one undo restores OLD."""
    from plugin.writer.html_import import replace_xtext_with_html

    style, _name = _style(doc)
    style.setPropertyValue("HeaderIsOn", True)
    header = style.getPropertyValue("HeaderText")
    header.setString("OLD")

    replace_xtext_with_html(header, "<p>New header</p><p></p>", model=doc)
    assert header.getString().strip() == "New header"

    doc.getUndoManager().undo()
    assert header.getString().strip() == "OLD"




def _first_paragraph(text_obj):
    return text_obj.createEnumeration().nextElement()


@native_test
@with_native_doc("writer")
def test_header_data_lo_style_and_para_round_trip_uno(ctx, doc):
    """data-lo-style / data-lo-para apply on a header write and come back from the read.

    Writer stores lengths in twips, so 1cm (1000 in 1/100 mm) can read back as 999 or 1001.
    """
    html = '<p data-lo-style="Header" data-lo-para="margin-left:1cm; text-align:center">Letterhead</p>'
    res = _set(doc, ctx, "header", html)
    assert res["status"] == "ok", res
    assert "warnings" not in res, res

    para = _first_paragraph(_region_text(doc, "header"))
    assert para.getPropertyValue("ParaStyleName") == "Header"
    assert abs(para.getPropertyValue("ParaLeftMargin") - 1000) <= 1
    assert para.getPropertyValue("ParaAdjust") == 3  # CENTER

    read = _get(doc, ctx, "header")["content"]
    assert 'data-lo-style="Header"' in read, read
    assert "text-align:center" in read and "margin-left:" in read, read
    # Values the paragraph inherits from its style are not reported as overrides.
    assert "margin-right" not in read and "margin-top" not in read, read

    res2 = _set(doc, ctx, "header", read)
    assert res2["status"] == "ok", res2
    # Only read-only names may warn: on some machines the import sets a direct font that
    # the read then reports (fonts are not written back, see _READ_ONLY_PARA_NAMES).
    from plugin.writer.html_import_styles import _READ_ONLY_PARA_NAMES

    warned = {(w["message"] if isinstance(w, dict) else w).split("'")[1] for w in res2.get("warnings", [])}
    assert warned <= _READ_ONLY_PARA_NAMES, (res2, read)
    para = _first_paragraph(_region_text(doc, "header"))
    assert para.getString() == "Letterhead"
    assert para.getPropertyValue("ParaStyleName") == "Header"
    assert abs(para.getPropertyValue("ParaLeftMargin") - 1000) <= 1


@native_test
@with_native_doc("writer")
def test_header_unapplied_data_lo_para_is_reported_uno(ctx, doc):
    """A paragraph-level font cannot be applied (it would flatten spans), so it warns."""
    res = _set(doc, ctx, "footer", '<p data-lo-para="font-weight:bold">Footer</p>')
    assert res["status"] == "ok", res
    assert res.get("warnings") == [
        {"code": "PARA_PROPERTY_UNSUPPORTED", "message": "data-lo-para property 'font-weight' was not applied"}
    ], res
    assert _region_text(doc, "footer").getString().strip() == "Footer"


@native_test
@with_native_doc("writer")
def test_header_custom_style_read_reports_margin_uno(ctx, doc):
    """A custom paragraph style copied into the scratch doc keeps its name and inherited margin."""
    para_styles = doc.getStyleFamilies().getByName("ParagraphStyles")
    custom = doc.createInstance("com.sun.star.style.ParagraphStyle")
    para_styles.insertByName("CustomLetterhead", custom)
    custom.setPropertyValue("ParaLeftMargin", 2000)
    custom.setPropertyValue("ParaAdjust", 3)

    style, _name = _style(doc)
    style.setPropertyValue("HeaderIsOn", True)
    header = style.getPropertyValue("HeaderText")
    header.setString("Letterhead Header")
    para = _first_paragraph(header)
    para.setPropertyValue("ParaStyleName", "CustomLetterhead")

    read = _get(doc, ctx, "header")["content"]
    assert 'data-lo-style="CustomLetterhead"' in read, read
    assert "Letterhead Header" in read, read
    assert "margin-left" not in read, read
    assert "text-align" not in read, read

    res = _set(doc, ctx, "header", read)
    assert res.get("status") == "ok", res
    warnings = res.get("warnings") or []
    assert not any("CustomLetterhead" in (w.get("message", "") if isinstance(w, dict) else str(w)) for w in warnings)

    para_after = _first_paragraph(_region_text(doc, "header"))
    assert para_after.getPropertyValue("ParaStyleName") == "CustomLetterhead"
    assert para_after.getPropertyState("ParaLeftMargin").value == "DEFAULT_VALUE"
    assert abs(para_after.getPropertyValue("ParaLeftMargin") - 2000) <= 1


@native_test
@with_native_doc("writer")
def test_header_read_does_not_repeat_run_formatting_uno(ctx, doc):
    """Header reads paint runs after plain insertion so formatting never leaks across runs."""
    from com.sun.star.awt.FontSlant import ITALIC
    from plugin.writer.html_export import xtext_to_content

    style, _name = _style(doc)
    style.setPropertyValue("HeaderIsOn", True)
    header = style.getPropertyValue("HeaderText")
    header.setString("")
    cur = header.createTextCursor()

    # Paragraph 1: italic middle run
    header.insertString(cur, "Beta has ", False)
    cur.setPropertyValue("CharPosture", ITALIC)
    header.insertString(cur, "italic", False)
    cur.setPropertyValue("CharPosture", 0)
    header.insertString(cur, " run", False)

    # Paragraph 2: bold first run + plain second run (applied as a run, not paragraph-wide)
    header.insertControlCharacter(cur, 0, False)
    p2_start = cur.getStart()
    header.insertString(cur, "Bold plain", False)
    p2_bold = header.createTextCursorByRange(p2_start)
    p2_bold.goRight(4, True)
    p2_bold.setPropertyValue("CharWeight", 150.0)

    # Paragraph 3: hyperlink followed by plain text
    header.insertControlCharacter(cur, 0, False)
    p3_start = cur.getStart()
    header.insertString(cur, "Link after", False)
    p3_link = header.createTextCursorByRange(p3_start)
    p3_link.goRight(4, True)
    p3_link.setPropertyValue("HyperLinkURL", "https://example.com/")

    # Paragraph 4: bold page-number field
    header.insertControlCharacter(cur, 0, False)
    p4_start = cur.getStart()
    field = doc.createInstance("com.sun.star.text.textfield.PageNumber")
    header.insertTextContent(cur, field, False)
    p4_bold = header.createTextCursorByRange(p4_start)
    p4_bold.gotoEndOfParagraph(True)
    p4_bold.setPropertyValue("CharWeight", 150.0)

    # Paragraph 5: plain paragraph after a bold-ended paragraph
    header.insertControlCharacter(cur, 0, False)
    cur.setPropertyValue("CharWeight", 150.0)
    header.insertString(cur, "Ends bold", False)
    header.insertControlCharacter(cur, 0, False)
    cur.setPropertyValue("CharWeight", 100.0)
    header.insertString(cur, "Next plain", False)

    import re

    html = xtext_to_content(header, doc, ctx)
    lines = re.findall(r"<p\b.*?</p>", html, re.DOTALL)
    assert len(lines) == 6, html

    p1 = lines[0]
    p1_rest, n = re.subn(
        r'<(?:em|i)\b[^>]*>italic</(?:em|i)>|<span\b[^>]*font-style\s*:\s*italic[^>]*>italic</span>',
        "",
        p1,
        count=1,
    )
    assert n == 1, p1
    assert "<em" not in p1_rest and "<i" not in p1_rest and "font-style" not in p1_rest, p1

    p2 = lines[1]
    assert "data-lo-para" not in p2, p2
    assert ("font-weight:bold" in p2 or "<strong>Bold</strong>" in p2 or "<b>Bold</b>" in p2), p2
    assert "plain" in p2 and "font-weight:bold" not in p2.split("Bold", 1)[1], p2

    p3 = lines[2]
    assert 'href="https://example.com/"' in p3 and "</a> after" in p3, p3

    p4 = lines[3]
    assert 'title="page-number"' in p4 and ("<strong>" in p4 or "<b>" in p4 or "font-weight:bold" in p4), p4

    p6 = lines[5]
    assert "<strong>" not in p6 and "<b>" not in p6 and "font-weight:bold" not in p6, p6


@native_test
@with_native_doc("writer")
def test_header_read_skips_tracked_deletions_uno(ctx, doc):
    from plugin.writer.html_export import xtext_to_content
    from plugin.writer.record_changes import record_changes

    style, _name = _style(doc)
    style.setPropertyValue("HeaderIsOn", True)
    header = style.getPropertyValue("HeaderText")
    cur = header.createTextCursor()
    header.insertString(cur, "Keep GONE tail", False)
    cur.gotoStart(False)
    cur.goRight(len("Keep "), False)
    cur.goRight(len("GONE "), True)
    with record_changes(doc, True):
        cur.setString("")
    cur.gotoEnd(False)
    header.insertControlCharacter(cur, 0, False)
    title_field = doc.createInstance("com.sun.star.text.textfield.docinfo.Title")
    header.insertTextContent(cur, title_field, False)

    tbl = doc.createInstance("com.sun.star.text.TextTable")
    tbl.initialize(1, 1)
    header.insertTextContent(header.getEnd(), tbl, False)
    cell = tbl.getCellByName("A1")
    c_cur = cell.createTextCursor()
    cell.insertString(c_cur, "CellKeep CELLGONE end", False)
    c_cur.gotoStart(False)
    c_cur.goRight(len("CellKeep "), False)
    c_cur.goRight(len("CELLGONE "), True)
    with record_changes(doc, True):
        c_cur.setString("")

    redlines = doc.getRedlines()
    assert any(
        str(redlines.getByIndex(idx).getPropertyValue("RedlineType")) == "Delete"
        for idx in range(redlines.getCount())
    )

    html = xtext_to_content(header, doc, ctx)
    assert "Keep tail" in html, html
    assert "GONE" not in html, html
    assert 'title="title"' in html, html
    assert "CellKeep end" in html, html
    assert "CELLGONE" not in html, html


@native_test
@with_native_doc("writer")
def test_header_read_graphic_position_uno(ctx, doc):
    from com.sun.star.text.TextContentAnchorType import AS_CHARACTER, AT_CHARACTER, AT_PARAGRAPH
    from plugin.writer.html_export import xtext_to_content
    from plugin.writer.record_changes import record_changes

    style, _name = _style(doc)
    style.setPropertyValue("HeaderIsOn", True)
    header = style.getPropertyValue("HeaderText")
    cur = header.createTextCursor()
    header.insertString(cur, "DEL ", False)
    cur.gotoStart(False)
    cur.goRight(len("DEL "), True)
    with record_changes(doc, True):
        cur.setString("")
    redlines = doc.getRedlines()
    assert any(
        str(redlines.getByIndex(idx).getPropertyValue("RedlineType")) == "Delete"
        for idx in range(redlines.getCount())
    )
    tbl = doc.createInstance("com.sun.star.text.TextTable")
    tbl.initialize(1, 1)
    header.insertTextContent(header.getEnd(), tbl, False)
    tbl.getCellByName("A1").setString("Cell")

    cur.gotoEnd(False)
    date_field = doc.createInstance("com.sun.star.text.textfield.DateTime")
    date_field.setPropertyValue("IsDate", True)
    header.insertTextContent(cur, date_field, False)
    header.insertString(cur, " before ", False)

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(_PNG_BYTES)
        img_path = f.name
    try:
        import uno

        def _make_graphic(anchor_type, title):
            g = doc.createInstance("com.sun.star.text.TextGraphicObject")
            g.setPropertyValue("GraphicURL", uno.systemPathToFileUrl(img_path))
            g.setPropertyValue("AnchorType", anchor_type)
            g.setPropertyValue("Width", 500)
            g.setPropertyValue("Height", 500)
            g.setPropertyValue("Title", title)
            return g

        header.insertTextContent(cur, _make_graphic(AS_CHARACTER, "inline_logo"), False)
        header.insertString(cur, " after", False)
        header.insertTextContent(cur, _make_graphic(AT_CHARACTER, "at_char_logo"), False)
        header.insertTextContent(cur, _make_graphic(AT_PARAGRAPH, "at_para_logo"), False)
    finally:
        os.unlink(img_path)

    html = xtext_to_content(header, doc, ctx)
    assert "DEL" not in html
    assert html.count("<img") == 3, html
    before_idx = html.index("before")
    after_idx = html.index("after")
    img_idx = html.index("<img", before_idx)
    assert before_idx < img_idx < after_idx, html


@native_test
@with_native_doc("writer")
def test_header_table_round_trip_adds_no_paragraph_uno(ctx, doc):
    import re
    from plugin.writer.html_export import xtext_to_content

    style, _name = _style(doc)
    style.setPropertyValue("HeaderIsOn", True)
    header = style.getPropertyValue("HeaderText")
    cur = header.createTextCursor()
    header.insertString(cur, "before", False)
    tbl = doc.createInstance("com.sun.star.text.TextTable")
    tbl.initialize(1, 1)
    header.insertTextContent(header.getEnd(), tbl, False)
    tbl.getCellByName("A1").setString("cell")
    cur.gotoEnd(False)
    header.insertString(cur, "after", False)

    html = xtext_to_content(header, doc, ctx)
    paras = re.findall(r"<p\b[^>]*>(.*?)</p>", html, re.DOTALL)
    texts = [re.sub(r"<[^>]+>", "", p).strip() for p in paras]
    assert "" not in texts, (texts, html)


def _header_structure(header):
    from plugin.doc.text_helpers import iter_elements
    from plugin.writer.range_copy import supports_service

    out = []
    for el in iter_elements(header.createEnumeration()):
        if supports_service(el, "com.sun.star.text.TextTable"):
            out.append("T")
        else:
            out.append("P:" + el.getString())
    return out


@native_test
@with_native_doc("writer")
def test_header_table_shapes_cleared_and_trailing_para_style_uno(ctx, doc):
    from plugin.doc.text_helpers import iter_elements

    style, _name = _style(doc)
    style.setPropertyValue("HeaderIsOn", True)
    header = style.getPropertyValue("HeaderText")

    # [T, b]
    header.setString("")
    tbl = doc.createInstance("com.sun.star.text.TextTable")
    tbl.initialize(1, 1)
    header.insertTextContent(header.getStart(), tbl, False)
    tbl.getCellByName("A1").setString("c")
    cur = header.createTextCursor()
    cur.gotoEnd(False)
    header.insertString(cur, "b", False)
    res = _set(doc, ctx, "header", "<p>z</p>")
    assert res.get("status") == "ok", res
    assert _header_structure(header) == ["P:z"]

    # [a, T] without tail paragraph
    header.setString("a")
    tbl = doc.createInstance("com.sun.star.text.TextTable")
    tbl.initialize(1, 1)
    header.insertTextContent(header.getEnd(), tbl, False)
    tbl.getCellByName("A1").setString("c")
    elems = list(iter_elements(header.createEnumeration()))
    elems[-1].dispose()
    assert _header_structure(header) == ["P:a", "T"]
    res = _set(doc, ctx, "header", "<p>z</p>")
    assert res.get("status") == "ok", res
    assert _header_structure(header) == ["P:z"]

    # [T] only
    header.setString("")
    tbl = doc.createInstance("com.sun.star.text.TextTable")
    tbl.initialize(1, 1)
    header.insertTextContent(header.getStart(), tbl, False)
    tbl.getCellByName("A1").setString("c")
    elems = list(iter_elements(header.createEnumeration()))
    elems[-1].dispose()
    assert _header_structure(header) == ["T"]
    res = _set(doc, ctx, "header", "<p>z</p>")
    assert res.get("status") == "ok", res
    assert _header_structure(header) == ["P:z"]

    # Writing a table keeps a trailing paragraph of style Header, and round-trips cleanly.
    header.createTextCursor().setPropertyValue("ParaStyleName", "Header")
    res = _set(doc, ctx, "header", "<table><tr><td>h</td></tr></table>")
    assert res.get("status") == "ok", res
    assert _header_structure(header) == ["T", "P:"]
    elems = list(iter_elements(header.createEnumeration()))
    assert elems[-1].getPropertyValue("ParaStyleName") == "Header", elems[-1].getPropertyValue("ParaStyleName")

    for _ in range(2):
        got = _get(doc, ctx, "header")
        assert got.get("status") == "ok", got
        res = _set(doc, ctx, "header", got["content"])
        assert res.get("status") == "ok", res
        assert _header_structure(header) == ["T", "P:"]


@native_test
@with_native_doc("writer")
def test_header_table_recorded_accept_uno(ctx, doc):
    from plugin.framework.uno_context import execute_dispatch

    style, _name = _style(doc)
    style.setPropertyValue("HeaderIsOn", True)
    header = style.getPropertyValue("HeaderText")
    tbl = doc.createInstance("com.sun.star.text.TextTable")
    tbl.initialize(1, 1)
    header.insertTextContent(header.getStart(), tbl, False)
    tbl.getCellByName("A1").setString("c")
    cur = header.createTextCursor()
    cur.gotoEnd(False)
    header.insertString(cur, "b", False)

    doc.setPropertyValue("RecordChanges", True)
    try:
        res = _set(doc, ctx, "header", "<p>z</p>")
        assert res.get("status") == "ok", res
        execute_dispatch(ctx, doc.getCurrentController(), ".uno:AcceptAllTrackedChanges")
        assert _header_structure(header) == ["P:z", "P:"]
    finally:
        doc.setPropertyValue("RecordChanges", False)


@native_test
@with_native_doc("writer")
def test_header_table_cell_as_character_logo_round_trip_uno(ctx, doc):
    import uno
    from com.sun.star.text.TextContentAnchorType import AS_CHARACTER

    style, _name = _style(doc)
    style.setPropertyValue("HeaderIsOn", True)
    header = style.getPropertyValue("HeaderText")
    tbl = doc.createInstance("com.sun.star.text.TextTable")
    tbl.initialize(1, 1)
    header.insertTextContent(header.getStart(), tbl, False)
    cell = tbl.getCellByName("A1")

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(_PNG_BYTES)
        img_path = f.name
    try:
        g = doc.createInstance("com.sun.star.text.TextGraphicObject")
        g.setPropertyValue("GraphicURL", uno.systemPathToFileUrl(img_path))
        g.setPropertyValue("AnchorType", AS_CHARACTER)
        g.setPropertyValue("Width", 500)
        g.setPropertyValue("Height", 500)
        cell.insertTextContent(cell.getStart(), g, False)
    finally:
        os.unlink(img_path)

    for _ in range(2):
        got = _get(doc, ctx, "header")
        assert got.get("status") == "ok", got
        assert "<img" in got["content"], got
        res = _set(doc, ctx, "header", got["content"])
        assert res.get("status") == "ok", res

    graphics = doc.getGraphicObjects()
    assert graphics.getCount() == 1
    anchor_text = graphics.getByIndex(0).getAnchor().getText()
    assert anchor_text.supportsService("com.sun.star.text.CellProperties")


@native_test
@with_native_doc("writer")
def test_header_html_hides_multi_para_deletion_and_keeps_live_page_number_attrs_uno(ctx, doc):
    from plugin.tests.testing_utils import fill_five_paragraphs, strike_middle_three_paragraphs
    from plugin.writer.html_export import xtext_to_content

    style, _name = _style(doc)
    style.setPropertyValue("HeaderIsOn", True)
    header = style.getPropertyValue("HeaderText")
    paras = fill_five_paragraphs(header)

    # Insert both fields with RecordChanges off before striking so the middle field stays tracked-deleted.
    struck_field = doc.createInstance("com.sun.star.text.TextField.PageNumber")
    struck_field.setPropertyValue("NumberingType", 4)  # ARABIC (offapi/com/sun/star/style/NumberingType.idl)
    live_field = doc.createInstance("com.sun.star.text.TextField.PageNumber")
    live_field.setPropertyValue("NumberingType", 2)  # ROMAN_UPPER

    mid_cur = header.createTextCursorByRange(paras[2].getStart())
    header.insertTextContent(mid_cur, struck_field, False)
    end_cur = header.createTextCursorByRange(paras[4].getEnd())
    header.insertTextContent(end_cur, live_field, False)

    strike_middle_three_paragraphs(doc, header, paras)

    field_enum = doc.getTextFields().createEnumeration()
    page_fields = []
    while field_enum.hasMoreElements():
        f = field_enum.nextElement()
        if f.supportsService("com.sun.star.text.textfield.PageNumber"):
            page_fields.append(f)
    assert len(page_fields) == 2, page_fields

    html = xtext_to_content(header, doc, ctx)
    assert "gone" not in html, html
    assert html.count('data-numbering-type="ROMAN_UPPER"') == 1, html
