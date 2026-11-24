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


def _write_temp_png_file():
    fd, img_path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    with open(img_path, "wb") as f:
        f.write(_PNG_BYTES)
    return img_path


def _insert_header_logo(doc, target_text, img_path, anchor_type, *, name="LogoPic", title="ti", desc="de", offset=0, link_url=""):
    import uno

    g = doc.createInstance("com.sun.star.text.TextGraphicObject")
    g.setPropertyValue("GraphicURL", uno.systemPathToFileUrl(img_path))
    g.setPropertyValue("AnchorType", anchor_type)
    g.setPropertyValue("Width", 500)
    g.setPropertyValue("Height", 500)
    if offset or anchor_type.value != "AS_CHARACTER":
        g.setPropertyValue("HoriOrient", 0)
        g.setPropertyValue("HoriOrientPosition", 500)
        g.setPropertyValue("VertOrient", 0)
        g.setPropertyValue("VertOrientPosition", offset)
    cur = target_text.createTextCursor()
    cur.gotoEnd(False)
    target_text.insertTextContent(cur, g, False)
    g.setName(name)
    g.setPropertyValue("Title", title)
    g.setPropertyValue("Description", desc)
    if link_url:
        g.setPropertyValue("HyperLinkURL", link_url)
    return g


@native_test
def test_header_logo_roundtrip_matrix_uno(ctx):
    from com.sun.star.text.TextContentAnchorType import AS_CHARACTER, AT_CHARACTER, AT_PARAGRAPH
    from plugin.tests.testing_utils import TestingFactory

    img_path = _write_temp_png_file()
    try:
        for in_cell in (False, True):
            for anchor_type in (AS_CHARACTER, AT_CHARACTER, AT_PARAGRAPH):
                for include_images in (True, False):
                    doc = TestingFactory.create_native_doc(ctx, "writer")
                    try:
                        style, _ = _style(doc)
                        style.setPropertyValue("HeaderIsOn", True)
                        header = style.getPropertyValue("HeaderText")
                        if in_cell:
                            tbl = doc.createInstance("com.sun.star.text.TextTable")
                            tbl.initialize(1, 1)
                            header.insertTextContent(header.getStart(), tbl, False)
                            target = tbl.getCellByName("A1")
                        else:
                            target = header
                        target.setString("abc")
                        orig = _insert_header_logo(
                            doc,
                            target,
                            img_path,
                            anchor_type,
                            name="BrandLogo",
                            title="ti",
                            desc="de",
                            offset=300 if anchor_type == AT_PARAGRAPH else 0,
                        )
                        orig_pos = (orig.getPropertyValue("HoriOrientPosition"), orig.getPropertyValue("VertOrientPosition"))

                        for _ in range(2):
                            got = _get(doc, ctx, "header", include_images=include_images)
                            assert got.get("status") == "ok", got
                            res = _set(doc, ctx, "header", got["content"])
                            assert res.get("status") == "ok", res

                        assert doc.getTextFrames().getCount() == 0
                        graphics = doc.getGraphicObjects()
                        assert graphics.getCount() == 1
                        pic = graphics.getByIndex(0)
                        assert pic.getName() == "BrandLogo"
                        assert pic.getPropertyValue("AnchorType") == anchor_type
                        if anchor_type in (AT_PARAGRAPH, AT_CHARACTER):
                            assert (pic.getPropertyValue("HoriOrientPosition"), pic.getPropertyValue("VertOrientPosition")) == orig_pos
                        assert pic.getPropertyValue("Title") == "ti"
                        assert pic.getPropertyValue("Description") == "de"
                        if in_cell:
                            assert pic.getAnchor().getText().supportsService("com.sun.star.text.CellProperties")
                            assert doc.getTextTables().getByIndex(0).getCellByName("A1").getString().strip() == "abc"
                        else:
                            assert header.getString().strip() == "abc"

                        doc.getUndoManager().undo()
                        assert doc.getGraphicObjects().getCount() == 1
                    finally:
                        doc.close(True)
    finally:
        os.unlink(img_path)


