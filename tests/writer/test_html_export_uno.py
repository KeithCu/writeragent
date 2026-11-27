# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""UNO tests for Writer HTML range export (html_export)."""

from typing import Any

import uno  # noqa: F401

from plugin.framework.uno_context import enum_has_more
from plugin.testing_runner import native_test
from tests.testing_utils import TestingFactory, with_native_doc
from tests.writer.doc_builders import accept_all_changes, body_shape


@native_test
@with_native_doc("writer")
def test_range_export_keeps_bold_inside_odd_para_uno(ctx: Any, doc: Any) -> None:
    """Range export preserves emphasis even when the source paragraph carries unusual Para* values."""
    text = doc.getText()
    text.setString("")
    cur = text.createTextCursor()
    text.insertString(cur, "plain ", False)
    cur.setPropertyValue("CharWeight", 150.0)
    text.insertString(cur, "BOLD", False)
    cur.setPropertyValue("CharWeight", 100.0)
    text.insertString(cur, " tail", False)

    para = text.createTextCursor()
    para.gotoStart(False)
    para.gotoEndOfParagraph(True)
    para.setPropertyValue("ParaLeftMargin", 5000)
    para.setPropertyValue("ParaFirstLineIndent", -2000)
    para.setPropertyValue("ParaBackColor", 0xFFE4C4)

    from plugin.writer.html_export import _range_to_content_via_temp_doc

    html = _range_to_content_via_temp_doc(doc, ctx, 0, 16, None)
    assert "BOLD" in html, html
    compact = html.lower().replace(" ", "")
    has_emphasis = (
        "<strong>" in compact
        or "<b>" in compact
        or "font-weight:bold" in compact
        or "font-weight:700" in compact
    )
    assert has_emphasis, html


@native_test
@with_native_doc("writer")
def test_copy_xtext_keeps_nested_text_table_uno(ctx: Any, doc: Any) -> None:
    """_copy_cell_xtext preserves nested in-cell TextTables in the destination."""
    text = doc.getText()
    outer = doc.createInstance("com.sun.star.text.TextTable")
    outer.initialize(2, 2)
    text.insertTextContent(text.getEnd(), outer, False)
    host = outer.getCellByName("B2")
    host.setString("EXPORT_CAPTION")
    nested = doc.createInstance("com.sun.star.text.TextTable")
    nested.initialize(1, 2)
    host.insertTextContent(host.getEnd(), nested, False)
    nested.setName("ExportNested")
    nested.getCellByName("A1").setString("NEST_A")
    nested.getCellByName("B1").setString("NEST_B")

    from plugin.framework.scratch_writer import new_blank_writer
    from plugin.writer.range_copy import copy_xtext_into_doc
    from plugin.writer.specialized.tables import TableGetCells, TableList

    temp_doc = None
    try:
        temp_doc = new_blank_writer(ctx)
        copy_xtext_into_doc(doc, doc.getText(), temp_doc)
        tool_ctx = TestingFactory.create_context(doc=temp_doc, ctx=ctx, env="native")
        listed = TableList().execute(tool_ctx)
        assert listed.get("status") == "ok", listed
        assert listed["count"] >= 2, listed
        hosted = {}
        for entry in listed["tables"]:
            hosted.update(entry.get("nested_in_cells") or {})
        assert hosted, listed
        nested_name = next(iter(hosted.values()))[0]
        cells = TableGetCells().execute(tool_ctx, name=nested_name)
        assert cells["cells"]["A1"] == "NEST_A"
        assert cells["cells"]["B1"] == "NEST_B"
        parent_name = next(
            t["name"] for t in listed["tables"] if t.get("nested_in_cells")
        )
        parent_cells = TableGetCells().execute(tool_ctx, name=parent_name)
        assert "EXPORT_CAPTION" in (parent_cells["cells"].get("B2") or "")
    finally:
        if temp_doc is not None:
            try:
                temp_doc.close(True)
            except Exception:
                pass


@native_test
@with_native_doc("writer")
def test_copy_table_keeps_merged_banner_d2_uno(ctx: Any, doc: Any) -> None:
    """HTML copy used range(cols); after A1:D1 merge that dropped D2."""
    text = doc.getText()
    table = doc.createInstance("com.sun.star.text.TextTable")
    table.initialize(5, 4)
    text.insertTextContent(text.getEnd(), table, False)
    table.setName("ExportMerged")
    table.getCellByName("A1").setString("Title")
    cursor = table.createCursorByCellName("A1")
    cursor.gotoCellByName("D1", True)
    cursor.mergeRange()
    table.getCellByName("D2").setString("Telèfon responsable")

    from plugin.framework.scratch_writer import new_blank_writer
    from plugin.writer.range_copy import copy_xtext_into_doc
    from plugin.writer.specialized.tables import TableGetCells, TableList

    temp_doc = None
    try:
        temp_doc = new_blank_writer(ctx)
        copy_xtext_into_doc(doc, doc.getText(), temp_doc)
        tool_ctx = TestingFactory.create_context(doc=temp_doc, ctx=ctx, env="native")
        listed = TableList().execute(tool_ctx)
        assert listed.get("status") == "ok", listed
        assert listed["count"] >= 1, listed
        dest_name = listed["tables"][0]["name"]
        cells = TableGetCells().execute(tool_ctx, name=dest_name)
        assert cells.get("status") == "ok", cells
        assert cells["cells"].get("D2") == "Telèfon responsable", cells
    finally:
        if temp_doc is not None:
            try:
                temp_doc.close(True)
            except Exception:
                pass


def _outline_paragraph(text, chunk, url):
    """Insert *chunk* at the end of *text* and set its outline hyperlink."""
    cursor = text.createTextCursor()
    cursor.gotoEnd(False)
    text.insertString(cursor, chunk, False)
    sel = text.createTextCursorByRange(cursor.getEnd())
    if not sel.goLeft(len(chunk), True):
        raise AssertionError("could not select inserted run %r" % chunk)
    sel.setPropertyValue("HyperLinkURL", url)


@native_test
@with_native_doc("writer")
def test_selection_export_returns_the_outline_entry_uno(ctx: Any, doc: Any) -> None:
    """A selected outline TOC line must export that line, including its href.

    Discussion #819: scope=selection hung and then returned an unrelated body
    paragraph, with no href. The view cursor is checked first so a bad
    controller selection is not blamed on the exporter. The outline line is
    the second paragraph, so an offset walk that lands early would export the
    body sentence instead.
    """
    from plugin.framework.tool import ToolContext
    from plugin.app import get_services
    from plugin.writer.get_document_content import GetDocumentContent
    from plugin.writer.html_export import _range_to_content_via_temp_doc

    outline = "#2.3.4.TOC_ENTRY_TITLE.|outline"
    body = "BODY_SENTENCE_UNIQUE"
    toc = "2.3.4. TOC_ENTRY_TITLE\t42"
    text = doc.getText()
    text.setString("")
    text.insertString(text.getEnd(), body, False)
    text.insertControlCharacter(text.getEnd(), 0, False)
    _outline_paragraph(text, toc, outline)

    enum = text.createEnumeration()
    enum.nextElement()
    second = enum.nextElement()
    vc = doc.getCurrentController().getViewCursor()
    vc.gotoRange(second.getStart(), False)
    vc.gotoRange(second.getEnd(), True)
    selected = vc.getString() or ""
    assert "TOC_ENTRY_TITLE" in selected, selected
    assert body not in selected, selected

    tool_ctx = ToolContext(doc, ctx, "writer", get_services(), "test")
    result = GetDocumentContent().execute(tool_ctx, scope="selection", max_chars=5000)
    assert result.get("status") == "ok", result
    content = result.get("content") or ""
    assert "TOC_ENTRY_TITLE" in content, content
    assert body not in content, content
    assert "|outline" in content or "%7Coutline" in content, content

    # scope=range still uses offsets. The second paragraph starts after the
    # body text plus the paragraph break.
    start = len(body) + 1
    ranged = _range_to_content_via_temp_doc(doc, ctx, start, start + len(toc), None)
    assert "TOC_ENTRY_TITLE" in ranged, ranged
    assert body not in ranged, ranged
    assert "|outline" in ranged or "%7Coutline" in ranged, ranged


def _hrefs(html: str) -> list[str]:
    import re
    return re.findall(r'href="([^"]*)"', html)


@native_test
@with_native_doc("writer")
def test_split_outline_run_does_not_shift_the_next_href_uno(ctx: Any, doc: Any) -> None:
    """A bold split is two portions and one anchor; the next link keeps its own URL."""
    from plugin.framework.tool import ToolContext
    from plugin.app import get_services
    from plugin.writer.get_document_content import GetDocumentContent

    left = "#1.Left.|outline"
    right = "#2.Right.|outline"
    text = doc.getText()
    text.setString("")
    cursor = text.createTextCursor()
    text.insertString(cursor, "LeftBOLD", False)
    whole = text.createTextCursorByRange(cursor.getEnd())
    assert whole.goLeft(len("LeftBOLD"), True)
    whole.setPropertyValue("HyperLinkURL", left)
    bold = text.createTextCursor()
    bold.gotoStart(False)
    assert bold.goRight(4, False)
    assert bold.goRight(4, True)
    bold.setPropertyValue("CharWeight", 150.0)
    text.insertString(cursor, "Right", False)
    right_sel = text.createTextCursorByRange(cursor.getEnd())
    assert right_sel.goLeft(len("Right"), True)
    right_sel.setPropertyValue("HyperLinkURL", right)

    para = text.createTextCursor()
    para.gotoStart(False)
    para.gotoEndOfParagraph(True)
    vc = doc.getCurrentController().getViewCursor()
    vc.gotoRange(para.getStart(), False)
    vc.gotoRange(para.getEnd(), True)
    tool_ctx = ToolContext(doc, ctx, "writer", get_services(), "test")
    result = GetDocumentContent().execute(tool_ctx, scope="selection", max_chars=5000)
    assert result.get("status") == "ok", result
    content = result.get("content") or ""
    hrefs = _hrefs(content)
    assert left in hrefs, content
    assert right in hrefs, content
    assert hrefs.index(left) < hrefs.index(right), hrefs


@native_test
@with_native_doc("writer")
def test_content_index_selection_exports_the_entry_uno(ctx: Any, doc: Any) -> None:
    """A selection inside a real content index exports that entry, not the body sentence."""
    from plugin.framework.tool import ToolContext
    from plugin.app import get_services
    from plugin.writer.get_document_content import GetDocumentContent
    from plugin.writer.specialized.indexes import IndexesCreate

    text = doc.getText()
    text.setString("")
    text.insertString(text.getEnd(), "TOC_ENTRY_TITLE", False)
    heading = text.createTextCursor()
    heading.gotoStart(False)
    heading.gotoEndOfParagraph(True)
    heading.setPropertyValue("ParaStyleName", "Heading 1")
    text.insertControlCharacter(text.getEnd(), 0, False)
    text.insertString(text.getEnd(), "BODY_SENTENCE_UNIQUE", False)
    tctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    created = IndexesCreate().execute(tctx, kind="toc", title="Contents", target="beginning", create_from_outline=True)
    assert created.get("status") == "ok", created

    entry = None
    enum = text.createEnumeration()
    while enum_has_more(enum):
        para = enum.nextElement()
        try:
            chunk = para.getString() or ""
        except Exception:
            continue
        if "TOC_ENTRY_TITLE" in chunk and "\t" in chunk:
            entry = para
            break
    assert entry is not None, text.getString()
    vc = doc.getCurrentController().getViewCursor()
    vc.gotoRange(entry.getStart(), False)
    vc.gotoRange(entry.getEnd(), True)
    selected = vc.getString() or ""
    assert "TOC_ENTRY_TITLE" in selected, selected
    assert "BODY_SENTENCE_UNIQUE" not in selected, selected
    page = selected.split("\t", 1)[1].strip()
    assert page, selected

    tool_ctx = ToolContext(doc, ctx, "writer", get_services(), "test")
    result = GetDocumentContent().execute(tool_ctx, scope="selection", max_chars=5000)
    assert result.get("status") == "ok", result
    content = result.get("content") or ""
    assert "TOC_ENTRY_TITLE" in content, content
    assert page in content, content
    assert "BODY_SENTENCE_UNIQUE" not in content, content


def _seed_ruby_paragraph(doc: Any) -> None:
    """漢字 + ruby かんじ + trailing です, via cursor properties (not a text field)."""
    text = doc.getText()
    text.setString("")
    cur = text.createTextCursor()
    text.insertString(cur, "漢字です", False)
    cur.gotoStart(False)
    cur.goRight(2, True)
    cur.setPropertyValue("RubyText", "かんじ")
    cur.setPropertyValue("RubyIsAbove", True)


def _assert_semantic_ruby(html: str) -> None:
    """Base and reading stay distinct; reading is in <rt>, not glued body text."""
    assert "漢字かんじです" not in html, html
    assert "<ruby>" in html, html
    assert "<rt>かんじ</rt>" in html, html
    assert "漢字" in html, html
    assert "です" in html, html


