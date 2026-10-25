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
    with_native_doc,
    writer_services_tool_context,
)
from com.sun.star.text.TextContentAnchorType import AT_CHARACTER, AT_PARAGRAPH

from plugin.writer.images.image_tools import (
    _file_url_for_path,
    _graphic_from_provider,
    insert_image_into_header_footer,
)
from plugin.writer.page import (
    PageGetHeaderFooterText,
    PageReplaceHeaderFooterText,
    PageSetHeaderFooterText,
    PageSetStyleProperties,
    _scan_region_content,
    copy_page_style,
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
    return writer_services_tool_context(
        doc, ctx, skip_reason="page_header Hidden _blank xtext_to_content"
    )


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
    # Shared first page is the fixture: leftover FirstIsShared=False +
    # header_first from a prior letterhead test is not this scenario
    # (GHA 35466498641). Product also skips mirror regions when shared.
    style, _name = _style(doc)
    style.setPropertyValue("FirstIsShared", True)

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
    style.setPropertyValue("FirstIsShared", True)
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
    assert "still has content" in res["message"]
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


@native_test
@with_native_doc("writer")
def test_page_insert_break_no_anchor(ctx, doc):
    from plugin.writer.page import PageInsertBreak

    text = doc.getText()
    cursor = text.createTextCursor()
    cursor.setString("This is a paragraph with some text.")

    view_cursor = doc.getCurrentController().getViewCursor()
    view_cursor.gotoStart(False)
    view_cursor.goRight(10, True) # Select "This is a "

    assert view_cursor.getString() == "This is a "

    tool = PageInsertBreak()
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    res = tool.execute(tool_ctx)

    assert res["status"] == "ok"

    # SwXTextViewCursor implements XTextViewCursor, not XParagraphCursor.
    # gotoEndOfParagraph exists only on a model TextCursor and always raises
    # AttributeError here, so the paragraph checks never ran. gotoStart would
    # also drop the selection this path must leave on the original ten characters.
    assert view_cursor.getString() == "This is a "

    para = text.createTextCursorByRange(view_cursor.getStart())
    para.gotoStartOfParagraph(False)
    para.gotoEndOfParagraph(True)
    assert para.getString() == "This is a paragraph with some text."

    from com.sun.star.style.BreakType import PAGE_BEFORE
    assert para.getPropertyValue("BreakType") == PAGE_BEFORE
    assert view_cursor.getString() == "This is a "


def _para_with_text(doc, needle):
    enum = doc.getText().createEnumeration()
    while enum.hasMoreElements():
        para = enum.nextElement()
        try:
            if needle in para.getString():
                return para
        except Exception:
            continue
    return None


@native_test
@with_native_doc("writer")
def test_page_insert_break_applies_cloned_style_and_restarts_numbering(ctx, doc):
    from plugin.writer.page import PageInsertBreak
    from com.sun.star.text.ControlCharacter import PARAGRAPH_BREAK
    from com.sun.star.style.BreakType import PAGE_BEFORE, NONE

    pages = doc.getStyleFamilies().getByName("PageStyles")
    if pages.hasByName("AnnexRestart1424"):
        pages.removeByName("AnnexRestart1424")
    copy_page_style(doc, _style_name(doc), "AnnexRestart1424", uno_ctx=ctx)

    text = doc.getText()
    cursor = text.createTextCursor()
    cursor.gotoStart(False)
    cursor.setPropertyValue("PageDescName", "")
    cursor.setPropertyValue("BreakType", NONE)
    cursor.setPropertyToDefault("PageNumberOffset")
    text.insertString(cursor, "Body stays.", False)
    text.insertControlCharacter(cursor, PARAGRAPH_BREAK, False)
    text.insertString(cursor, "Annex start", False)

    tool = PageInsertBreak()
    res = tool.execute(
        _tool_ctx(doc, ctx),
        before_text="Annex start",
        page_style="AnnexRestart1424",
        restart_page_number=1,
    )
    assert res["status"] == "ok"
    assert res["page_style"] == "AnnexRestart1424"
    assert res["restart_page_number"] == 1
    assert "before" in res["message"]
    assert set(res) == {"status", "message", "page_style", "restart_page_number"}

    annex_para = _para_with_text(doc, "Annex start")
    assert annex_para is not None
    assert annex_para.getPropertyValue("BreakType") == PAGE_BEFORE
    assert annex_para.getPropertyValue("PageDescName") == "AnnexRestart1424"
    assert int(annex_para.getPropertyValue("PageNumberOffset")) == 1

    body_para = _para_with_text(doc, "Body stays.")
    assert body_para is not None
    assert body_para.getPropertyValue("BreakType") != PAGE_BEFORE
    assert body_para.getPropertyValue("PageDescName") != "AnnexRestart1424"


@native_test
@with_native_doc("writer")
def test_page_insert_break_after_text_styles_the_following_paragraph(ctx, doc):
    from plugin.writer.page import PageInsertBreak
    from com.sun.star.text.ControlCharacter import PARAGRAPH_BREAK
    from com.sun.star.style.BreakType import PAGE_BEFORE, NONE

    pages = doc.getStyleFamilies().getByName("PageStyles")
    if pages.hasByName("AnnexAfter1424"):
        pages.removeByName("AnnexAfter1424")
    copy_page_style(doc, _style_name(doc), "AnnexAfter1424", uno_ctx=ctx)

    text = doc.getText()
    cursor = text.createTextCursor()
    cursor.gotoStart(False)
    cursor.setPropertyValue("PageDescName", "")
    cursor.setPropertyValue("BreakType", NONE)
    cursor.setPropertyToDefault("PageNumberOffset")
    text.insertString(cursor, "Lead clause", False)
    text.insertControlCharacter(cursor, PARAGRAPH_BREAK, False)
    text.insertString(cursor, "Annex start", False)

    tool = PageInsertBreak()
    res = tool.execute(
        _tool_ctx(doc, ctx),
        after_text="Lead clause",
        page_style="AnnexAfter1424",
        restart_page_number=3,
    )
    assert res["status"] == "ok"
    assert "after" in res["message"]

    annex_para = _para_with_text(doc, "Annex start")
    assert annex_para is not None
    assert annex_para.getPropertyValue("BreakType") == PAGE_BEFORE
    assert annex_para.getPropertyValue("PageDescName") == "AnnexAfter1424"
    assert int(annex_para.getPropertyValue("PageNumberOffset")) == 3

    lead_para = _para_with_text(doc, "Lead clause")
    assert lead_para is not None
    assert lead_para.getPropertyValue("BreakType") != PAGE_BEFORE
    assert lead_para.getPropertyValue("PageDescName") != "AnnexAfter1424"


@native_test
@with_native_doc("writer")
def test_page_insert_break_unknown_style_changes_nothing(ctx, doc):
    from plugin.writer.page import PageInsertBreak
    from com.sun.star.style.BreakType import PAGE_BEFORE, NONE

    text = doc.getText()
    cursor = text.createTextCursor()
    cursor.gotoStart(False)
    cursor.setPropertyValue("PageDescName", "")
    cursor.setPropertyValue("BreakType", NONE)
    cursor.setPropertyToDefault("PageNumberOffset")
    text.insertString(cursor, "Annex start", False)

    tool = PageInsertBreak()
    res = tool.execute(
        _tool_ctx(doc, ctx),
        before_text="Annex start",
        page_style="MissingAnnex1424",
    )
    assert res["status"] == "error"
    assert "No page style named 'MissingAnnex1424'" in res["message"]

    para = _para_with_text(doc, "Annex start")
    assert para is not None
    assert "Annex start" in para.getString()
    assert para.getPropertyValue("BreakType") != PAGE_BEFORE
    assert para.getPropertyValue("PageDescName") != "MissingAnnex1424"
    assert int(para.getPropertyValue("PageNumberOffset") or 0) == 0


@native_test
@with_native_doc("writer")
def test_page_insert_break_after_last_paragraph_does_not_apply_style(ctx, doc):
    from plugin.writer.page import PageInsertBreak
    from com.sun.star.style.BreakType import PAGE_BEFORE, NONE

    pages = doc.getStyleFamilies().getByName("PageStyles")
    if pages.hasByName("AnnexLast1424"):
        pages.removeByName("AnnexLast1424")
    copy_page_style(doc, _style_name(doc), "AnnexLast1424", uno_ctx=ctx)

    text = doc.getText()
    cursor = text.createTextCursor()
    cursor.gotoStart(False)
    cursor.setPropertyValue("PageDescName", "")
    cursor.setPropertyValue("BreakType", NONE)
    cursor.setPropertyToDefault("PageNumberOffset")
    text.insertString(cursor, "Only paragraph", False)

    tool = PageInsertBreak()
    res = tool.execute(
        _tool_ctx(doc, ctx),
        after_text="Only paragraph",
        page_style="AnnexLast1424",
        restart_page_number=1,
    )
    assert res["status"] == "error"
    assert "last paragraph" in res["message"]

    para = _para_with_text(doc, "Only paragraph")
    assert para is not None
    assert para.getPropertyValue("BreakType") != PAGE_BEFORE
    assert para.getPropertyValue("PageDescName") != "AnnexLast1424"


@native_test
@with_native_doc("writer")
def test_page_insert_break_view_cursor_applies_style_and_keeps_selection(ctx, doc):
    from plugin.writer.page import PageInsertBreak
    from com.sun.star.style.BreakType import PAGE_BEFORE

    pages = doc.getStyleFamilies().getByName("PageStyles")
    if pages.hasByName("AnnexCursor1424"):
        pages.removeByName("AnnexCursor1424")
    copy_page_style(doc, _style_name(doc), "AnnexCursor1424", uno_ctx=ctx)

    text = doc.getText()
    cursor = text.createTextCursor()
    cursor.setString("This is a paragraph with some text.")

    view_cursor = doc.getCurrentController().getViewCursor()
    view_cursor.gotoStart(False)
    view_cursor.goRight(10, True)  # Select "This is a "

    assert view_cursor.getString() == "This is a "

    tool = PageInsertBreak()
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    res = tool.execute(tool_ctx, page_style="AnnexCursor1424", restart_page_number=2)

    assert res["status"] == "ok"
    assert res["message"] == "Page break inserted."
    assert view_cursor.getString() == "This is a "

    para = text.createTextCursorByRange(view_cursor.getStart())
    para.gotoStartOfParagraph(False)
    para.gotoEndOfParagraph(True)
    assert para.getString() == "This is a paragraph with some text."

    assert para.getPropertyValue("BreakType") == PAGE_BEFORE
    assert para.getPropertyValue("PageDescName") == "AnnexCursor1424"
    assert int(para.getPropertyValue("PageNumberOffset")) == 2
    assert view_cursor.getString() == "This is a "


def _shapes_anchored_in(doc, text_obj):
    found = []
    draw_page = doc.getDrawPage()
    for i in range(int(draw_page.getCount())):
        shape = draw_page.getByIndex(i)
        try:
            if uno_same(shape.getAnchor().getText(), text_obj):
                found.append(shape)
        except Exception:
            continue
    return found


@native_test
@with_native_doc("writer")
def test_copy_page_style_keeps_footer_field_and_graphic(ctx, doc):
    """Direct margin, footer field, and a floating hyperlink survive; the source shape stays.

    Probes C1, C3, C4. HTML import does not carry AT_PARAGRAPH graphics, so the
    cloner re-creates them on the draw page.
    """
    logo = _header_logo_path()
    assert os.path.isfile(logo), "fixture image missing: %s" % logo
    graphic = _graphic_from_provider(ctx, _file_url_for_path(logo))
    assert graphic is not None, "GraphicProvider could not load the fixture"

    pages = doc.getStyleFamilies().getByName("PageStyles")
    if pages.hasByName("AnnexFooterClone"):
        pages.removeByName("AnnexFooterClone")

    style, name = _style(doc)
    style.setPropertyValue("FooterIsOn", True)
    style.setPropertyValue("LeftMargin", 1234)
    footer = style.getPropertyValue("FooterText")
    footer.setString("Annex 1 ")
    cursor = footer.createTextCursor()
    cursor.gotoEnd(False)
    field = doc.createInstance("com.sun.star.text.textfield.PageNumber")
    footer.insertTextContent(cursor, field, False)

    shape = doc.createInstance("com.sun.star.text.TextGraphicObject")
    shape.setPropertyValue("Graphic", graphic)
    shape.setPropertyValue("AnchorType", AT_PARAGRAPH)
    shape.setPropertyValue("Width", 1500)
    shape.setPropertyValue("Height", 1500)
    shape.setPropertyValue("HyperLinkURL", "https://example.com/index")
    shape.setPropertyValue("Title", "back-to-index")
    cursor.gotoEnd(False)
    footer.insertTextContent(cursor, shape, False)
    source_url = shape.getPropertyValue("HyperLinkURL")
    source_scan = _scan_region_content(doc, footer)
    assert source_scan["fields"], source_scan

    stats = copy_page_style(doc, name, "AnnexFooterClone", uno_ctx=ctx)

    assert stats["shapes_copied"] >= 1, stats
    clone = pages.getByName("AnnexFooterClone")
    assert clone.getPropertyValue("FollowStyle") == "AnnexFooterClone"
    assert abs(int(clone.getPropertyValue("LeftMargin")) - int(style.getPropertyValue("LeftMargin"))) <= 2

    clone_footer = clone.getPropertyValue("FooterText")
    assert "Annex" in clone_footer.getString(), clone_footer.getString()
    scan = _scan_region_content(doc, clone_footer)
    assert scan["fields"], (scan, clone_footer.getString())

    clones = _shapes_anchored_in(doc, clone_footer)
    linked = [s for s in clones if s.getPropertyValue("HyperLinkURL") == "https://example.com/index"]
    assert linked, "clone footer lost the floating hyperlink"
    assert linked[0].getPropertyValue("Title") == "back-to-index"

    sources = _shapes_anchored_in(doc, footer)
    assert sources, "source footer graphic was moved instead of copied"
    assert any(s.getPropertyValue("HyperLinkURL") == source_url for s in sources)


@native_test
@with_native_doc("writer")
def test_replace_footer_keeps_field_and_graphic(ctx, doc):
    """Title and a literal after a field change; the page field and hyperlink stay.

    Probes A and A'. The date field's visible text is longer than one cursor step,
    so a getString() index would miss "274". "Annex 1: Intro" is split by a bold
    run so the match has to span portions.
    """
    logo = _header_logo_path()
    assert os.path.isfile(logo), "fixture image missing: %s" % logo
    graphic = _graphic_from_provider(ctx, _file_url_for_path(logo))
    assert graphic is not None, "GraphicProvider could not load the fixture"

    style, name = _style(doc)
    style.setPropertyValue("FooterIsOn", True)
    footer = style.getPropertyValue("FooterText")
    footer.setString("Annex 1: Intro")
    bold = footer.createTextCursor()
    bold.gotoStart(False)
    bold.goRight(len("Annex"), True)
    bold.setPropertyValue("CharWeight", 150.0)

    cursor = footer.createTextCursor()
    cursor.gotoEnd(False)
    page_field = doc.createInstance("com.sun.star.text.textfield.PageNumber")
    footer.insertTextContent(cursor, page_field, False)
    cursor.gotoEnd(False)
    footer.insertString(cursor, " ", False)
    cursor.gotoEnd(False)
    date_field = doc.createInstance("com.sun.star.text.textfield.DateTime")
    footer.insertTextContent(cursor, date_field, False)
    shown = date_field.getPresentation(False)
    assert isinstance(shown, str) and len(shown) != 1, shown
    cursor.gotoEnd(False)
    footer.insertString(cursor, "274", False)

    shape = doc.createInstance("com.sun.star.text.TextGraphicObject")
    shape.setPropertyValue("Graphic", graphic)
    shape.setPropertyValue("AnchorType", AT_PARAGRAPH)
    shape.setPropertyValue("Width", 1500)
    shape.setPropertyValue("Height", 1500)
    shape.setPropertyValue("HyperLinkURL", "https://example.com/index")
    cursor.gotoEnd(False)
    footer.insertTextContent(cursor, shape, False)

    tool = PageReplaceHeaderFooterText()
    tool_ctx = _tool_ctx(doc, ctx)
    title = tool.execute(
        tool_ctx, style=name, region="footer",
        old_content="Annex 1: Intro", content="Annex A: Methods",
    )
    assert title["status"] == "ok", title
    assert title["replaced_count"] == 1
    assert title["fields_preserved"] >= 1
    assert title["shapes_preserved"] >= 1

    literal = tool.execute(
        tool_ctx, style=name, region="footer", old_content="274", content="300",
    )
    assert literal["status"] == "ok", literal
    plain = footer.getString()
    assert "Annex A: Methods" in plain, plain
    assert "274" not in plain, plain
    assert "300" in plain, plain
    assert "Annex 1" not in plain, plain
    scan = _scan_region_content(doc, footer)
    assert len(scan["fields"]) >= 2, scan
    linked = [
        s for s in _shapes_anchored_in(doc, footer)
        if s.getPropertyValue("HyperLinkURL") == "https://example.com/index"
    ]
    assert linked, "footer graphic or its hyperlink was dropped"


@native_test
@with_native_doc("writer")
def test_replace_footer_refuses_character_anchor_inside_match(ctx, doc):
    """AT_CHARACTER inside the match is refused and the footer text stays."""
    logo = _header_logo_path()
    graphic = _graphic_from_provider(ctx, _file_url_for_path(logo))
    assert graphic is not None

    style, name = _style(doc)
    style.setPropertyValue("FooterIsOn", True)
    footer = style.getPropertyValue("FooterText")
    footer.setString("Annex 1: Intro")
    cursor = footer.createTextCursor()
    cursor.gotoStart(False)
    cursor.goRight(len("Annex "), False)
    shape = doc.createInstance("com.sun.star.text.TextGraphicObject")
    shape.setPropertyValue("Graphic", graphic)
    shape.setPropertyValue("AnchorType", AT_CHARACTER)
    shape.setPropertyValue("Width", 800)
    shape.setPropertyValue("Height", 800)
    footer.insertTextContent(cursor, shape, False)
    before = footer.getString()

    res = PageReplaceHeaderFooterText().execute(
        _tool_ctx(doc, ctx), style=name, region="footer",
        old_content="Annex 1: Intro", content="Annex A: Methods",
    )
    assert res["status"] == "error", res
    assert footer.getString() == before
    assert _shapes_anchored_in(doc, footer), "refuse path removed the graphic"

@native_test
@with_native_doc("writer")
def test_copy_page_style_clones_height_and_fields_correctly(ctx, doc):
    """
    Verifies fixes for discussion #1424:
    1. Header/footer heights and body distances match the source.
    2. Footer paragraph count doesn't increase (no trailing empty para).
    3. PageNumber fields retain SubType and aren't reset to previous-page (0).
    """
    from com.sun.star.text.PageNumberType import CURRENT

    pages = doc.getStyleFamilies().getByName("PageStyles")
    if pages.hasByName("CloneBug1424"):
        pages.removeByName("CloneBug1424")

    style, name = _style(doc)
    style.setPropertyValue("HeaderIsOn", True)
    style.setPropertyValue("HeaderBodyDistance", 500)
    style.setPropertyValue("HeaderHeight", 1500)

    style.setPropertyValue("FooterIsOn", True)
    style.setPropertyValue("FooterBodyDistance", 600)
    style.setPropertyValue("FooterHeight", 1200)

    # Set up footer with a CURRENT PageNumber field and ONLY 1 paragraph
    footer = style.getPropertyValue("FooterText")
    footer.setString("Page ")
    cursor = footer.createTextCursor()
    cursor.gotoEnd(False)

    field = doc.createInstance("com.sun.star.text.textfield.PageNumber")
    field.setPropertyValue("SubType", CURRENT)
    footer.insertTextContent(cursor, field, False)

    from plugin.writer.page import copy_page_style
    # Clone the page style
    stats = copy_page_style(doc, name, "CloneBug1424", uno_ctx=ctx)
    assert stats["properties_copied"] > 0

    clone = pages.getByName("CloneBug1424")

    # 1. Check heights and distances
    assert abs(int(clone.getPropertyValue("HeaderBodyDistance")) - 500) <= 2
    assert clone.getPropertyValue("HeaderBodyDistance") == style.getPropertyValue("HeaderBodyDistance")
    assert abs(int(clone.getPropertyValue("HeaderHeight")) - 1500) <= 2
    assert clone.getPropertyValue("HeaderHeight") == style.getPropertyValue("HeaderHeight")
    assert abs(int(clone.getPropertyValue("FooterBodyDistance")) - 600) <= 2
    assert clone.getPropertyValue("FooterBodyDistance") == style.getPropertyValue("FooterBodyDistance")
    assert abs(int(clone.getPropertyValue("FooterHeight")) - 1200) <= 2
    assert clone.getPropertyValue("FooterHeight") == style.getPropertyValue("FooterHeight")

    # 2. Check paragraph count
    clone_footer = clone.getPropertyValue("FooterText")
    enum = clone_footer.createEnumeration()
    para_count = 0
    while enum.hasMoreElements():
        enum.nextElement()
        para_count += 1
    assert para_count == 1, "Cloned footer should have exactly 1 paragraph, got %d" % para_count

    # 3. Check PageNumber field properties (SubType)
    # Get the fields from the clone_footer
    scan = _scan_region_content(doc, clone_footer)
    assert scan["fields"], "No fields found in cloned footer"

    # We inserted 1 field, find its SubType
    # We can walk the cloned footer to find the textfield
    clone_cursor = clone_footer.createTextCursor()
    clone_cursor.gotoStart(False)

    enum = clone_footer.createEnumeration()
    found_field = False
    while enum.hasMoreElements():
        para = enum.nextElement()
        portions = para.createEnumeration()
        while portions.hasMoreElements():
            portion = portions.nextElement()
            if portion.getPropertyValue("TextPortionType") == "TextField":
                cloned_field = portion.getPropertyValue("TextField")
                if cloned_field.supportsService("com.sun.star.text.textfield.PageNumber"):
                    found_field = True
                    # Assert it's CURRENT (1)
                    assert cloned_field.getPropertyValue("SubType") == CURRENT
    assert found_field, "Could not locate cloned PageNumber field to check SubType"

@native_test
@with_native_doc("writer")
def test_copy_page_style_clones_next_page_number_and_roman(ctx, doc):
    """
    Verifies NEXT-page field with offset 1 and roman numeral survives clone.
    """
    from com.sun.star.text.PageNumberType import NEXT

    pages = doc.getStyleFamilies().getByName("PageStyles")
    if pages.hasByName("CloneBug1424_Next"):
        pages.removeByName("CloneBug1424_Next")

    style, name = _style(doc)
    style.setPropertyValue("HeaderIsOn", True)
    header = style.getPropertyValue("HeaderText")
    header.setString("Next Page: ")
    cursor = header.createTextCursor()
    cursor.gotoEnd(False)

    field = doc.createInstance("com.sun.star.text.textfield.PageNumber")
    field.setPropertyValue("SubType", NEXT)
    field.setPropertyValue("Offset", 1)
    field.setPropertyValue("NumberingType", 2) # ROMAN_UPPER
    header.insertTextContent(cursor, field, False)

    from plugin.writer.page import copy_page_style
    # Clone the page style
    stats = copy_page_style(doc, name, "CloneBug1424_Next", uno_ctx=ctx)
    assert stats["properties_copied"] > 0

    clone = pages.getByName("CloneBug1424_Next")
    clone_header = clone.getPropertyValue("HeaderText")

    enum = clone_header.createEnumeration()
    found_field = False
    while enum.hasMoreElements():
        para = enum.nextElement()
        portions = para.createEnumeration()
        while portions.hasMoreElements():
            portion = portions.nextElement()
            if portion.getPropertyValue("TextPortionType") == "TextField":
                cloned_field = portion.getPropertyValue("TextField")
                if cloned_field.supportsService("com.sun.star.text.textfield.PageNumber"):
                    found_field = True
                    assert cloned_field.getPropertyValue("SubType") == NEXT
                    assert cloned_field.getPropertyValue("Offset") == 1
                    assert cloned_field.getPropertyValue("NumberingType") == 2
    assert found_field, "Could not locate cloned PageNumber field to check properties"


@native_test
@with_native_doc("writer")
def test_is_landscape_does_not_swap_unless_sizes_passed_uno(ctx, doc):
    style, name = _style(doc)
    w_snap = int(style.getPropertyValue("Width"))
    h_snap = int(style.getPropertyValue("Height"))

    res1 = _set_props(doc, ctx, is_landscape=True)
    assert res1["status"] == "ok", res1
    assert style.getPropertyValue("IsLandscape") is True
    assert int(style.getPropertyValue("Width")) == w_snap
    assert int(style.getPropertyValue("Height")) == h_snap

    res2 = _set_props(doc, ctx, is_landscape=True, width_mm=297, height_mm=210)
    assert res2["status"] == "ok", res2
    assert style.getPropertyValue("IsLandscape") is True
    assert abs(int(style.getPropertyValue("Width")) - 29700) <= 2
    assert abs(int(style.getPropertyValue("Height")) - 21000) <= 2


@native_test
@with_native_doc("writer")
def test_numbering_type_roman_upper_uno(ctx, doc):
    from plugin.writer.page import get_page_style_properties

    res = _set_props(doc, ctx, numbering_type=2)
    assert res["status"] == "ok", res

    props = get_page_style_properties(doc, _style_name(doc))
    assert props["properties"]["numbering_type"] == 2


@native_test
@with_native_doc("writer")
def test_page_break_inside_table_cell_uno(ctx, doc):
    from plugin.writer.page import PageInsertBreak

    text = doc.getText()
    cursor = text.createTextCursor()
    cursor.gotoEnd(False)
    table = doc.createInstance("com.sun.star.text.TextTable")
    table.initialize(1, 1)
    text.insertTextContent(cursor, table, False)

    cell = table.getCellByName("A1")
    cell_text = cell.getText()
    cell_cursor = cell_text.createTextCursor()
    cell_cursor.setString("UniqueCellWord")

    tool = PageInsertBreak()
    res = tool.execute(_tool_ctx(doc, ctx), before_text="UniqueCellWord")
    assert res["status"] == "error", res
    assert "table cell" in res["message"]

    from com.sun.star.style.BreakType import PAGE_BEFORE
    assert cell_cursor.getPropertyValue("BreakType") != PAGE_BEFORE


@native_test
@with_native_doc("writer")
def test_page_break_inside_table_cell_with_page_style_uno(ctx, doc):
    from plugin.writer.page import PageInsertBreak
    from com.sun.star.style.BreakType import PAGE_BEFORE

    pages = doc.getStyleFamilies().getByName("PageStyles")
    if pages.hasByName("AnnexCell1424"):
        pages.removeByName("AnnexCell1424")
    copy_page_style(doc, _style_name(doc), "AnnexCell1424", uno_ctx=ctx)

    text = doc.getText()
    cursor = text.createTextCursor()
    cursor.gotoEnd(False)
    table = doc.createInstance("com.sun.star.text.TextTable")
    table.initialize(1, 1)
    text.insertTextContent(cursor, table, False)

    cell = table.getCellByName("A1")
    cell_text = cell.getText()
    cell_cursor = cell_text.createTextCursor()
    cell_cursor.setString("UniqueCellWord")

    tool = PageInsertBreak()
    res = tool.execute(
        _tool_ctx(doc, ctx),
        before_text="UniqueCellWord",
        page_style="AnnexCell1424",
        restart_page_number=1,
    )
    assert res["status"] == "error", res
    assert "table cell" in res["message"]

    assert cell_cursor.getPropertyValue("BreakType") != PAGE_BEFORE
    assert cell_cursor.getPropertyValue("PageDescName") != "AnnexCell1424"


@native_test
@with_native_doc("writer")
def test_column_spacing_zero_uno(ctx, doc):
    from plugin.writer.page import PageSetColumns, PageGetColumns

    set_tool = PageSetColumns()
    get_tool = PageGetColumns()
    tool_ctx = _tool_ctx(doc, ctx)
    s_name = _style_name(doc)

    res1 = set_tool.execute(tool_ctx, style=s_name, column_count=2, spacing_mm=5)
    assert res1["status"] == "ok", res1

    res2 = set_tool.execute(tool_ctx, style=s_name, column_count=2, spacing_mm=0)
    assert res2["status"] == "ok", res2

    cols_res = get_tool.execute(tool_ctx, style=s_name)
    assert cols_res["status"] == "ok", cols_res
    cols = cols_res["columns"]
    assert len(cols) == 2
    assert cols[0]["right_margin_mm"] == 0.0
    assert cols[1]["left_margin_mm"] == 0.0