@native_test
@with_native_doc("writer")
def test_header_logo_recording_hyperlink_refusals_and_nested_scan_uno(ctx, doc):
    from com.sun.star.text.TextContentAnchorType import AT_PARAGRAPH
    from unittest.mock import patch
    from plugin.framework.uno_context import execute_dispatch
    from plugin.writer import html_import
    from plugin.writer.page import _scan_region_content

    img_path = _write_temp_png_file()
    try:
        style, _ = _style(doc)
        style.setPropertyValue("HeaderIsOn", True)
        header = style.getPropertyValue("HeaderText")
        header.setString("abc")
        orig = _insert_header_logo(
            doc, header, img_path, AT_PARAGRAPH, name="LinkLogo", link_url="https://example.com/logo"
        )
        orig_size = orig.getSize()

        # Hyperlink and size survive round-trip.
        got = _get(doc, ctx, "header", include_images=True)
        assert _set(doc, ctx, "header", got["content"]).get("status") == "ok"
        pic = doc.getGraphicObjects().getByIndex(0)
        assert pic.getPropertyValue("HyperLinkURL") == "https://example.com/logo"
        assert (pic.getSize().Width, pic.getSize().Height) == (orig_size.Width, orig_size.Height)

        # Unknown src="" wrapper is refused and leaves header unchanged.
        err_img = _set(doc, ctx, "header", '<p>changed</p><div id="NoSuchPic"><img src=""/></div>')
        assert err_img.get("status") == "error"
        assert header.getString().strip() == "abc"

        # Failed table import inside header rolls back and leaves header unchanged.
        with patch.object(html_import, "insert_html_fragment_at_cursor"):
            err_tbl = _set(doc, ctx, "header", "<p>x</p><table><tr><td>c</td></tr></table>")
        assert err_tbl.get("status") == "error"
        assert err_tbl.get("code") == "TABLE_INSERT_FAILED", err_tbl
        assert "Table insert produced no table in document." in err_tbl.get("message", ""), err_tbl
        assert header.getString().strip() == "abc"

        # Recording: Accept and Reject each leave one logo for AT_PARAGRAPH, AS_CHARACTER,
        # AT_CHARACTER, and a cell original (fresh doc per case).
        from com.sun.star.text.TextContentAnchorType import AS_CHARACTER, AT_CHARACTER
        from plugin.tests.testing_utils import TestingFactory

        for rec_in_cell, rec_anchor in (
            (False, AT_PARAGRAPH),
            (False, AS_CHARACTER),
            (False, AT_CHARACTER),
            (True, AT_PARAGRAPH),
        ):
            rdoc = TestingFactory.create_native_doc(ctx, "writer")
            try:
                rstyle, _ = _style(rdoc)
                rstyle.setPropertyValue("HeaderIsOn", True)
                rheader = rstyle.getPropertyValue("HeaderText")
                if rec_in_cell:
                    tbl = rdoc.createInstance("com.sun.star.text.TextTable")
                    tbl.initialize(1, 1)
                    rheader.insertTextContent(rheader.getStart(), tbl, False)
                    rec_target = tbl.getCellByName("A1")
                else:
                    rec_target = rheader
                if rec_anchor == AT_CHARACTER:
                    # Anchor mid-paragraph so Accept deletes it and _insert_copy runs.
                    rec_target.setString("a")
                    _insert_header_logo(
                        rdoc, rec_target, img_path, rec_anchor, name=f"Rec_{rec_in_cell}_{rec_anchor.value}"
                    )
                    rcur = rec_target.createTextCursor()
                    rcur.gotoEnd(False)
                    rec_target.insertString(rcur, "bc", False)
                else:
                    rec_target.setString("abc")
                    _insert_header_logo(
                        rdoc, rec_target, img_path, rec_anchor, name=f"Rec_{rec_in_cell}_{rec_anchor.value}"
                    )
                rctrl = rdoc.getCurrentController()
                rdoc.setPropertyValue("RecordChanges", True)
                got_rec = _get(rdoc, ctx, "header", include_images=True)
                assert _set(rdoc, ctx, "header", got_rec["content"].replace("abc", "def")).get("status") == "ok"
                execute_dispatch(ctx, rctrl, ".uno:AcceptAllTrackedChanges")
                assert rdoc.getGraphicObjects().getCount() == 1, (
                    rec_in_cell,
                    rec_anchor,
                    rdoc.getGraphicObjects().getCount(),
                )

                got_rej = _get(rdoc, ctx, "header", include_images=True)
                assert _set(rdoc, ctx, "header", got_rej["content"].replace("def", "ghi")).get("status") == "ok"
                execute_dispatch(ctx, rctrl, ".uno:RejectAllTrackedChanges")
                assert rdoc.getGraphicObjects().getCount() == 1, (
                    rec_in_cell,
                    rec_anchor,
                    rdoc.getGraphicObjects().getCount(),
                )
            finally:
                rdoc.close(True)

        # Recorded AT_CHARACTER at the end of the last paragraph: 1-para header keeps the
        # surviving original; 2-para header deletes it on Accept and keeps the restored copy.
        for two_paras in (False, True):
            rdoc = TestingFactory.create_native_doc(ctx, "writer")
            try:
                rstyle, _ = _style(rdoc)
                rstyle.setPropertyValue("HeaderIsOn", True)
                rheader = rstyle.getPropertyValue("HeaderText")
                rcur = rheader.createTextCursor()
                rcur.gotoStart(False)
                rheader.insertString(rcur, "first", False)
                if two_paras:
                    rcur.gotoEnd(False)
                    rheader.insertControlCharacter(rcur, 0, False)
                    rcur.gotoEnd(False)
                    rheader.insertString(rcur, "second", False)
                _insert_header_logo(
                    rdoc, rheader, img_path, AT_CHARACTER, name=f"EndChar_{two_paras}"
                )
                rctrl = rdoc.getCurrentController()
                rdoc.setPropertyValue("RecordChanges", True)
                got = _get(rdoc, ctx, "header", include_images=True)
                assert _set(rdoc, ctx, "header", got["content"].replace("first", "FIRST")).get("status") == "ok"
                execute_dispatch(ctx, rctrl, ".uno:AcceptAllTrackedChanges")
                assert rdoc.getGraphicObjects().getCount() == 1, two_paras
            finally:
                rdoc.close(True)

        # Edge: wrapper copied from another region (body) creates a new logo in header and leaves body untouched.
        header.setString("")
        body = doc.getText()
        body.setString("body")
        _insert_header_logo(doc, body, img_path, AT_PARAGRAPH, name="BodyLogo")
        from plugin.writer.html_export import document_to_content

        body_html = document_to_content(doc, ctx, scope="full", include_images=False)
        assert _set(doc, ctx, "header", body_html).get("status") == "ok"
        assert doc.getGraphicObjects().getCount() == 2

        # Edge: unknown wrapper id WITH data imports inline and takes that name.
        import base64

        b64 = base64.b64encode(_PNG_BYTES).decode("ascii")
        assert _set(doc, ctx, "header", f'<p><span id="FreshLogo"><img src="data:image/png;base64,{b64}"/></span></p>').get("status") == "ok"
        assert doc.getGraphicObjects().hasByName("FreshLogo")

        # Nested-cell logo appears in _scan_region_content / images list.
        header.setString("")
        outer = doc.createInstance("com.sun.star.text.TextTable")
        outer.initialize(1, 1)
        header.insertTextContent(header.getStart(), outer, False)
        outer_cell = outer.getCellByName("A1")
        _insert_header_logo(doc, outer_cell, img_path, AT_PARAGRAPH, name="OuterCellLogo")
        inner = doc.createInstance("com.sun.star.text.TextTable")
        inner.initialize(1, 1)
        outer_cell.insertTextContent(outer_cell.getStart(), inner, False)
        _insert_header_logo(doc, inner.getCellByName("A1"), img_path, AT_PARAGRAPH, name="InnerCellLogo")
        scan = _scan_region_content(doc, header)
        assert {"OuterCellLogo", "InnerCellLogo"}.issubset(scan["images"]), scan["images"]
    finally:
        os.unlink(img_path)


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