@native_test
@with_native_doc("writer")
def test_full_get_document_content_emits_semantic_ruby_uno(ctx: Any, doc: Any) -> None:
    """Full XHTML export emits semantic <ruby>/<rt> markup instead of concatenating base and reading."""
    from plugin.writer.get_document_content import GetDocumentContent

    _seed_ruby_paragraph(doc)
    assert doc.getText().getString() == "漢字です"

    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    result = GetDocumentContent().execute(tool_ctx, scope="full")
    assert result.get("status") == "ok", result
    _assert_semantic_ruby(result.get("content") or "")


@native_test
@with_native_doc("writer")
def test_range_get_document_content_preserves_ruby_reading_uno(ctx: Any, doc: Any) -> None:
    """Range copy preserves RubyText so the reading appears in <rt>."""
    from plugin.writer.get_document_content import GetDocumentContent

    _seed_ruby_paragraph(doc)
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    full = GetDocumentContent().execute(tool_ctx, scope="full")
    assert full.get("status") == "ok", full
    doc_len = int(full.get("document_length") or 0)
    assert doc_len >= 4, full
    result = GetDocumentContent().execute(
        tool_ctx, scope="range", start=0, end=doc_len,
    )
    assert result.get("status") == "ok", result
    _assert_semantic_ruby(result.get("content") or "")


@native_test
@with_native_doc("writer")
def test_selection_get_document_content_preserves_ruby_reading_uno(ctx: Any, doc: Any) -> None:
    from plugin.framework.tool import ToolContext
    from plugin.app import get_services
    from plugin.writer.get_document_content import GetDocumentContent

    _seed_ruby_paragraph(doc)
    text = doc.getText()
    vc = doc.getCurrentController().getViewCursor()
    vc.gotoRange(text.getStart(), False)
    vc.gotoRange(text.getEnd(), True)
    tool_ctx = ToolContext(doc, ctx, "writer", get_services(), "test")
    result = GetDocumentContent().execute(tool_ctx, scope="selection")
    assert result.get("status") == "ok", result
    _assert_semantic_ruby(result.get("content") or "")


@native_test
@with_native_doc("writer")
def test_search_sees_ruby_base_not_reading_uno(ctx: Any, doc: Any) -> None:
    """Paragraph getString() / LO find are base-only; reading is not searchable text.

    Unchanged in Phase 1: ruby is a portion mark, not a TextField, so
    search still matches 漢字 and not かんじ.
    """
    _seed_ruby_paragraph(doc)
    text = doc.getText()
    assert text.getString() == "漢字です"
    from plugin.writer.search import find_first_range

    found_base = find_first_range(doc, "漢字")
    assert found_base is not None
    assert "漢字" in found_base.getString()
    assert "かんじ" not in found_base.getString()
    assert find_first_range(doc, "かんじ") is None

@native_test
@with_native_doc("writer")
def test_export_table_cell_selection_uno(ctx: Any, doc: Any) -> None:
    """A selection inside a table cell must export correctly instead of returning empty."""
    from plugin.framework.tool import ToolContext
    from plugin.app import get_services
    from plugin.writer.get_document_content import GetDocumentContent

    text = doc.getText()
    table = doc.createInstance("com.sun.star.text.TextTable")
    table.initialize(2, 2)
    text.insertTextContent(text.getEnd(), table, False)

    cell = table.getCellByName("A1")
    cell.setString("Hello Table Selection")

    cursor = cell.createTextCursor()
    cursor.gotoStart(False)
    cursor.goRight(6, False)
    cursor.goRight(5, True) # select "Table"

    doc.getCurrentController().select(cursor)

    tool_ctx = ToolContext(doc, ctx, "writer", get_services(), "test")
    result = GetDocumentContent().execute(tool_ctx, scope="selection", max_chars=5000)

    assert result.get("status") == "ok", result
    content = result.get("content") or ""

    assert "Table" in content, content
    assert "Hello" not in content and "Selection" not in content, content


@native_test
@with_native_doc("writer")
def test_selection_and_range_export_includes_math_tex_uno(ctx: Any, doc: Any) -> None:
    """Selection and range export must preserve inline ($) and display ($$) formulas."""
    from plugin.framework.tool import ToolContext
    from plugin.app import get_services
    from plugin.writer.get_document_content import GetDocumentContent
    from plugin.writer.math.math_mml_convert import insert_writer_math_formula

    text = doc.getText()
    text.setString("")
    cur = text.createTextCursor()
    text.insertString(cur, "Start ", False)
    insert_writer_math_formula(doc, cur, "a + b", display_block=False)
    text.insertString(cur, " middle", False)
    insert_writer_math_formula(doc, cur, "x", display_block=True)
    text.insertString(cur, "End", False)

    tool_ctx = ToolContext(doc, ctx, "writer", get_services(), "test")

    # Scope selection covering all text
    vc = doc.getCurrentController().getViewCursor()
    vc.gotoRange(text.getStart(), False)
    vc.gotoRange(text.getEnd(), True)
    sel_res = GetDocumentContent().execute(tool_ctx, scope="selection")
    assert sel_res.get("status") == "ok", sel_res
    sel_html = sel_res.get("content") or ""
    assert "$$" in sel_html, sel_html
    assert sel_html.count("$$") == 2, sel_html
    idx_inline = sel_html.find("$a")
    idx_display = sel_html.find("$$x$$")
    assert idx_inline != -1 and idx_display != -1, sel_html
    assert idx_inline < idx_display, sel_html

    # Scope range covering the full character count
    full_len = len(text.getString())
    range_res = GetDocumentContent().execute(
        tool_ctx, scope="range", start=0, end=full_len
    )
    assert range_res.get("status") == "ok", range_res
    range_html = range_res.get("content") or ""
    assert "$$" in range_html, range_html
    assert range_html.count("$$") == 2, range_html
    r_idx_inline = range_html.find("$a")
    r_idx_display = range_html.find("$$x$$")
    assert r_idx_inline != -1 and r_idx_display != -1, range_html
    assert r_idx_inline < r_idx_display, range_html


@native_test
@with_native_doc("writer")
def test_header_math_xtext_to_content_uno(ctx: Any, doc: Any) -> None:
    """A formula in a header must export its TeX through xtext_to_content."""
    from plugin.writer.html_export import xtext_to_content
    from plugin.writer.math.math_mml_convert import insert_writer_math_formula

    styles = doc.getStyleFamilies().getByName("PageStyles")
    style_name = "Standard" if styles.hasByName("Standard") else styles.getElementNames()[0]
    style = styles.getByName(style_name)
    style.setPropertyValue("HeaderIsOn", True)
    header_text = style.getPropertyValue("HeaderText")
    cur = header_text.createTextCursor()
    header_text.insertString(cur, "Header math: ", False)
    insert_writer_math_formula(doc, cur, "y = m x + b", display_block=False)

    content = xtext_to_content(header_text, doc, ctx)
    assert content, "Expected non-empty HTML from header xtext_to_content"
    assert "$y = m x + b$" in content, content


@native_test
@with_native_doc("writer")
def test_range_and_header_reads_report_line_height_uno(ctx: Any, doc: Any) -> None:
    """Range and header reads must report hand-set line height in data-lo-para."""
    import re

    from plugin.writer import html_export

    text = doc.getText()
    cur = text.createTextCursor()
    text.insertString(cur, "Alpha spaced line", False)
    text.insertControlCharacter(cur, 0, False)
    text.insertString(cur, "Beta indented", False)
    text.insertControlCharacter(cur, 0, False)
    text.insertString(cur, "Gamma plain", False)

    en = text.createEnumeration()
    paras = []
    while enum_has_more(en):
        paras.append(en.nextElement())

    ls = uno.createUnoStruct("com.sun.star.style.LineSpacing")
    ls.Mode = 0
    ls.Height = 150
    paras[0].setPropertyValue("ParaLineSpacing", ls)
    paras[1].setPropertyValue("ParaLeftMargin", 1000)

    n = len(text.getString())
    range_html = html_export.document_to_content(doc, ctx, scope="range", range_start=0, range_end=n)
    xtext_html = html_export.xtext_to_content(text, doc, ctx)

    for html in (range_html, xtext_html):
        paras_html = re.findall(r"<p\b[^>]*>", html)
        assert len(paras_html) >= 3
        assert 'data-lo-para="line-height:150%"' in paras_html[0]
        assert "data-lo-para=" not in paras_html[2]


@native_test
@with_native_doc("writer")
def test_range_export_custom_para_style_keeps_name_uno(ctx: Any, doc: Any) -> None:
    """A custom paragraph style cloned into scratch keeps its name and avoids direct CSS."""
    para_styles = doc.getStyleFamilies().getByName("ParagraphStyles")
    standard = para_styles.getByName("Standard")
    custom = doc.createInstance("com.sun.star.style.ParagraphStyle")
    para_styles.insertByName("CustomBody", custom)
    custom.setPropertyValue("ParaLeftMargin", 2000)
    custom.setPropertyValue("CharFontName", standard.getPropertyValue("CharFontName"))

    text = doc.getText()
    text.setString("Styled body")
    cursor = text.createTextCursor()
    cursor.gotoStart(False)
    cursor.gotoEndOfParagraph(True)
    cursor.setPropertyValue("ParaStyleName", "CustomBody")

    from plugin.writer import html_export

    html = html_export.document_to_content(
        doc, ctx, scope="range", range_start=0, range_end=len(text.getString())
    )
    assert 'data-lo-style="CustomBody"' in html, html
    assert "Styled body" in html, html
    assert "margin-left" not in html, html
    assert "margin-right" not in html, html
    assert "margin-top" not in html, html
    assert "margin-bottom" not in html, html
    assert "font-family" not in html, html


@native_test
@with_native_doc("writer")
def test_range_export_parent_chain_names_the_child_uno(ctx: Any, doc: Any) -> None:
    """Cloning a child style copies missing parent styles first, preserving inheritance."""
    para_styles = doc.getStyleFamilies().getByName("ParagraphStyles")
    parent_style = doc.createInstance("com.sun.star.style.ParagraphStyle")
    para_styles.insertByName("LetterParent", parent_style)
    parent_style.setPropertyValue("ParaLeftMargin", 2000)
    ls = uno.createUnoStruct("com.sun.star.style.LineSpacing")
    ls.Mode = 0
    ls.Height = 150
    parent_style.setPropertyValue("ParaLineSpacing", ls)

    child_style = doc.createInstance("com.sun.star.style.ParagraphStyle")
    para_styles.insertByName("LetterChild", child_style)
    child_style.setParentStyle("LetterParent")

    from plugin.framework.scratch_writer import new_blank_writer
    from plugin.writer.range_copy import _clone_missing_paragraph_style, release_scratch_style_holds

    temp_doc = new_blank_writer(ctx)
    try:
        dest_family = temp_doc.getStyleFamilies().getByName("ParagraphStyles")
        dest_standard = dest_family.getByName("Standard")
        dest_standard.setPropertyValue("ParaBackColor", 0xAABBCC)

        cloned = _clone_missing_paragraph_style(doc, temp_doc, dest_family, "LetterChild")
        assert cloned is True
        assert dest_family.hasByName("LetterParent")
        assert dest_family.hasByName("LetterChild")

        dest_child = dest_family.getByName("LetterChild")
        assert dest_child.getParentStyle() == "LetterParent"
        assert abs(dest_child.getPropertyValue("ParaLeftMargin") - 2000) <= 1
        assert dest_child.getPropertyState("ParaLeftMargin").value != "DIRECT_VALUE"

        assert dest_standard.getPropertyValue("ParaBackColor") == 0xAABBCC

        dest_parent = dest_family.getByName("LetterParent")
        assert dest_parent.getPropertyValue("ParaLineSpacing").Height == 150
    finally:
        release_scratch_style_holds(temp_doc)
        temp_doc.close(True)

    text = doc.getText()
    text.setString("Child paragraph")
    cursor = text.createTextCursor()
    cursor.gotoStart(False)
    cursor.gotoEndOfParagraph(True)
    cursor.setPropertyValue("ParaStyleName", "LetterChild")

    from plugin.writer import html_export

    html = html_export.document_to_content(
        doc, ctx, scope="range", range_start=0, range_end=len(text.getString())
    )
    assert 'data-lo-style="LetterChild"' in html, html
    assert "Child paragraph" in html, html
    assert "margin-left" not in html, html


@native_test
@with_native_doc("writer")
def test_range_export_missing_follow_style_keeps_name_uno(ctx: Any, doc: Any) -> None:
    """A custom style whose FollowStyle is missing still exports its data-lo-style token."""
    para_styles = doc.getStyleFamilies().getByName("ParagraphStyles")
    style = doc.createInstance("com.sun.star.style.ParagraphStyle")
    para_styles.insertByName("FollowedHead", style)
    style.setPropertyValue("FollowStyle", "NoSuchFollow")

    text = doc.getText()
    text.setString("Followed heading")
    cursor = text.createTextCursor()
    cursor.gotoStart(False)
    cursor.gotoEndOfParagraph(True)
    cursor.setPropertyValue("ParaStyleName", "FollowedHead")

    from plugin.writer import html_export

    html = html_export.document_to_content(
        doc, ctx, scope="range", range_start=0, range_end=len(text.getString())
    )
    assert 'data-lo-style="FollowedHead"' in html, html
    assert "Followed heading" in html, html


@native_test
@with_native_doc("writer")
def test_full_read_keeps_outline_link_targets_uno(ctx: Any, doc: Any) -> None:
    """Full-document XHTML read restores stored |outline hrefs over #a_... anchors."""
    from plugin.app import get_services
    from plugin.framework.tool import ToolContext
    from plugin.writer.apply_document_content import ApplyDocumentContent
    from plugin.writer.get_document_content import GetDocumentContent

    tool_ctx = ToolContext(doc, ctx, "writer", get_services(), "test")
    html = (
        "<h1>Getting Started</h1>"
        "<p>Intro text</p>"
        '<p><a href="#Getting Started|outline">see here</a> '
        '<a href="https://example.com/">ext</a> '
        '<a href="#Intro &amp; More|outline"><b>bo</b>ld</a></p>'
        "<h2>Intro &amp; More</h2>"
        "<table>"
        '<tr><td><a href="#Getting Started|outline">c1</a></td><td>c2</td></tr>'
        '<tr><td>c3</td><td><a href="#Intro &amp; More|outline">c4</a></td></tr>'
        "</table>"
    )
    apply_res = ApplyDocumentContent().execute(tool_ctx, target="full_document", content=html)
    assert apply_res.get("status") == "ok", apply_res

    enum = doc.getText().createEnumeration()
    while enum_has_more(enum):
        para = enum.nextElement()
        if para.supportsService("com.sun.star.text.Paragraph") and para.getString() == "Intro text":
            fn = doc.createInstance("com.sun.star.text.Footnote")
            doc.getText().insertTextContent(para.getEnd(), fn, False)
            fn.setString("Footnote text")
            break

    read_res = GetDocumentContent().execute(tool_ctx, scope="full", max_chars=20000)
    assert read_res.get("status") == "ok", read_res
    content = read_res.get("content") or ""
    hrefs = _hrefs(content)
    assert not any(h.startswith("#a_") for h in hrefs), hrefs
    assert "#ftn0" in hrefs, hrefs
    non_ftn = [h for h in hrefs if "ftn" not in h]
    assert non_ftn == [
        "#Getting Started|outline",
        "https://example.com/",
        "#Intro &amp; More|outline",
        "#Getting Started|outline",
        "#Intro &amp; More|outline",
    ], non_ftn


@native_test
@with_native_doc("writer")
def test_range_and_selection_reads_report_paragraph_font_like_full_read_uno(ctx: Any, doc: Any) -> None:
    """Id 306: range and selection reads report paragraph font/colour in data-lo-para like full."""
    import re
    from com.sun.star.awt.FontSlant import ITALIC
    from com.sun.star.text.ControlCharacter import PARAGRAPH_BREAK

    from plugin.writer import html_export

    text = doc.getText()
    text.setString("")
    cur = text.createTextCursor()
    text.insertString(cur, "Beta has italic run", False)
    text.insertControlCharacter(cur, PARAGRAPH_BREAK, False)
    text.insertString(cur, "Gamma bold with plain word", False)
    text.insertControlCharacter(cur, PARAGRAPH_BREAK, False)
    text.insertString(cur, "Eps plain", False)

    paras = []
    enum = text.createEnumeration()
    while enum_has_more(enum):
        paras.append(enum.nextElement())

    p0 = text.createTextCursorByRange(paras[0].getStart())
    p0.gotoEndOfParagraph(True)
    p0.setPropertyValue("CharFontName", "Liberation Mono")
    r0 = text.createTextCursorByRange(paras[0].getStart())
    r0.goRight(9, False)
    r0.goRight(6, True)
    r0.setPropertyValue("CharPosture", ITALIC)

    p1 = text.createTextCursorByRange(paras[1].getStart())
    p1.gotoEndOfParagraph(True)
    p1.setPropertyValue("CharWeight", 150.0)
    r1 = text.createTextCursorByRange(paras[1].getStart())
    r1.goRight(16, False)
    r1.goRight(5, True)
    r1.setPropertyValue("CharWeight", 100.0)

    total = len(text.getString())
    full = html_export.document_to_content(doc, ctx, scope="full")
    rng = html_export.document_to_content(doc, ctx, scope="range", range_start=0, range_end=total)

    vc = doc.getCurrentController().getViewCursor()
    vc.gotoStart(False)
    vc.gotoEnd(True)
    sel = html_export.document_to_content(doc, ctx, scope="selection")

    partial = html_export.document_to_content(doc, ctx, scope="range", range_start=9, range_end=35)

    def blocks(html: str) -> list[str]:
        return re.findall(r"<p\b.*?</p>", re.sub(r"\s+", " ", html))

    full_blocks = blocks(full)
    rng_blocks = blocks(rng)
    sel_blocks = blocks(sel)
    assert rng_blocks == full_blocks, (rng_blocks, full_blocks)
    assert sel_blocks == full_blocks, (sel_blocks, full_blocks)

    open0, body0 = rng_blocks[0].split(">", 1)
    assert "data-lo-para=" in open0 and "font-family:" in open0, rng_blocks[0]
    assert "font-family" not in body0, rng_blocks[0]

    open1, body1 = rng_blocks[1].split(">", 1)
    assert "data-lo-para=" in open1 and "font-weight:bold" in open1, rng_blocks[1]
    assert "font-weight:normal" in body1, rng_blocks[1]

    partial_blocks = blocks(partial)
    partial_open0 = partial_blocks[0].split(">", 1)[0]
    assert "data-lo-para=" in partial_open0 and "font-family:" in partial_open0, partial_blocks[0]


@native_test
@with_native_doc("writer")
def test_range_read_after_tables_is_not_shifted_uno(ctx: Any, doc: Any) -> None:
    """_offset_window stays aligned with read_visible_body across tables."""
    import re
    from plugin.doc.text_helpers import read_visible_body
    from plugin.writer.html_export import document_to_content

    def _body_structure() -> list[str]:
        return ["TextTable" if style == "TABLE" else f"Paragraph:{text}" for style, text in body_shape(doc.getText())]

    def _reset_and_build(layout: str) -> tuple[list[str], list[str]]:
        while doc.getTextTables().getCount() > 0:
            doc.getTextTables().getByIndex(0).dispose()
        text = doc.getText()
        cur = text.createTextCursor()
        cur.gotoStart(False)
        cur.gotoEnd(True)
        cur.setString("")
        cur.gotoStart(False)

        def _add_table(cell_text: str) -> None:
            tbl = doc.createInstance("com.sun.star.text.TextTable")
            tbl.initialize(1, 1)
            text.insertTextContent(text.getEnd(), tbl, False)
            tbl.getCellByName("A1").setString(cell_text)

        def _fill_trailing_para(s: str) -> None:
            cur.gotoEnd(False)
            text.insertString(cur, s, False)

        if layout == "middle":
            text.insertString(cur, "Alpha", False)
            _add_table("T1")
            _fill_trailing_para("Beta")
            return ["Alpha", "T1", "Beta"], ["Paragraph:Alpha", "TextTable", "Paragraph:Beta"]
        if layout == "leading":
            _add_table("T1")
            _fill_trailing_para("Beta")
            return ["T1", "Beta"], ["TextTable", "Paragraph:Beta"]
        if layout == "adjacent_and_later":
            text.insertString(cur, "Alpha", False)
            _add_table("T1")
            _add_table("T2")
            _fill_trailing_para("Beta")
            _add_table("T3")
            _fill_trailing_para("Gamma")
            return (
                ["Alpha", "T1", "T2", "Beta", "T3", "Gamma"],
                ["Paragraph:Alpha", "TextTable", "TextTable", "Paragraph:Beta", "TextTable", "Paragraph:Gamma"],
            )
        # empty paragraph after table
        text.insertString(cur, "Alpha", False)
        _add_table("T1")
        cur.gotoEnd(False)
        text.insertControlCharacter(cur, 0, False)
        _fill_trailing_para("Gamma")
        return ["Alpha", "T1", "Gamma"], ["Paragraph:Alpha", "TextTable", "Paragraph:", "Paragraph:Gamma"]

    for layout in ("middle", "leading", "adjacent_and_later", "empty_after"):
        words, expected_structure = _reset_and_build(layout)
        assert _body_structure() == expected_structure, (layout, _body_structure())
        full_str, _, _ = read_visible_body(doc)
        for word in words:
            offset = full_str.index(word)
            html = document_to_content(
                doc, ctx, scope="range", range_start=offset, range_end=offset + len(word)
            )
            plain = re.sub(r"<[^>]+>", "", html).strip()
            assert plain == word, (layout, word, offset, html)


@native_test
@with_native_doc("writer")
def test_range_and_selection_reads_include_tables_uno(ctx: Any, doc: Any) -> None:
    import re
    from plugin.writer.html_export import document_to_content

    text = doc.getText()
    cur = text.createTextCursor()
    cur.gotoStart(False)
    cur.setPropertyValue("ParaLeftMargin", 1000)
    cur.setPropertyValue("CharWeight", 150.0)
    text.insertString(cur, "Lead paragraph", False)

    for tbl_name, cell_text, url in (("FirstTbl", "One", ""), ("SecondTbl", "Two", "")):
        tbl = doc.createInstance("com.sun.star.text.TextTable")
        tbl.initialize(1, 1)
        text.insertTextContent(text.getEnd(), tbl, False)
        tbl.setName(tbl_name)
        tbl.getCellByName("A1").setString(cell_text)

    cur.gotoEnd(False)
    text.insertControlCharacter(cur, 0, False)
    cur.gotoEnd(False)

    link_tbl = doc.createInstance("com.sun.star.text.TextTable")
    link_tbl.initialize(1, 1)
    text.insertTextContent(text.getEnd(), link_tbl, False)
    link_tbl.setName("LinkTbl")
    c_cur = link_tbl.getCellByName("A1").createTextCursor()
    link_tbl.getCellByName("A1").insertString(c_cur, "CellLink", False)
    c_cur.goLeft(len("CellLink"), True)
    c_cur.setPropertyValue("HyperLinkURL", "https://cell.example.com/")

    cur.gotoEnd(False)
    text.insertString(cur, "TailLink", False)
    cur.goLeft(len("TailLink"), True)
    cur.setPropertyValue("HyperLinkURL", "https://tail.example.com/")

    full_html = document_to_content(doc, ctx, scope="full")
    full_order = re.findall(r"<(?:p|table)\b", full_html)
    range_html = document_to_content(doc, ctx, scope="range", range_start=0, range_end=len(text.getString()))
    range_order = re.findall(r"<(?:p|table)\b", range_html)
    assert range_order == full_order, (range_order, full_order)
    for name in ("FirstTbl", "SecondTbl", "LinkTbl"):
        assert re.search(rf'class="[^"]*\btable-{name}\b[^"]*"', range_html), (name, range_html)
    assert re.search(
        r'<a\b[^>]*\bhref="https://cell\.example\.com/"[^>]*>(?:<[^>]+>)*CellLink(?:<[^>]+>)*</a>',
        range_html,
    ), range_html
    assert re.search(
        r'<a\b[^>]*\bhref="https://tail\.example\.com/"[^>]*>(?:<[^>]+>)*TailLink(?:<[^>]+>)*</a>',
        range_html,
    ), range_html

    # Selection spanning only the first two tables returns only tables.
    sel_cur = text.createTextCursor()
    sel_cur.gotoStart(False)
    sel_cur.gotoEndOfParagraph(False)
    sel_cur.gotoNextParagraph(True)
    doc.getCurrentController().select(sel_cur)
    sel_tables_html = document_to_content(doc, ctx, scope="selection")
    assert "<table" in sel_tables_html
    outside_tables = re.sub(r"<table\b.*?</table>", "", sel_tables_html, flags=re.DOTALL)
    assert "<p" not in outside_tables and not outside_tables.strip(), sel_tables_html

    # Selection of one paragraph has no table.
    p_cur = text.createTextCursor()
    p_cur.gotoStart(False)
    p_cur.gotoEndOfParagraph(True)
    doc.getCurrentController().select(p_cur)
    sel_para_html = document_to_content(doc, ctx, scope="selection")
    assert "<table" not in sel_para_html
    assert "Lead paragraph" in sel_para_html

    # Offset inside a table returns that table block.
    raw = text.getString()
    idx = raw.index("One")
    tbl_slice = document_to_content(doc, ctx, scope="range", range_start=idx, range_end=idx + 1)
    assert "FirstTbl" in tbl_slice