@native_test
@with_native_doc("writer")
def test_header_table_delete_recorded_round_trip_stays_deleted_uno(ctx, doc):
    from plugin.framework.uno_context import execute_dispatch
    from plugin.writer.page import PageSetStyleProperties, _scan_region_content
    from plugin.writer.specialized.tables import TableDelete

    style, style_name = _style(doc)
    style.setPropertyValue("HeaderIsOn", True)
    header = style.getPropertyValue("HeaderText")
    cur = header.createTextCursor()
    header.insertString(cur, "top", False)
    header.insertControlCharacter(cur, 0, False)
    tbl = doc.createInstance("com.sun.star.text.TextTable")
    tbl.initialize(2, 2)
    header.insertTextContent(cur, tbl, False)
    for cell_name in ("A1", "B1", "A2", "B2"):
        tbl.getCellByName(cell_name).setString(cell_name)
    a1 = tbl.getCellByName("A1")
    a1_cur = a1.createTextCursor()
    a1_cur.gotoEnd(False)
    a1.insertTextContent(a1_cur, doc.createInstance("com.sun.star.text.textfield.PageNumber"), False)
    cur.gotoEnd(False)
    header.insertString(cur, "tail", False)

    ctrl = doc.getCurrentController()
    doc.setPropertyValue("RecordChanges", True)
    try:
        del_res = TableDelete().execute(_tool_ctx(doc, ctx), name=tbl.getName())
        assert del_res["status"] == "ok", del_res
        assert del_res.get("pending_review") is True, del_res
        assert "T" in _header_structure(header), _header_structure(header)

        got = _get(doc, ctx, "header")
        assert got["status"] == "ok"
        assert "<table" not in got["content"], got["content"]
        assert got.get("fields", []) == [], got
        assert got["paragraph_count"] == 2, got
        assert _scan_region_content(doc, header)["has_struck"] is True
        off_res = PageSetStyleProperties().execute(
            _tool_ctx(doc, ctx), style=style_name, header_is_on=False,
        )
        assert off_res["status"] == "error"

        assert _set(doc, ctx, "header", got["content"]).get("status") == "ok"
        execute_dispatch(ctx, ctrl, ".uno:AcceptAllTrackedChanges")
        assert "T" not in _header_structure(header), _header_structure(header)
    finally:
        doc.setPropertyValue("RecordChanges", False)