@native_test
@with_native_doc("writer")
def test_range_read_table_cells_uno(ctx: Any, doc: Any) -> None:
    from plugin.writer.html_export import document_to_content
    from plugin.writer.record_changes import record_changes

    text = doc.getText()
    cur = text.createTextCursor()
    text.insertString(cur, "Before", False)
    tbl = doc.createInstance("com.sun.star.text.TextTable")
    tbl.initialize(2, 2)
    text.insertTextContent(text.getEnd(), tbl, False)

    # Cell A1: nested table followed by text.
    a1 = tbl.getCellByName("A1")
    a1_cur = a1.createTextCursor()
    nested = doc.createInstance("com.sun.star.text.TextTable")
    nested.initialize(1, 1)
    a1.insertTextContent(a1_cur, nested, False)
    nested.getCellByName("A1").setString("Inner")
    a1.insertString(a1_cur, "AfterNested", False)

    # Cell B1: two paragraphs, red run, date field, tracked deletion.
    b1 = tbl.getCellByName("B1")
    b1_cur = b1.createTextCursor()
    b1.insertString(b1_cur, "Plain ", False)
    b1.insertString(b1_cur, "Red", False)
    b1_cur.goLeft(len("Red"), True)
    b1_cur.setPropertyValue("CharColor", 0xFF0000)
    b1_cur.gotoEnd(False)
    b1_cur.setPropertyValue("CharColor", -1)
    date_field = doc.createInstance("com.sun.star.text.textfield.DateTime")
    date_field.setPropertyValue("IsDate", True)
    b1.insertTextContent(b1_cur, date_field, False)
    b1.insertControlCharacter(b1_cur, 0, False)
    b1.insertString(b1_cur, "Keep GONE", False)
    del_cur = b1.createTextCursor()
    del_cur.gotoEnd(False)
    del_cur.goLeft(len("GONE"), True)
    with record_changes(doc, True):
        del_cur.setString("")
    redlines = doc.getRedlines()
    assert any(
        str(redlines.getByIndex(idx).getPropertyValue("RedlineType")) == "Delete"
        for idx in range(redlines.getCount())
    )

    # Row 2: merged then split cell.
    row_cur = tbl.createCursorByCellName("A2")
    row_cur.gotoCellByName("B2", True)
    row_cur.mergeRange()
    row_cur.splitRange(2, False)
    tbl.getCellByName("A2").setString("MergedCell")
    tbl.getCellByName("B2").setString("SplitRight")

    cur.gotoEnd(False)
    text.insertString(cur, "After", False)

    import re

    html = document_to_content(doc, ctx, scope="range", range_start=0, range_end=len(text.getString()))
    assert "Inner" in html
    assert "AfterNested" in html
    assert re.search(
        r'color\s*:\s*(?:#ff0000|rgb\(\s*255\s*,\s*0\s*,\s*0\s*\))[^>]*>Red<',
        html,
        re.IGNORECASE,
    ), html
    assert 'title="date"' in html
    td_b1 = re.search(r"<td\b[^>]*>((?:(?!</?td\b).)*?Red.*?)</td>", html, re.DOTALL)
    assert td_b1 is not None and len(re.findall(r"<p\b", td_b1.group(1))) == 2, html
    assert "Keep" in html
    assert "GONE" not in html
    assert "MergedCell" in html
    assert "SplitRight" in html


@native_test
@with_native_doc("writer")
def test_para_adjust_start_round_trip_ltr_and_rtl_uno(ctx: Any, doc: Any) -> None:
    import unittest
    from com.sun.star.text.WritingMode2 import LR_TB, RL_TB
    from plugin.writer.html_export import document_to_content
    from plugin.writer.html_import import replace_full_document

    text = doc.getText()
    cur = text.createTextCursor()
    try:
        cur.setPropertyValue("ParaAdjust", 5)
    except Exception:
        raise unittest.SkipTest("ParagraphAdjust.START unavailable")
    if int(cur.getPropertyValue("ParaAdjust")) != 5:
        raise unittest.SkipTest("ParagraphAdjust.START unavailable")
    cur.setPropertyValue("WritingMode", LR_TB)
    text.insertString(cur, "LTR start", False)
    text.insertControlCharacter(cur, 0, False)
    cur.setPropertyValue("ParaAdjust", 5)
    cur.setPropertyValue("WritingMode", RL_TB)
    text.insertString(cur, "RTL start", False)

    html = document_to_content(doc, ctx, scope="full")
    assert "text-align:start" in html, html

    replace_full_document(doc, ctx, html)

    enum = doc.getText().createEnumeration()
    adjusts = []
    while enum_has_more(enum):
        el = enum.nextElement()
        adjusts.append(int(el.getPropertyValue("ParaAdjust")))
    assert adjusts == [5, 5], adjusts


@native_test
@with_native_doc("writer")
def test_selection_starting_in_table_exports_whole_tables_uno(ctx: Any, doc: Any) -> None:
    from plugin.doc.text_helpers import live_selection_range, selection_cursor_snapshot
    from plugin.framework.errors import uno_read
    from plugin.framework.uno_context import execute_dispatch, uno_same
    from plugin.writer.html_export import document_to_content

    def _reset_and_add_table(cell_text: str) -> Any:
        tbl = doc.createInstance("com.sun.star.text.TextTable")
        tbl.initialize(1, 1)
        text.insertTextContent(text.getEnd(), tbl, False)
        tbl.getCellByName("A1").setString(cell_text)
        return tbl

    text = doc.getText()
    controller = doc.getCurrentController()

    def _ctrl_a_whole_doc(cell: Any, presses: int = 3) -> None:
        controller.select(cell.getStart())
        # In a non-empty cell, Ctrl+A expands: cell -> table -> (outer cell -> outer table ->) document.
        for _ in range(presses):
            execute_dispatch(ctx, controller, ".uno:SelectAll")
        sel = live_selection_range(controller)
        assert sel is not None and not uno_same(sel.getText(), text)

    # [T, tail], [T, T, tail], [T, mid, T, tail], and nested leading table via Ctrl+A and snapshot.
    cases = [
        (["T0"], "tail", ["T0", "tail"]),
        (["T0", "T1"], "tail", ["T0", "T1", "tail"]),
    ]
    for table_cells, tail, expected in cases:
        text.setString("")
        tables = [_reset_and_add_table(c) for c in table_cells]
        cur = text.createTextCursor()
        cur.gotoEnd(False)
        text.insertString(cur, tail, False)

        _ctrl_a_whole_doc(tables[0].getCellByName("A1"))
        html = document_to_content(doc, ctx, scope="selection")
        for token in expected:
            assert token in html, (expected, html)
        snap = selection_cursor_snapshot(doc)
        html_snap = document_to_content(doc, ctx, scope="selection", selection=snap)
        for token in expected:
            assert token in html_snap, (expected, html_snap)

    # [T, mid, T, tail]
    text.setString("")
    t0 = _reset_and_add_table("T0")
    cur = text.createTextCursor()
    cur.gotoEnd(False)
    text.insertString(cur, "mid", False)
    _reset_and_add_table("T1")
    cur.gotoEnd(False)
    text.insertString(cur, "tail", False)
    _ctrl_a_whole_doc(t0.getCellByName("A1"))
    html = document_to_content(doc, ctx, scope="selection")
    for token in ("T0", "mid", "T1", "tail"):
        assert token in html, html

    # Nested leading table
    text.setString("")
    outer = _reset_and_add_table("OUTER")
    nested = doc.createInstance("com.sun.star.text.TextTable")
    nested.initialize(1, 1)
    cell_a1 = outer.getCellByName("A1")
    cell_a1.insertTextContent(cell_a1.getEnd(), nested, False)
    nested.getCellByName("A1").setString("INNER")
    cur = text.createTextCursor()
    cur.gotoEnd(False)
    text.insertString(cur, "tail", False)
    _ctrl_a_whole_doc(nested.getCellByName("A1"), presses=5)
    html = document_to_content(doc, ctx, scope="selection")
    for token in ("OUTER", "INNER", "tail"):
        assert token in html, html

    # ['BEFORE_PARA', T, 'tail'] with a body-text selection starting in T and ending in 'tail'
    # exercises the SwXBodyText form of _table_start_range (SwCursor::IsSelOvr forbids
    # Shift+Down from leaving a table, so pass the range directly).
    from types import SimpleNamespace

    text.setString("BEFORE_PARA")
    tbl = _reset_and_add_table("T0")
    cur = text.createTextCursor()
    cur.gotoEnd(False)
    text.insertString(cur, "tail", False)
    body_sel = SimpleNamespace(
        getText=lambda: text,
        getStart=lambda: tbl.getCellByName("A1").getStart(),
        getEnd=lambda: text.getEnd(),
    )
    assert uno_read(lambda: text.createTextCursorByRange(body_sel.getStart()), None) is None
    html = document_to_content(doc, ctx, scope="selection", selection=body_sel)
    assert "T0" in html and "tail" in html and "BEFORE_PARA" not in html, html

    # In-cell selection unchanged.
    cell_cur = tbl.getCellByName("A1").createTextCursor()
    cell_cur.gotoStart(False)
    cell_cur.gotoEnd(True)
    controller.select(cell_cur)
    html_cell = document_to_content(doc, ctx, scope="selection")
    assert "T0" in html_cell and "tail" not in html_cell, html_cell


@native_test
@with_native_doc("writer")
def test_html_export_hides_multi_para_deletion_in_range_selection_table_links_ruby_uno(ctx: Any, doc: Any) -> None:
    from plugin.doc.text_helpers import get_string_without_tracked_deletions
    from plugin.tests.testing_utils import strike_across_paragraphs
    from plugin.writer.html_export import document_to_content

    text = doc.getText()
    strike_across_paragraphs(doc, text)

    # 1. Range read across all paragraphs: no gone.
    full_len = len(get_string_without_tracked_deletions(text))
    range_html = document_to_content(doc, ctx, scope="range", range_start=0, range_end=full_len)
    assert "gone" not in range_html, range_html
    assert "Keep" in range_html and "Tail" in range_html and "After" in range_html

    # 2. Range from Tail's visible offset returns Tail.
    visible_str = get_string_without_tracked_deletions(text)
    tail_off = visible_str.index("Tail")
    tail_html = document_to_content(
        doc, ctx, scope="range", range_start=tail_off, range_end=tail_off + len("Tail")
    )
    assert "Tail" in tail_html and "gone" not in tail_html, tail_html

    # 3. Selection from gone2 paragraph to the end: no gone.
    enum = text.createEnumeration()
    paras = []
    while enum_has_more(enum):
        paras.append(enum.nextElement())
    sel_cur = text.createTextCursorByRange(paras[2].getStart())
    sel_cur.gotoRange(text.getEnd(), True)
    doc.getCurrentController().select(sel_cur)
    sel_html = document_to_content(doc, ctx, scope="selection")
    assert "gone" not in sel_html, sel_html
    assert "Tail" in sel_html and "After" in sel_html


@native_test
@with_native_doc("writer")
def test_range_and_selection_export_keep_pictures_and_names_uno(ctx: Any, doc: Any) -> None:
    import base64
    import os
    import re
    import tempfile
    from com.sun.star.text.TextContentAnchorType import AS_CHARACTER, AT_CHARACTER, AT_PARAGRAPH
    from plugin.framework.uno_context import execute_dispatch
    from plugin.tests.testing_utils import native_writer_tool_context
    from plugin.writer.apply_document_content import ApplyDocumentContent
    from plugin.writer.html_export import document_to_content

    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )
    fd, img_path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    with open(img_path, "wb") as f:
        f.write(png)

    def _add_pic(target_text: Any, anchor_type: Any, name: str, at_end: bool = True) -> None:
        g = doc.createInstance("com.sun.star.text.TextGraphicObject")
        g.setPropertyValue("GraphicURL", uno.systemPathToFileUrl(img_path))
        g.setPropertyValue("AnchorType", anchor_type)
        g.setPropertyValue("Width", 400)
        g.setPropertyValue("Height", 400)
        cur = target_text.createTextCursor()
        if at_end:
            cur.gotoEnd(False)
        else:
            cur.gotoStart(False)
        target_text.insertTextContent(cur, g, False)
        g.setName(name)

    def _img_ids(html: str) -> list[str]:
        return re.findall(r'<(?:div|span)\b[^>]*\bid="([^"]+)"[^>]*>\s*(?:<p\b[^>]*>\s*)?<img\b', html)

    try:
        text = doc.getText()
        controller = doc.getCurrentController()

        # 1. Earlier picture shifts automatic names; range & selection keep real names across 3 anchors.
        for anchor_type in (AS_CHARACTER, AT_CHARACTER, AT_PARAGRAPH):
            tag = anchor_type.value
            text.setString("intro")
            _add_pic(text, AT_PARAGRAPH, f"EarlierPic_{tag}")
            cur = text.createTextCursor()
            cur.gotoEnd(False)
            text.insertControlCharacter(cur, 0, False)
            cur.gotoEnd(False)
            text.insertString(cur, "one ", False)
            _add_pic(text, anchor_type, f"PicA_{tag}")
            cur.gotoEnd(False)
            text.insertString(cur, " two ", False)
            _add_pic(text, anchor_type, f"PicB_{tag}")
            cur.gotoEnd(False)
            text.insertString(cur, " three", False)

            full_html = document_to_content(doc, ctx, scope="full", include_images=False)
            full_ids = _img_ids(full_html)
            assert f"PicA_{tag}" in full_ids and f"PicB_{tag}" in full_ids, full_html

            # Range read covering the second paragraph.
            full_str = text.getString()
            p2_start = full_str.index("one")
            range_html = document_to_content(
                doc, ctx, scope="range", range_start=p2_start, range_end=len(full_str), include_images=False
            )
            assert _img_ids(range_html) == [f"PicA_{tag}", f"PicB_{tag}"], range_html

            # Selection read over the second paragraph.
            sel_cur = text.createTextCursor()
            sel_cur.gotoEnd(False)
            sel_cur.gotoStartOfParagraph(True)
            controller.select(sel_cur)
            sel_html = document_to_content(doc, ctx, scope="selection", include_images=False)
            assert _img_ids(sel_html) == [f"PicA_{tag}", f"PicB_{tag}"], sel_html

        # 2. Selection write-back keeps both pictures, off and recorded + Accept.
        text.setString("")
        text.insertString(text.getStart(), "one ", False)
        _add_pic(text, AS_CHARACTER, "SelA")
        cur = text.createTextCursor()
        cur.gotoEnd(False)
        text.insertString(cur, " two ", False)
        _add_pic(text, AS_CHARACTER, "SelB")
        cur.gotoEnd(False)
        text.insertString(cur, " three", False)

        sel_cur = text.createTextCursor()
        sel_cur.gotoStart(False)
        sel_cur.gotoEnd(True)
        controller.select(sel_cur)
        sel_html = document_to_content(doc, ctx, scope="selection", include_images=False)
        tctx = native_writer_tool_context(doc, ctx)
        res = ApplyDocumentContent().execute(
            tctx, target="selection", content=sel_html.replace("three", "THREE")
        )
        assert res.get("status") == "ok", res
        assert doc.getGraphicObjects().getCount() == 2
        after1 = document_to_content(doc, ctx, scope="full", include_images=False)
        assert re.search(r"one.*SelA.*two.*SelB.*THREE", after1, re.DOTALL), after1

        doc.setPropertyValue("RecordChanges", True)
        try:
            sel_cur = text.createTextCursor()
            sel_cur.gotoStart(False)
            sel_cur.gotoEnd(True)
            controller.select(sel_cur)
            sel_html2 = document_to_content(doc, ctx, scope="selection", include_images=False)
            res2 = ApplyDocumentContent().execute(
                tctx, target="selection", content=sel_html2.replace("one", "ONE")
            )
            assert res2.get("status") == "ok", res2
            execute_dispatch(ctx, controller, ".uno:AcceptAllTrackedChanges")
            assert doc.getGraphicObjects().getCount() == 2
            after2 = document_to_content(doc, ctx, scope="full", include_images=False)
            assert re.search(r"ONE.*<img\b.*two.*<img\b.*THREE", after2, re.DOTALL), after2
        finally:
            doc.setPropertyValue("RecordChanges", False)

        # 3. Image-only first paragraph is included at range_start=0.
        text.setString("")
        _add_pic(text, AT_PARAGRAPH, "FirstParaPic", at_end=False)
        cur = text.createTextCursor()
        cur.gotoEnd(False)
        text.insertControlCharacter(cur, 0, False)
        text.insertString(cur, "after", False)
        r0 = document_to_content(doc, ctx, scope="range", range_start=0, range_end=3, include_images=False)
        assert "FirstParaPic" in _img_ids(r0), r0

        # 4. Cell picture in range and selection reads (AS_CHARACTER and AT_PARAGRAPH).
        text.setString("")
        tbl = doc.createInstance("com.sun.star.text.TextTable")
        tbl.initialize(1, 1)
        text.insertTextContent(text.getStart(), tbl, False)
        cell = tbl.getCellByName("A1")
        cell.setString("cell")
        _add_pic(cell, AT_PARAGRAPH, "CellPicPara")
        _add_pic(cell, AS_CHARACTER, "CellPicInline")
        r_cell = document_to_content(doc, ctx, scope="range", range_start=0, range_end=10, include_images=False)
        assert {"CellPicPara", "CellPicInline"}.issubset(_img_ids(r_cell)), r_cell
        cell_cur = cell.createTextCursor()
        cell_cur.gotoStart(False)
        cell_cur.gotoEnd(True)
        controller.select(cell_cur)
        s_cell = document_to_content(doc, ctx, scope="selection", include_images=False)
        assert {"CellPicPara", "CellPicInline"}.issubset(_img_ids(s_cell)), s_cell

        # 5. Formula paragraph with a picture in range and selection reads (_copy_formula_runs).
        text.setString("")
        math_obj = doc.createInstance("com.sun.star.text.TextEmbeddedObject")
        math_obj.CLSID = "078B7ABA-54FC-457F-8551-6147e776a997"
        text.insertTextContent(text.getStart(), math_obj, False)
        math_obj.getEmbeddedObject().Formula = "x^2"
        _add_pic(text, AS_CHARACTER, "FormulaPic")
        r_math = document_to_content(doc, ctx, scope="range", range_start=0, range_end=5, include_images=False)
        assert "FormulaPic" in _img_ids(r_math), r_math
        sel_math = text.createTextCursor()
        sel_math.gotoStart(False)
        sel_math.gotoEnd(True)
        controller.select(sel_math)
        s_math = document_to_content(doc, ctx, scope="selection", include_images=False)
        assert "FormulaPic" in _img_ids(s_math), s_math
    finally:
        os.unlink(img_path)


@native_test
@with_native_doc("writer")
def test_html_export_range_over_struck_table_uno(ctx: Any, doc: Any) -> None:
    from plugin.doc.text_helpers import get_string_without_tracked_deletions
    from plugin.tests.testing_utils import strike_across_table
    from plugin.writer.html_export import document_to_content

    text = doc.getText()
    strike_across_table(doc, text)

    vis_len = len(get_string_without_tracked_deletions(text))
    html_tbl = document_to_content(doc, ctx, scope="range", range_start=0, range_end=vis_len)
    assert "<table" not in html_tbl.lower(), html_tbl
    assert "CellGone" not in html_tbl, html_tbl


@native_test
@with_native_doc("writer")
def test_html_export_full_read_skips_mid_deletion_outline_link_uno(ctx: Any, doc: Any) -> None:
    import re
    from plugin.writer.html_export import document_to_content

    text = doc.getText()
    c = text.createTextCursor()
    text.insertString(c, "KeepHeading", False)
    c.setPropertyValue("ParaStyleName", "Heading 1")
    text.insertControlCharacter(c, 0, False)
    c.setPropertyValue("ParaStyleName", "Standard")
    text.insertString(c, "StartGone", False)
    text.insertControlCharacter(c, 0, False)
    text.insertString(c, "StruckLink", False)
    c.gotoStartOfParagraph(True)
    c.setPropertyValue("HyperLinkURL", "#KeepHeading|outline")
    c.gotoEndOfParagraph(False)
    text.insertControlCharacter(c, 0, False)
    text.insertString(c, "EndGone ", False)
    text.insertString(c, "LiveLink", False)
    c.goLeft(len("LiveLink"), True)
    c.setPropertyValue("HyperLinkURL", "#KeepHeading|outline")

    # Strike from StartGone (plain text) through EndGone; StruckLink sits in the middle para.
    enum = text.createEnumeration()
    paras = []
    while enum_has_more(enum):
        paras.append(enum.nextElement())
    d = text.createTextCursorByRange(paras[1].getStart())
    e = text.createTextCursorByRange(paras[3].getStart())
    e.goRight(len("EndGone "), False)
    d.gotoRange(e, True)
    doc.setPropertyValue("RecordChanges", True)
    try:
        d.setString("")
    finally:
        doc.setPropertyValue("RecordChanges", False)

    assert doc.getRedlines().getCount() == 1
    mid_enum = paras[2].createEnumeration()
    while enum_has_more(mid_enum):
        assert mid_enum.nextElement().getPropertyValue("TextPortionType") != "Redline"

    full_html = document_to_content(doc, ctx, scope="full")
    m = re.search(r'<a\b[^>]*\bhref="([^"]*)"[^>]*>LiveLink</a>', full_html)
    assert m is not None, full_html
    assert m.group(1) in ("#KeepHeading|outline", "#KeepHeading%7Coutline"), m.group(1)


@native_test
@with_native_doc("writer")
def test_html_export_full_read_skips_mid_deletion_ruby_uno(ctx: Any, doc: Any) -> None:
    from plugin.writer.html_export import document_to_content

    text = doc.getText()
    rc = text.createTextCursor()
    text.insertString(rc, "start", False)
    text.insertControlCharacter(rc, 0, False)
    text.insertString(rc, "漢", False)
    rc.gotoStartOfParagraph(True)
    rc.setPropertyValue("RubyText", "かん")
    rc.gotoEndOfParagraph(False)
    text.insertControlCharacter(rc, 0, False)
    text.insertString(rc, "end 漢", False)

    enum = text.createEnumeration()
    paras = []
    while enum_has_more(enum):
        paras.append(enum.nextElement())
    d = text.createTextCursorByRange(paras[0].getStart())
    e = text.createTextCursorByRange(paras[2].getStart())
    e.goRight(len("end "), False)
    d.gotoRange(e, True)
    doc.setPropertyValue("RecordChanges", True)
    try:
        d.setString("")
    finally:
        doc.setPropertyValue("RecordChanges", False)

    assert doc.getRedlines().getCount() == 1
    mid_enum = paras[1].createEnumeration()
    while enum_has_more(mid_enum):
        assert mid_enum.nextElement().getPropertyValue("TextPortionType") != "Redline"

    ruby_html = document_to_content(doc, ctx, scope="full")
    assert "<ruby>" not in ruby_html, ruby_html


@native_test
@with_native_doc("writer")
def test_full_html_keeps_deleted_paragraph_breaks_and_list_joins_uno(ctx, doc):
    import re
    from plugin.tests.testing_utils import strike_across_paragraphs
    from plugin.writer.html_export import document_to_content

    text = doc.getText()
    strike_across_paragraphs(doc, text)
    redline_count = doc.getRedlines().getCount()
    desktop = ctx.getServiceManager().createInstanceWithContext("com.sun.star.frame.Desktop", ctx)
    frames_before = desktop.getFrames().getCount()

    html = document_to_content(doc, ctx, scope="full")
    assert doc.getRedlines().getCount() == redline_count
    assert desktop.getFrames().getCount() == frames_before
    p_texts = re.findall(r"<p\b[^>]*>(.*?)</p>", html, re.DOTALL)
    assert any("Start" in p and "Tail" not in p for p in p_texts), html
    assert any("Tail" in p and "Start" not in p for p in p_texts), html

    # List join: 3 numbered items, strike across middle item.
    text.setString("")
    cur = text.createTextCursor()
    for idx, label in enumerate(("One", "Two", "Three")):
        if idx > 0:
            text.insertControlCharacter(cur, 0, False)
        text.insertString(cur, label, False)
        cur.gotoStartOfParagraph(False)
        cur.setPropertyValue("NumberingStyleName", "Numbering 123")
        cur.gotoEndOfParagraph(False)

    enum = text.createEnumeration()
    items = []
    while enum_has_more(enum):
        items.append(enum.nextElement())
    d_cur = text.createTextCursorByRange(items[0].getEnd())
    d_cur.gotoRange(items[2].getStart(), True)
    doc.setPropertyValue("RecordChanges", True)
    try:
        d_cur.setString("")
    finally:
        doc.setPropertyValue("RecordChanges", False)

    list_html = document_to_content(doc, ctx, scope="full")
    li_texts = re.findall(r"<li\b[^>]*>(.*?)</li>", list_html, re.DOTALL)
    assert any("One" in li and "Three" not in li for li in li_texts), list_html
    assert any("Three" in li and "One" not in li for li in li_texts), list_html


@native_test
@with_native_doc("writer")
def test_full_html_struck_range_with_comment_stays_one_paragraph_uno(ctx, doc):
    import re
    from plugin.writer.html_export import document_to_content

    text = doc.getText()
    text.setString("Keep struck rest")
    ann_cur = text.createTextCursor()
    ann_cur.gotoStart(False)
    ann_cur.goRight(len("Keep str"), False)
    ann = doc.createInstance("com.sun.star.text.textfield.Annotation")
    ann.setPropertyValue("Author", "Tester")
    ann.setPropertyValue("Content", "note")
    text.insertTextContent(ann_cur, ann, False)

    del_cur = text.createTextCursor()
    del_cur.gotoStart(False)
    del_cur.goRight(len("Keep "), False)
    del_cur.goRight(len("struck ") + 1, True)  # includes annotation anchor
    doc.setPropertyValue("RecordChanges", True)
    try:
        del_cur.setString("")
    finally:
        doc.setPropertyValue("RecordChanges", False)

    html = document_to_content(doc, ctx, scope="full")
    p_texts = re.findall(r"<p\b[^>]*>(.*?)</p>", html, re.DOTALL)
    assert len(p_texts) == 1, html
    assert "Keep" in p_texts[0] and "rest" in p_texts[0], html


@native_test
@with_native_doc("writer")
def test_titled_frame_omits_title_from_content_uno(ctx, doc):
    from plugin.writer.html_export import document_to_content

    text = doc.getText()
    frame = doc.createInstance("com.sun.star.text.TextFrame")
    frame.setName("TitledFrame")
    frame.setPropertyValue("Title", "SecretFrameTitle")
    frame.setPropertyValue("Width", 2000)
    frame.setPropertyValue("Height", 1000)
    text.insertTextContent(text.getStart(), frame, False)
    frame.getText().setString("FrameBody")

    html = document_to_content(doc, ctx, scope="full")
    assert "FrameBody" in html
    assert "SecretFrameTitle" not in html