@native_test
@with_native_doc("writer")
def test_header_cell_text_struck_keeps_table_uno(ctx, doc):
    from plugin.framework.uno_context import execute_dispatch

    style, _name = _style(doc)
    style.setPropertyValue("HeaderIsOn", True)
    header = style.getPropertyValue("HeaderText")
    cur = header.createTextCursor()
    header.insertString(cur, "top", False)
    header.insertControlCharacter(cur, 0, False)
    tbl = doc.createInstance("com.sun.star.text.TextTable")
    tbl.initialize(1, 1)
    header.insertTextContent(cur, tbl, False)
    a1 = tbl.getCellByName("A1")
    a1.setString("cell text")
    cur.gotoEnd(False)
    header.insertString(cur, "tail A", False)

    doc.setPropertyValue("RecordChanges", True)
    try:
        c = a1.createTextCursor()
        c.gotoStart(False)
        c.gotoEnd(True)
        c.setString("")
        got = _get(doc, ctx, "header")
        assert got["status"] == "ok"
        assert "<table" in got["content"], got["content"]
        execute_dispatch(ctx, doc.getCurrentController(), ".uno:AcceptAllTrackedChanges")
        assert "T" in _header_structure(header), _header_structure(header)
    finally:
        doc.setPropertyValue("RecordChanges", False)


@native_test
@with_native_doc("writer")
def test_header_table_second_recorded_write_not_duplicated_uno(ctx, doc):
    from plugin.framework.uno_context import execute_dispatch

    style, _name = _style(doc)
    style.setPropertyValue("HeaderIsOn", True)
    header = style.getPropertyValue("HeaderText")
    cur = header.createTextCursor()
    header.insertString(cur, "top ", False)
    cur.gotoEnd(False)
    header.insertTextContent(cur, doc.createInstance("com.sun.star.text.textfield.PageNumber"), False)
    header.insertControlCharacter(cur, 0, False)
    tbl = doc.createInstance("com.sun.star.text.TextTable")
    tbl.initialize(1, 2)
    header.insertTextContent(cur, tbl, False)
    a1 = tbl.getCellByName("A1")
    a1.insertTextContent(a1.createTextCursor(), doc.createInstance("com.sun.star.text.textfield.PageCount"), False)
    cur.gotoEnd(False)
    header.insertString(cur, "tail", False)

    doc.setPropertyValue("RecordChanges", True)
    try:
        for _idx in range(2):
            got = _get(doc, ctx, "header")
            assert got["status"] == "ok"
            assert got["content"].count("<table") == 1, got["content"]
            assert len(got.get("fields", [])) == 2, got
            assert _set(doc, ctx, "header", got["content"]).get("status") == "ok"
        got_before_accept = _get(doc, ctx, "header")
        execute_dispatch(ctx, doc.getCurrentController(), ".uno:AcceptAllTrackedChanges")
        structure = _header_structure(header)
        assert structure.count("T") == 1, structure
        assert got_before_accept["paragraph_count"] == len(structure), (got_before_accept, structure)
    finally:
        doc.setPropertyValue("RecordChanges", False)