def _setup_struck_doc(
    doc: Any,
    paragraphs: list[str],
    del_start: tuple[int, int],
    del_end: tuple[int, int],
    styles: list[str] | None = None,
) -> None:
    """Populate *doc* with *paragraphs* and record a deletion from (p, off) to (p, off)."""
    doc.setPropertyValue("RecordChanges", False)
    text = doc.getText()
    text.setString("")
    cur = text.createTextCursor()
    for idx, p in enumerate(paragraphs):
        if idx:
            text.insertControlCharacter(cur, 0, False)
        if styles:
            cur.setPropertyValue("ParaStyleName", styles[idx])
        if p:
            text.insertString(cur, p, False)

    enum = text.createEnumeration()
    paras = []
    while enum_has_more(enum):
        paras.append(enum.nextElement())
    sel = text.createTextCursorByRange(paras[del_start[0]].getStart())
    if del_start[1]:
        sel.goRight(del_start[1], False)
    end_cur = text.createTextCursorByRange(paras[del_end[0]].getStart())
    if del_end[1]:
        end_cur.goRight(del_end[1], False)
    sel.gotoRange(end_cur, True)
    doc.setPropertyValue("RecordChanges", True)
    sel.setString("")


def _block_styles_and_texts(html: str) -> list[tuple[str | None, str]]:
    import html as _html
    import re

    out: list[tuple[str | None, str]] = []
    for tag, attrs, inner in re.findall(r"<(p|h[1-6])\b([^>]*)(?<!/)>(.*?)</\1>", html, re.DOTALL):
        m = re.search(r'data-lo-style="([^"]+)"', attrs)
        style = m.group(1) if m else None
        text = _html.unescape(re.sub(r"<[^>]+>", "", inner))
        out.append((style, text))
    return out


@native_test
@with_native_doc("writer")
def test_struck_paragraphs_omitted_full_and_range_uno(ctx: Any, doc: Any) -> None:
    from plugin.doc.text_helpers import read_visible_body
    from plugin.writer.html_export import document_to_content

    four_styles = ["Heading 1", "Standard", "Quotations", "Text body"]
    two_styles = ["Heading 1", "Text body"]
    cases = [
        (["AA", "BB", "", "CC"], four_styles, (1, 0), (1, 2), [("Heading1", "AA"), ("Quotations", "\xa0"), ("Textbody", "CC")]),
        (["AA", "BB", "", "CC"], four_styles, (1, 0), (2, 0), [("Heading1", "AA"), ("Quotations", "\xa0"), ("Textbody", "CC")]),
        (["AA", "BB", "", "CC"], four_styles, (0, 2), (1, 2), [("Heading1", "AA"), ("Quotations", "\xa0"), ("Textbody", "CC")]),
        (["AA", "BB", "", "CC"], four_styles, (1, 2), (2, 0), [("Heading1", "AA"), ("Standard", "BB"), ("Textbody", "CC")]),
        (["AA", "BB", "", "CC"], four_styles, (2, 0), (3, 0), [("Heading1", "AA"), ("Standard", "BB"), ("Textbody", "CC")]),
        (["AA", "BB", "", "CC"], four_styles, (0, 0), (3, 0), [("Textbody", "CC")]),
        (["AA", "BB", "", "CC"], four_styles, (0, 2), (2, 0), [("Heading1", "AA"), ("Textbody", "CC")]),
        (["AA", "CC"], two_styles, (0, 2), (1, 2), [("Heading1", "AA")]),
        (["AA", "CC"], two_styles, (0, 0), (1, 2), []),
    ]
    desktop = ctx.getServiceManager().createInstanceWithContext("com.sun.star.frame.Desktop", ctx)
    for paragraphs, styles, d_start, d_end, expected in cases:
        _setup_struck_doc(doc, paragraphs, d_start, d_end, styles=styles)
        redlines_before = doc.getRedlines().getCount()
        frames_before = desktop.getFrames().getCount()
        full_html = document_to_content(doc, ctx, scope="full")
        assert _block_styles_and_texts(full_html) == expected, (paragraphs, d_start, d_end, full_html)
        doc_len = len(read_visible_body(doc)[0])
        range_html = document_to_content(doc, ctx, scope="range", range_start=0, range_end=doc_len + 1)
        assert _block_styles_and_texts(range_html) == expected, (paragraphs, d_start, d_end, range_html)
        assert doc.getRedlines().getCount() == redlines_before
        assert desktop.getFrames().getCount() == frames_before


@native_test
@with_native_doc("writer")
def test_numbered_list_with_struck_middle_item_uno(ctx: Any, doc: Any) -> None:
    import re
    from plugin.doc.text_helpers import read_visible_body
    from plugin.writer.html_export import document_to_content

    doc.setPropertyValue("RecordChanges", False)
    text = doc.getText()
    text.setString("")
    cur = text.createTextCursor()
    cur.setPropertyValue("NumberingStyleName", "Numbering 123")
    for idx, label in enumerate(("First", "Middle", "Third")):
        if idx:
            text.insertControlCharacter(cur, 0, False)
        text.insertString(cur, label, False)

    enum = text.createEnumeration()
    paras = []
    while enum_has_more(enum):
        paras.append(enum.nextElement())
    doc.setPropertyValue("RecordChanges", True)
    try:
        sel = text.createTextCursorByRange(paras[1].getStart())
        sel.gotoRange(paras[2].getStart(), True)
        sel.setString("")
        full_html = re.sub(r"\n+", "\n", document_to_content(doc, ctx, scope="full")).strip()
        doc_len = len(read_visible_body(doc)[0])
        range_html = re.sub(
            r"\n+", "\n", document_to_content(doc, ctx, scope="range", range_start=0, range_end=doc_len + 1)
        ).strip()
        assert "Middle" not in full_html and "Middle" not in range_html
        assert full_html.count("<ol>") == 1 and full_html.count("<li>") == 2, full_html
        items = [
            (
                re.sub(r"<[^>]+>", "", lbl).strip(),
                re.sub(r"<[^>]+>", "", body).replace("\xa0", "").strip(),
            )
            for lbl, body in re.findall(r"<li><p[^>]*>(<span[^>]*>[^<]*</span>)(.*?)</p></li>", full_html, re.DOTALL)
        ]
        assert items == [("1.", "First"), ("3.", "Third")], full_html
        assert range_html == (
            '<p data-lo-style="Standard">First</p>\n'
            '<p data-lo-style="Standard">Third</p>'
        ), range_html
    finally:
        doc.setPropertyValue("RecordChanges", False)


@native_test
@with_native_doc("writer")
def test_multi_paragraph_deletion_exact_output_uno(ctx: Any, doc: Any) -> None:
    import re
    from plugin.doc.text_helpers import read_visible_body
    from plugin.writer.html_export import document_to_content

    _setup_struck_doc(doc, ["Keep before", "Deleted 1", "Deleted 2", "Deleted 3", "Keep after"], (1, 0), (4, 0))
    expected = (
        '<p data-lo-style="Standard">Keep before</p>\n'
        '<p data-lo-style="Standard">Keep after</p>'
    )
    full_html = re.sub(r"\n+", "\n", document_to_content(doc, ctx, scope="full")).strip()
    doc_len = len(read_visible_body(doc)[0])
    range_html = re.sub(
        r"\n+", "\n", document_to_content(doc, ctx, scope="range", range_start=0, range_end=doc_len + 1)
    ).strip()
    assert full_html == expected, full_html
    assert range_html == expected, range_html


@native_test
@with_native_doc("writer")
def test_struck_text_with_live_paragraph_anchored_image_kept_uno(ctx: Any, doc: Any) -> None:
    import base64
    import os
    import tempfile
    from plugin.doc.text_helpers import read_visible_body
    from plugin.writer.html_export import document_to_content

    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(png)
        img_path = f.name

    try:
        text = doc.getText()
        text.setString("")
        cur = text.createTextCursor()
        text.insertString(cur, "AA", False)
        text.insertControlCharacter(cur, 0, False)
        text.insertString(cur, "BB", False)
        graphic = doc.createInstance("com.sun.star.text.TextGraphicObject")
        graphic.setPropertyValue("GraphicURL", uno.systemPathToFileUrl(img_path))
        graphic.setPropertyValue(
            "AnchorType", uno.Enum("com.sun.star.text.TextContentAnchorType", "AT_PARAGRAPH")
        )
        cur.gotoStartOfParagraph(False)
        text.insertTextContent(cur, graphic, False)

        enum = text.createEnumeration()
        paras = []
        while enum_has_more(enum):
            paras.append(enum.nextElement())
        sel = text.createTextCursorByRange(paras[1].getStart())
        sel.gotoEndOfParagraph(True)
        doc.setPropertyValue("RecordChanges", True)
        sel.setString("")

        full_html = document_to_content(doc, ctx, scope="full", include_images=True)
        doc_len = len(read_visible_body(doc)[0])
        range_html = document_to_content(
            doc, ctx, scope="range", range_start=0, range_end=doc_len + 1, include_images=True
        )
        assert "<img" in full_html, full_html
        assert "<img" in range_html, range_html
        assert "BB" not in full_html and "BB" not in range_html
        assert read_visible_body(doc)[2] == []
    finally:
        os.unlink(img_path)


@native_test
@with_native_doc("writer")
def test_exported_blocks_match_accepted_paragraph_count_uno(ctx: Any, doc: Any) -> None:
    from plugin.writer.html_export import document_to_content

    cases = [
        (["AA", "BB", "", "CC"], (1, 0), (2, 0)),
        (["AA", "BB", "", "CC"], (1, 2), (2, 0)),
        (["AA", "BB", "", "CC"], (2, 0), (3, 0)),
        (["AA", "BB", "", "CC"], (0, 2), (2, 0)),
        (["AA", "BB", "", "CC"], (0, 0), (3, 0)),
        (["AA", "CC"], (0, 2), (1, 2)),
    ]
    for paragraphs, d_start, d_end in cases:
        _setup_struck_doc(doc, paragraphs, d_start, d_end)
        blocks = _block_styles_and_texts(document_to_content(doc, ctx, scope="full"))
        accept_all_changes(doc, ctx)
        enum = doc.getText().createEnumeration()
        accepted_count = 0
        while enum_has_more(enum):
            enum.nextElement()
            accepted_count += 1
        assert len(blocks) == accepted_count, (paragraphs, d_start, d_end, blocks, accepted_count)

    # Text struck without its paragraph break is also omitted, though Accept All leaves an empty paragraph.
    text_only_cases = [
        (["AA", "BB", "", "CC"], (1, 0), (1, 2), 3, 4),
        (["AA", "CC"], (0, 0), (1, 2), 0, 1),
    ]
    for paragraphs, d_start, d_end, expected_exported, expected_accepted in text_only_cases:
        _setup_struck_doc(doc, paragraphs, d_start, d_end)
        blocks = _block_styles_and_texts(document_to_content(doc, ctx, scope="full"))
        assert len(blocks) == expected_exported, (paragraphs, d_start, d_end, blocks)
        accept_all_changes(doc, ctx)
        enum = doc.getText().createEnumeration()
        accepted_count = 0
        while enum_has_more(enum):
            enum.nextElement()
            accepted_count += 1
        assert accepted_count == expected_accepted, (paragraphs, d_start, d_end, accepted_count)


@native_test
@with_native_doc("writer")
def test_whole_doc_strike_list_and_heading_omitted_uno(ctx: Any, doc: Any) -> None:
    from plugin.writer.html_export import document_to_content

    # Whole-document strike of two paragraphs must not leak struck text via StarWriter fallback.
    _setup_struck_doc(doc, ["FirstGone", "SecondGone"], (0, 0), (1, 10))
    whole_html = document_to_content(doc, ctx, scope="full")
    assert whole_html == "", whole_html

    # Struck numbered list item and struck Heading 1 are omitted.
    doc.setPropertyValue("RecordChanges", False)
    text = doc.getText()
    text.setString("")
    cur = text.createTextCursor()
    cur.setPropertyValue("ParaStyleName", "Heading 1")
    text.insertString(cur, "StruckH1", False)
    text.insertControlCharacter(cur, 0, False)
    cur.setPropertyValue("ParaStyleName", "Standard")
    cur.setPropertyValue("NumberingStyleName", "Numbering 123")
    text.insertString(cur, "StruckItem", False)
    text.insertControlCharacter(cur, 0, False)
    cur.setPropertyValue("NumberingStyleName", "")
    text.insertString(cur, "LiveTail", False)

    enum = text.createEnumeration()
    paras = []
    while enum_has_more(enum):
        paras.append(enum.nextElement())
    doc.setPropertyValue("RecordChanges", True)
    for idx in (0, 1):
        c = text.createTextCursorByRange(paras[idx].getStart())
        c.gotoEndOfParagraph(True)
        c.setString("")
    html = document_to_content(doc, ctx, scope="full")
    assert html == '<p data-lo-style="Standard">LiveTail</p>', html


@native_test
@with_native_doc("writer")
def test_full_html_struck_run_keeps_direct_formatting_uno(ctx, doc):
    import re

    from plugin.writer.html_export import document_to_content

    text = doc.getText()
    cur = text.createTextCursor()
    text.insertString(cur, "BoldPara", False)
    text.insertControlCharacter(cur, 0, False)
    text.insertString(cur, "Keep StruckRun", False)

    bold_cur = text.createTextCursorByRange(text.getStart())
    bold_cur.gotoEndOfParagraph(True)
    bold_cur.setPropertyValue("CharWeight", 150.0)

    doc.setPropertyValue("RecordChanges", True)
    try:
        del_cur = text.createTextCursor()
        del_cur.gotoEnd(False)
        del_cur.goLeft(len("StruckRun"), True)
        del_cur.setString("")
    finally:
        doc.setPropertyValue("RecordChanges", False)

    html = document_to_content(doc, ctx, scope="full")
    assert "StruckRun" not in html
    blocks = re.findall(r"<p\b.*?</p>", re.sub(r"\s+", " ", html))
    bold_block = next(b for b in blocks if "BoldPara" in b)
    keep_block = next(b for b in blocks if "Keep" in b)
    bold_open = bold_block.split(">", 1)[0]
    keep_open = keep_block.split(">", 1)[0]
    assert "data-lo-para=" in bold_open and "font-weight:bold" in bold_open, bold_block
    assert "data-lo-para=" not in keep_open, keep_block


_TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _insert_tiny_png(doc: Any, target_text: Any, cursor: Any, anchor_type: Any, name: str) -> str:
    import base64
    import os
    import tempfile

    png = base64.b64decode(_TINY_PNG_B64)
    fd, img_path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    with open(img_path, "wb") as f:
        f.write(png)
    g = doc.createInstance("com.sun.star.text.TextGraphicObject")
    g.setPropertyValue("GraphicURL", uno.systemPathToFileUrl(img_path))
    g.setPropertyValue("AnchorType", anchor_type)
    g.setPropertyValue("Width", 400)
    g.setPropertyValue("Height", 400)
    target_text.insertTextContent(cursor, g, False)
    g.setName(name)
    return img_path


@native_test
@with_native_doc("writer", reuse=False)
def test_delete_table_omitted_from_full_and_range_reads_uno(ctx: Any, doc: Any) -> None:
    from plugin.doc.text_helpers import get_document_length
    from plugin.framework.uno_context import execute_dispatch
    from plugin.writer.html_export import document_to_content
    from plugin.writer.specialized.tables import TableDelete

    text = doc.getText()
    cur = text.createTextCursor()
    text.insertString(cur, "Before", False)
    text.insertControlCharacter(cur, 0, False)
    tbl = doc.createInstance("com.sun.star.text.TextTable")
    tbl.initialize(2, 2)
    text.insertTextContent(cur, tbl, False)
    tbl.setName("DelTbl")
    tbl.getCellByName("A1").setString("CellOne")
    tbl.getCellByName("B2").setString("CellTwo")
    text.insertControlCharacter(cur, 0, False)
    text.insertString(cur, "After", False)

    tctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native", doc_type="writer")
    doc.setPropertyValue("RecordChanges", True)
    try:
        res = TableDelete().execute(tctx, name="DelTbl")
        assert res["status"] == "ok", res
        len_before = get_document_length(doc)
        full_html = document_to_content(doc, ctx, scope="full")
        range_html = document_to_content(doc, ctx, scope="range", range_start=0, range_end=len_before)
        assert get_document_length(doc) == len_before
        for html in (full_html, range_html):
            assert "<table" not in html, html
            assert "CellOne" not in html, html
            assert "CellTwo" not in html, html
            assert "Before" in html and "After" in html, html
        execute_dispatch(ctx, doc.getCurrentController(), ".uno:AcceptAllTrackedChanges")
        assert doc.getTextTables().getCount() == 0
    finally:
        doc.setPropertyValue("RecordChanges", False)


@native_test
@with_native_doc("writer", reuse=False)
def test_body_with_only_struck_table_returns_empty_full_read_uno(ctx: Any, doc: Any) -> None:
    from plugin.writer.html_export import document_to_content
    from plugin.writer.specialized.tables import TableDelete

    text = doc.getText()
    cur = text.createTextCursor()
    tbl = doc.createInstance("com.sun.star.text.TextTable")
    tbl.initialize(1, 1)
    text.insertTextContent(cur, tbl, False)
    tbl.setName("OnlyTbl")
    tbl.getCellByName("A1").setString("SECRET")

    tctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native", doc_type="writer")
    doc.setPropertyValue("RecordChanges", True)
    try:
        res = TableDelete().execute(tctx, name="OnlyTbl")
        assert res["status"] == "ok", res
        warnings: list[str] = []
        html = document_to_content(doc, ctx, scope="full", walk_warnings=warnings)
        assert warnings == [], warnings
        assert html == "", repr(html)
    finally:
        doc.setPropertyValue("RecordChanges", False)


@native_test
@with_native_doc("writer", reuse=False)
def test_nested_delete_table_omitted_from_full_and_range_reads_uno(ctx: Any, doc: Any) -> None:
    from plugin.doc.text_helpers import get_document_length
    from plugin.writer.html_export import document_to_content
    from plugin.writer.specialized.tables import TableDelete

    text = doc.getText()
    cur = text.createTextCursor()
    outer = doc.createInstance("com.sun.star.text.TextTable")
    outer.initialize(1, 1)
    text.insertTextContent(cur, outer, False)
    outer.setName("OuterTbl")
    host_cell = outer.getCellByName("A1")
    host_cur = host_cell.createTextCursor()
    host_cell.insertString(host_cur, "HostText", False)
    host_cell.insertControlCharacter(host_cur, 0, False)
    inner = doc.createInstance("com.sun.star.text.TextTable")
    inner.initialize(1, 1)
    host_cell.insertTextContent(host_cur, inner, False)
    inner.setName("InnerTbl")
    inner.getCellByName("A1").setString("InnerGone")

    tctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native", doc_type="writer")
    doc.setPropertyValue("RecordChanges", True)
    try:
        res = TableDelete().execute(tctx, name="InnerTbl")
        assert res["status"] == "ok", res
        full_html = document_to_content(doc, ctx, scope="full")
        range_html = document_to_content(doc, ctx, scope="range", range_start=0, range_end=get_document_length(doc))
        for html in (full_html, range_html):
            assert "HostText" in html, html
            assert "InnerGone" not in html, html
            assert html.count("<table") == 1, html
    finally:
        doc.setPropertyValue("RecordChanges", False)


@native_test
@with_native_doc("writer", reuse=False)
def test_delete_row_and_struck_cell_para_omission_agrees_in_full_and_range_uno(ctx: Any, doc: Any) -> None:
    from plugin.doc.text_helpers import get_document_length
    from plugin.framework.uno_context import execute_dispatch
    from plugin.writer.html_export import document_to_content

    text = doc.getText()
    cur = text.createTextCursor()
    tbl = doc.createInstance("com.sun.star.text.TextTable")
    tbl.initialize(2, 1)
    text.insertTextContent(cur, tbl, False)
    tbl.setName("RowTbl")

    a1 = tbl.getCellByName("A1")
    c1 = a1.createTextCursor()
    a1.insertString(c1, "Top", False)
    a1.insertControlCharacter(c1, 0, False)
    a1.insertString(c1, "MidGone", False)
    a1.insertControlCharacter(c1, 0, False)
    a1.insertString(c1, "Bot", False)
    a2 = tbl.getCellByName("A2")
    a2.setString("Row2Gone")

    doc.setPropertyValue("RecordChanges", True)
    try:
        # Strike MidGone (middle paragraph of A1)
        del_cur = a1.createTextCursor()
        del_cur.gotoStart(False)
        del_cur.gotoNextParagraph(False)
        del_cur.gotoEndOfParagraph(True)
        del_cur.setString("")
        # Delete row 2 via dispatch
        doc.getCurrentController().select(a2.getStart())
        execute_dispatch(ctx, doc.getCurrentController(), ".uno:DeleteRows")

        full_html = document_to_content(doc, ctx, scope="full")
        range_html = document_to_content(doc, ctx, scope="range", range_start=0, range_end=get_document_length(doc))
        assert "Row2Gone" not in full_html and "Row2Gone" not in range_html
        assert full_html.count("<tr") == 2 and range_html.count("<tr") == 2
        assert _block_styles_and_texts(full_html) == [(None, "Top"), (None, "Bot")], full_html
        assert _block_styles_and_texts(range_html) == [(None, "Top"), (None, "Bot")], range_html
    finally:
        doc.setPropertyValue("RecordChanges", False)


@native_test
@with_native_doc("writer", reuse=False)
def test_struck_as_character_image_only_table_omitted_uno(ctx: Any, doc: Any) -> None:
    import os
    from com.sun.star.text.TextContentAnchorType import AS_CHARACTER
    from plugin.doc.text_helpers import DeletionState, get_document_length
    from plugin.framework.uno_context import execute_dispatch
    from plugin.writer.html_export import document_to_content
    from plugin.writer.range_copy import table_is_struck
    from plugin.writer.specialized.tables import TableDelete

    text = doc.getText()
    cur = text.createTextCursor()
    text.insertString(cur, "Lead", False)
    text.insertControlCharacter(cur, 0, False)
    tbl = doc.createInstance("com.sun.star.text.TextTable")
    tbl.initialize(1, 1)
    text.insertTextContent(cur, tbl, False)
    tbl.setName("ImgTbl")
    a1 = tbl.getCellByName("A1")
    img_path = _insert_tiny_png(doc, a1, a1.createTextCursor(), AS_CHARACTER, "CellImg")

    tctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native", doc_type="writer")
    doc.setPropertyValue("RecordChanges", True)
    try:
        res = TableDelete().execute(tctx, name="ImgTbl")
        assert res["status"] == "ok", res
        assert table_is_struck(doc, tbl, DeletionState()) is True
        full_html = document_to_content(doc, ctx, scope="full")
        range_html = document_to_content(doc, ctx, scope="range", range_start=0, range_end=get_document_length(doc))
        assert "<table" not in full_html and "<table" not in range_html
        execute_dispatch(ctx, doc.getCurrentController(), ".uno:AcceptAllTrackedChanges")
        assert doc.getTextTables().getCount() == 0
    finally:
        doc.setPropertyValue("RecordChanges", False)
        if os.path.exists(img_path):
            os.remove(img_path)


@native_test
@with_native_doc("writer", reuse=False)
def test_deletion_ending_on_inline_image_omitted_in_full_and_range_uno(ctx: Any, doc: Any) -> None:
    import os
    from com.sun.star.text.TextContentAnchorType import AS_CHARACTER
    from plugin.doc.text_helpers import get_document_length
    from plugin.writer.html_export import document_to_content

    text = doc.getText()
    cur = text.createTextCursor()
    text.insertString(cur, "AA", False)
    text.insertControlCharacter(cur, 0, False)
    text.insertString(cur, "BB", False)
    text.insertControlCharacter(cur, 0, False)
    img_path = _insert_tiny_png(doc, text, cur, AS_CHARACTER, "InlineEndImg")
    text.insertControlCharacter(cur, 0, False)
    text.insertString(cur, "CC", False)

    doc.setPropertyValue("RecordChanges", True)
    try:
        del_cur = text.createTextCursor()
        del_cur.gotoStart(False)
        del_cur.gotoNextParagraph(False)
        del_cur.gotoNextParagraph(True)
        del_cur.gotoEndOfParagraph(True)
        del_cur.setString("")

        full_html = document_to_content(doc, ctx, scope="full", include_images=True)
        range_html = document_to_content(
            doc, ctx, scope="range", range_start=0, range_end=get_document_length(doc), include_images=True
        )
        assert "<img" not in full_html and "<img" not in range_html
        assert _block_styles_and_texts(full_html) == [("Standard", "AA"), ("Standard", "CC")], full_html
        assert _block_styles_and_texts(range_html) == [("Standard", "AA"), ("Standard", "CC")], range_html
    finally:
        doc.setPropertyValue("RecordChanges", False)
        if os.path.exists(img_path):
            os.remove(img_path)


@native_test
@with_native_doc("writer", reuse=False)
def test_deletion_spanning_at_paragraph_image_omits_image_in_range_and_accept_uno(ctx: Any, doc: Any) -> None:
    import os
    from com.sun.star.text.TextContentAnchorType import AT_PARAGRAPH
    from plugin.doc.text_helpers import get_document_length
    from plugin.framework.uno_context import execute_dispatch
    from plugin.writer.html_export import document_to_content

    text = doc.getText()
    cur = text.createTextCursor()
    text.insertString(cur, "AA", False)
    text.insertControlCharacter(cur, 0, False)
    text.insertString(cur, "BB", False)
    img_cur = text.createTextCursorByRange(cur)
    img_path = _insert_tiny_png(doc, text, img_cur, AT_PARAGRAPH, "MidAtParaImg")
    text.insertControlCharacter(cur, 0, False)
    text.insertString(cur, "CC", False)
    text.insertControlCharacter(cur, 0, False)
    text.insertString(cur, "DD", False)

    pre_range_html = document_to_content(
        doc, ctx, scope="range", range_start=0, range_end=get_document_length(doc), include_images=True
    )
    assert "<img" in pre_range_html, pre_range_html

    doc.setPropertyValue("RecordChanges", True)
    try:
        del_cur = text.createTextCursor()
        del_cur.gotoStart(False)
        del_cur.goRight(1, False)
        del_cur.gotoNextParagraph(True)
        del_cur.gotoNextParagraph(True)
        del_cur.goRight(1, True)
        del_cur.setString("")

        full_html = document_to_content(doc, ctx, scope="full", include_images=True)
        range_html = document_to_content(
            doc, ctx, scope="range", range_start=0, range_end=get_document_length(doc), include_images=True
        )
        assert _block_styles_and_texts(full_html) == [
            ("Standard", "A"), ("Standard", "C"), ("Standard", "DD")
        ], full_html
        assert _block_styles_and_texts(range_html) == [
            ("Standard", "A"), ("Standard", "C"), ("Standard", "DD")
        ], range_html
        assert "<img" not in full_html and "<img" not in range_html
        execute_dispatch(ctx, doc.getCurrentController(), ".uno:AcceptAllTrackedChanges")
        assert doc.getGraphicObjects().getCount() == 0
    finally:
        doc.setPropertyValue("RecordChanges", False)
        if os.path.exists(img_path):
            os.remove(img_path)