@native_test
@with_native_doc("writer")
def test_header_cell_fields_listed_uno(ctx, doc):
    style, _name = _style(doc)
    style.setPropertyValue("HeaderIsOn", True)
    header = style.getPropertyValue("HeaderText")
    tbl = doc.createInstance("com.sun.star.text.TextTable")
    tbl.initialize(1, 1)
    header.insertTextContent(header.getStart(), tbl, False)
    a1 = tbl.getCellByName("A1")
    a1.insertTextContent(a1.createTextCursor(), doc.createInstance("com.sun.star.text.textfield.PageNumber"), False)

    got = _get(doc, ctx, "header")
    assert got["status"] == "ok"
    assert len(got.get("fields", [])) == 1, got


@native_test
@with_native_doc("writer")
def test_header_html_decodes_html5_entities_uno(ctx, doc):
    res = _set(doc, ctx, "header", "<p>&check; &lt;b&gt;</p>")
    assert res.get("status") == "ok", res
    assert _region_text(doc, "header").getString() == "✓ <b>"


def _insert_at_para_logo(ctx, doc, header, at_end: bool, name: str = "LogoPic") -> None:
    from com.sun.star.beans import PropertyValue
    from com.sun.star.text.TextContentAnchorType import AT_PARAGRAPH

    fd, img_path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    with open(img_path, "wb") as f:
        f.write(_PNG_BYTES)
    try:
        provider = ctx.getServiceManager().createInstanceWithContext("com.sun.star.graphic.GraphicProvider", ctx)
        pv = PropertyValue()
        pv.Name = "URL"
        pv.Value = "file://" + img_path
        gfx = doc.createInstance("com.sun.star.text.TextGraphicObject")
        gfx.setPropertyValue("Graphic", provider.queryGraphic((pv,)))
        gfx.setPropertyValue("AnchorType", AT_PARAGRAPH)
        gfx.setPropertyValue("Width", 300)
        gfx.setPropertyValue("Height", 300)
        header.insertTextContent(header.getEnd() if at_end else header.getStart(), gfx, False)
        gfx.setName(name)
    finally:
        if os.path.exists(img_path):
            os.unlink(img_path)


@native_test
@with_native_doc("writer")
def test_header_recorded_round_trips_keep_one_logo_uno(ctx, doc):
    from plugin.framework.uno_context import execute_dispatch

    for case_idx, (lines, at_end) in enumerate([(["one", "two"], False), (["one", "two"], True), (["one"], False)]):
        style, _name = _style(doc)
        style.setPropertyValue("HeaderIsOn", True)
        header = style.getPropertyValue("HeaderText")
        doc.setPropertyValue("RecordChanges", False)
        header.setString("")
        cur = header.createTextCursor()
        for l_idx, line in enumerate(lines):
            if l_idx:
                header.insertControlCharacter(cur, 0, False)
            header.insertString(cur, line, False)
        _insert_at_para_logo(ctx, doc, header, at_end=at_end, name=f"Logo_{case_idx}")

        doc.setPropertyValue("RecordChanges", True)
        try:
            counts = []
            for _round in range(3):
                got = _get(doc, ctx, "header")
                assert got["status"] == "ok", got
                assert got["content"].count("<img") == 1, (case_idx, got["content"])
                assert len(got.get("images", [])) == 1, (case_idx, got)
                assert _set(doc, ctx, "header", got["content"]).get("status") == "ok"
                counts.append(len(_header_structure(header)))
            assert counts[1] == counts[0] and counts[2] == counts[0], (case_idx, counts)
            execute_dispatch(ctx, doc.getCurrentController(), ".uno:AcceptAllTrackedChanges")
            assert doc.getGraphicObjects().getCount() == 1, (case_idx, doc.getGraphicObjects().getElementNames())
            struct = _header_structure(header)
            assert struct == [f"P:{line}" for line in lines] + ["P:"], (case_idx, struct)
        finally:
            doc.setPropertyValue("RecordChanges", False)
            execute_dispatch(ctx, doc.getCurrentController(), ".uno:AcceptAllTrackedChanges")
            header.setString("")
            for gname in list(doc.getGraphicObjects().getElementNames()):
                doc.getGraphicObjects().getByName(gname).dispose()