@native_test
@with_native_doc("writer")
def test_full_export_includes_hidden_and_conditional_sections_uno(ctx, doc):
    from plugin.framework.html_stripper import html_to_search_text
    from plugin.writer.html_export import document_to_content

    text = doc.getText()
    text.setString("")
    cur = text.createTextCursor()
    text.insertString(cur, "aaa", False)
    text.insertControlCharacter(cur, 0, False)
    text.insertString(cur, "hid", False)
    text.insertControlCharacter(cur, 0, False)
    text.insertString(cur, "cond", False)
    text.insertControlCharacter(cur, 0, False)
    text.insertString(cur, "ccc", False)

    p_cur = text.createTextCursor()
    p_cur.gotoStart(False)
    p_cur.gotoNextParagraph(False)
    p_cur.gotoEndOfParagraph(True)
    sec_plain = doc.createInstance("com.sun.star.text.TextSection")
    sec_plain.setName("PlainHid")
    sec_plain.setPropertyValue("IsVisible", False)
    text.insertTextContent(p_cur, sec_plain, True)

    p_cur.gotoNextParagraph(False)
    p_cur.gotoStartOfParagraph(False)
    p_cur.gotoEndOfParagraph(True)
    sec_cond = doc.createInstance("com.sun.star.text.TextSection")
    sec_cond.setName("CondHid")
    sec_cond.setPropertyValue("Condition", "1 == 1")
    sec_cond.setPropertyValue("IsVisible", False)
    text.insertTextContent(p_cur, sec_cond, True)

    doc.setModified(False)
    full_html = document_to_content(doc, ctx, scope="full")
    range_html = document_to_content(doc, ctx, scope="range", range_start=0, range_end=len(text.getString()))
    assert "display:condition" not in full_html
    assert html_to_search_text(full_html).strip() == html_to_search_text(range_html).strip()
    assert "hid" in full_html and "cond" in full_html
    assert doc.getTextSections().getByName("PlainHid").getPropertyValue("IsVisible") is False
    assert doc.getTextSections().getByName("CondHid").getPropertyValue("IsVisible") is False
    assert doc.isModified() is False


@native_test
@with_native_doc("writer")
def test_range_export_keeps_ruby_after_content_control_uno(ctx, doc):
    from plugin.doc.text_helpers import visible_body_range
    from plugin.writer.html_export import document_to_content

    text = doc.getText()
    text.setString("CTRL 漢字")
    cur = text.createTextCursor()
    cur.gotoStart(False)
    cur.goRight(4, True)
    ctrl = doc.createInstance("com.sun.star.text.ContentControl")
    text.insertTextContent(cur, ctrl, True)

    ruby_rng = visible_body_range(doc, 5, 7)
    assert ruby_rng is not None
    ruby_cur = ruby_rng.cursor
    assert ruby_cur.getString() == "漢字"
    ruby_cur.setPropertyValue("RubyText", "かんじ")
    ruby_cur.setPropertyValue("RubyIsAbove", True)
    ruby_cur.setPropertyValue("RubyAdjust", 0)

    range_html = document_to_content(doc, ctx, scope="range", range_start=0, range_end=7)
    assert "<ruby>漢字<rt>かんじ</rt></ruby>" in range_html


@native_test
@with_native_doc("writer")
def test_full_export_rewrites_bare_shapes_and_drops_empty_or_controls_uno(ctx, doc):
    import re
    from com.sun.star.awt import Point, Size
    from plugin.writer.html_export import document_to_content

    text = doc.getText()
    text.setString("Host")

    def _add_shape(service: str, name: str, title: str, label: str = ""):
        shape = doc.createInstance(service)
        shape.setPosition(Point(1000, 1000))
        shape.setSize(Size(3000, 1000))
        text.insertTextContent(text.getEnd(), shape, False)
        shape.setName(name)
        shape.setPropertyValue("Title", title)
        if label:
            shape.setString(label)
        return shape

    _add_shape("com.sun.star.drawing.RectangleShape", "RectText", "TITLE-R1", "TEXT-R1")
    _add_shape("com.sun.star.drawing.RectangleShape", "RectEmpty", "TITLE-R2")
    _add_shape("com.sun.star.drawing.ConnectorShape", "ConnEmpty", "TITLE-C1")
    _add_shape("com.sun.star.drawing.ControlShape", "Ctrl1", "TITLE-CTRL")

    html = document_to_content(doc, ctx, scope="full")
    assert "TITLE-" not in html
    assert "RectEmpty" not in html
    assert "ConnEmpty" not in html
    assert "Ctrl1" not in html
    assert re.search(r'id="RectText"[^>]*>(?:(?!</span>|</div>).)*TEXT-R1', html, re.DOTALL), html
    assert html.count("<p") == 1, html


@native_test
@with_native_doc("writer")
def test_full_export_drops_ole_metadata_and_keeps_preview_and_math_uno(ctx, doc):
    from plugin.writer.html_export import document_to_content

    text = doc.getText()
    text.setString("Before\n")

    for clsid in (
        "12DCAE26-281F-416F-a234-c3086127382e",  # Chart
        "47BBB4CB-CE4C-4E80-a591-42d9ae74950f",  # Calc
    ):
        ole = doc.createInstance("com.sun.star.text.TextEmbeddedObject")
        ole.setPropertyValue("CLSID", clsid)
        text.insertTextContent(text.getEnd(), ole, False)

    formula = doc.createInstance("com.sun.star.text.TextEmbeddedObject")
    formula.setPropertyValue("CLSID", "078B7ABA-54FC-457F-8551-6147e776a997")
    text.insertTextContent(text.getEnd(), formula, False)
    formula.getEmbeddedObject().setPropertyValue("Formula", "a over b")

    import re

    html = document_to_content(doc, ctx, scope="full", include_images=False)
    assert "LibreOffice/" not in html
    assert "background-image:url()" not in html
    assert "base64" not in html
    assert 'id="Object1"' in html
    assert 'id="Object2"' in html
    assert r"\frac{a}{b}" in html
    visible = re.sub(r"<[^>]+>", " ", html).split()
    assert sorted(visible) == [r"$\frac{a}{b}$", "Before"], visible


@native_test
@with_native_doc("writer")
def test_range_export_keeps_picture_and_surrounding_text_links_uno(ctx, doc):
    import os
    from com.sun.star.text.TextContentAnchorType import AS_CHARACTER, AT_CHARACTER, AT_PARAGRAPH
    from plugin.writer.html_export import document_to_content

    text = doc.getText()
    for anchor_type in (AS_CHARACTER, AT_CHARACTER, AT_PARAGRAPH):
        for i in range(doc.getGraphicObjects().getCount() - 1, -1, -1):
            doc.getGraphicObjects().getByIndex(i).dispose()
        text.setString("first ")
        cur = text.createTextCursor()
        cur.gotoStart(False)
        cur.goRight(5, True)
        cur.setPropertyValue("HyperLinkURL", "https://text/l")
        cur.collapseToEnd()
        cur.goRight(1, False)
        img_path = _insert_tiny_png(doc, text, cur, anchor_type, "PicLink")
        try:
            doc.getGraphicObjects().getByIndex(0).setPropertyValue("HyperLinkURL", "https://pic/l")
            text.insertString(text.getEnd(), " other", False)
            ocur = text.createTextCursor()
            ocur.gotoEnd(False)
            ocur.goLeft(5, True)
            ocur.setPropertyValue("HyperLinkURL", "https://other/o")
            html = document_to_content(doc, ctx, scope="range", range_start=0, range_end=len(text.getString()))
            expected = (
                ["https://pic/l", "https://text/l", "https://other/o"]
                if anchor_type == AT_PARAGRAPH
                else ["https://text/l", "https://pic/l", "https://other/o"]
            )
            assert _hrefs(html) == expected, (anchor_type, html)
        finally:
            os.unlink(img_path)

    # Unlinked AS_CHARACTER picture inside a text link followed by a different link.
    for i in range(doc.getGraphicObjects().getCount() - 1, -1, -1):
        doc.getGraphicObjects().getByIndex(i).dispose()
    text.setString("before after other")
    cur = text.createTextCursor()
    cur.gotoStart(False)
    cur.goRight(12, True)
    cur.setPropertyValue("HyperLinkURL", "https://text/l")
    cur.collapseToStart()
    cur.goRight(7, False)
    img_path = _insert_tiny_png(doc, text, cur, AS_CHARACTER, "UnlinkedInside")
    try:
        ocur = text.createTextCursor()
        ocur.gotoEnd(False)
        ocur.goLeft(5, True)
        ocur.setPropertyValue("HyperLinkURL", "https://other/o")
        html = document_to_content(doc, ctx, scope="range", range_start=0, range_end=len(text.getString()))
        assert _hrefs(html) == ["https://text/l", "https://text/l", "https://other/o"], html
    finally:
        os.unlink(img_path)


@native_test
@with_native_doc("writer")
def test_outline_picture_link_and_text_outline_link_restore_uno(ctx, doc):
    import os
    from com.sun.star.text.TextContentAnchorType import AS_CHARACTER
    from plugin.writer.html_export import document_to_content

    text = doc.getText()
    text.setString("Heading Target\nGo ")
    hcur = text.createTextCursor()
    hcur.gotoStart(False)
    hcur.setPropertyValue("ParaStyleName", "Heading 1")
    ecur = text.createTextCursor()
    ecur.gotoEnd(False)
    ecur.goLeft(2, True)
    ecur.setPropertyValue("HyperLinkURL", "#1.Heading%20Target|outline")
    ecur.collapseToEnd()
    img_path = _insert_tiny_png(doc, text, ecur, AS_CHARACTER, "PicOutline")
    try:
        doc.getGraphicObjects().getByIndex(0).setPropertyValue("HyperLinkURL", "#1.Heading%20Target|outline")
        full_html = document_to_content(doc, ctx, scope="full")
        range_html = document_to_content(doc, ctx, scope="range", range_start=0, range_end=len(text.getString()))
        assert _hrefs(full_html) == ["#1.Heading%20Target|outline", "#1.Heading%20Target|outline"], full_html
        assert _hrefs(range_html) == ["#1.Heading%20Target|outline", "#1.Heading%20Target|outline"], range_html
    finally:
        os.unlink(img_path)


@native_test
def test_shared_list_full_read_hides_tracked_deletions_uno(ctx):
    """Shared-list XHTML fallback hides pending deletions and points at scope='range'."""
    import os
    import tempfile

    from com.sun.star.beans import PropertyValue

    from plugin.app import get_services
    from plugin.framework.tool import ToolContext
    from plugin.writer.get_document_content import GetDocumentContent
    from tests.writer.redlined_contract_fixture import DELETED, INSERTED, build_redlined_contract

    contract = build_redlined_contract(
        articles=20,
        sections=4,
        clauses=7,
        tables=2,
        multi_every=40,
        shared_list=True,
    )
    with tempfile.TemporaryDirectory() as folder:
        path = os.path.join(folder, "shared_list.fodt")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(contract.xml)
        url = uno.systemPathToFileUrl(path)
        desktop = ctx.getServiceManager().createInstanceWithContext("com.sun.star.frame.Desktop", ctx)
        hidden = PropertyValue()
        hidden.Name, hidden.Value = "Hidden", True
        doc = desktop.loadComponentFromURL(url, "_blank", 0, (hidden,))
        assert doc is not None
        try:
            tool_ctx = ToolContext(doc, ctx, "writer", get_services(), "test")
            get_content = GetDocumentContent()

            # Relies on odf2xhtml countListItem recursion hitting xsltMaxDepth on 560 shared-style items;
            # if LibreOffice fixes that recursion, retarget this test to whatever triggers fallback.
            full_res = get_content.execute(tool_ctx, scope="full", max_chars=500000)
            assert full_res.get("status") == "ok", full_res
            assert full_res.get("warning"), full_res
            full_content = full_res.get("content") or ""
            assert DELETED.strip() not in full_content
            assert "whole struck clause" not in full_content
            assert INSERTED.strip() in full_content
            assert "Clause 1:" in full_content

            range_res = get_content.execute(tool_ctx, scope="range", start=0, end=5000)
            assert range_res.get("status") == "ok", range_res
            assert not range_res.get("warning"), range_res
            assert "data-lo-style" in (range_res.get("content") or "")
        finally:
            doc.close(True)