@native_test
@with_native_doc("writer")
def test_header_recorded_table_rewrites_do_not_grow_uno(ctx, doc):
    from plugin.framework.uno_context import execute_dispatch

    style, _name = _style(doc)
    style.setPropertyValue("HeaderIsOn", True)
    header = style.getPropertyValue("HeaderText")
    header.setString("a\nb")
    cur = header.createTextCursor()
    cur.gotoStart(False)
    cur.goRight(1, False)
    tbl = doc.createInstance("com.sun.star.text.TextTable")
    tbl.initialize(1, 1)
    header.insertTextContent(cur, tbl, False)
    orig_structure = _header_structure(header)

    doc.setPropertyValue("RecordChanges", True)
    try:
        counts = []
        for _round in range(3):
            res = _set(doc, ctx, "header", "<p>n</p><table><tr><td>c</td></tr></table><p>z</p>")
            assert res["status"] == "ok", res
            counts.append(len(_header_structure(header)))
        assert len(set(counts)) == 1, counts
        execute_dispatch(ctx, doc.getCurrentController(), ".uno:RejectAllTrackedChanges")
        assert _header_structure(header) == orig_structure
    finally:
        doc.setPropertyValue("RecordChanges", False)


@native_test
@with_native_doc("writer")
def test_header_struck_logo_blocks_disable_uno(ctx, doc):
    from plugin.writer.page import PageSetStyleProperties

    style, s_name = _style(doc)
    style.setPropertyValue("HeaderIsOn", True)
    header = style.getPropertyValue("HeaderText")
    header.setString("x")
    _insert_at_para_logo(ctx, doc, header, at_end=False, name="StruckOnlyLogo")

    doc.setPropertyValue("RecordChanges", True)
    try:
        cur = header.createTextCursor()
        cur.gotoStart(False)
        cur.gotoEnd(True)
        cur.setString("")
        got = _get(doc, ctx, "header")
        assert got["status"] == "ok", got
        assert not got.get("images"), got
        res = PageSetStyleProperties().execute(_tool_ctx(doc, ctx), style=s_name, header_is_on=False)
        assert res["status"] == "error", res
    finally:
        doc.setPropertyValue("RecordChanges", False)


@native_test
@with_native_doc("writer")
def test_header_recorded_trailing_table_without_tail_clears_uno(ctx, doc):
    from plugin.doc.text_helpers import iter_elements
    from plugin.framework.uno_context import execute_dispatch

    style, _name = _style(doc)
    style.setPropertyValue("HeaderIsOn", True)
    header = style.getPropertyValue("HeaderText")
    header.setString("a")
    tbl = doc.createInstance("com.sun.star.text.TextTable")
    tbl.initialize(1, 1)
    header.insertTextContent(header.getEnd(), tbl, False)
    tbl.getCellByName("A1").setString("c")
    elems = list(iter_elements(header.createEnumeration()))
    elems[-1].dispose()
    assert _header_structure(header) == ["P:a", "T"]

    doc.setPropertyValue("RecordChanges", True)
    try:
        res = _set(doc, ctx, "header", "<p>z</p>")
        assert res["status"] == "ok", res
        execute_dispatch(ctx, doc.getCurrentController(), ".uno:AcceptAllTrackedChanges")
        struct = _header_structure(header)
        assert struct == ["P:z", "P:"], struct
    finally:
        doc.setPropertyValue("RecordChanges", False)
