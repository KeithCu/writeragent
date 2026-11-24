# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""UNO tests for Writer HTML range export (html_export)."""

from typing import Any

import uno  # noqa: F401

from plugin.testing_runner import native_test
from tests.testing_utils import with_native_doc


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
    from tests.testing_utils import TestingFactory

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
    from tests.testing_utils import TestingFactory

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
    from tests.testing_utils import TestingFactory
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
    while enum.hasMoreElements():
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
    from tests.testing_utils import TestingFactory
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
    from tests.testing_utils import TestingFactory
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
    while en.hasMoreElements():
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
    from plugin.framework.uno_context import enum_has_more
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

    from plugin.framework.uno_context import enum_has_more
    from plugin.writer import html_export
    from plugin.writer.range_copy import PARAGRAPH_BREAK

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
    """_offset_window stays aligned with get_string_without_tracked_deletions across tables."""
    import re
    from plugin.doc.text_helpers import get_string_without_tracked_deletions
    from plugin.writer.html_export import document_to_content

    def _body_structure() -> list[str]:
        from plugin.framework.uno_context import enum_has_more

        enum = doc.getText().createEnumeration()
        out = []
        while enum_has_more(enum):
            el = enum.nextElement()
            if el.supportsService("com.sun.star.text.TextTable"):
                out.append("TextTable")
            else:
                out.append(f"Paragraph:{el.getString()}")
        return out

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
            return ["Alpha", "Beta"], ["Paragraph:Alpha", "TextTable", "Paragraph:Beta"]
        if layout == "leading":
            _add_table("T1")
            _fill_trailing_para("Beta")
            return ["Beta"], ["TextTable", "Paragraph:Beta"]
        if layout == "adjacent_and_later":
            text.insertString(cur, "Alpha", False)
            _add_table("T1")
            _add_table("T2")
            _fill_trailing_para("Beta")
            _add_table("T3")
            _fill_trailing_para("Gamma")
            return (
                ["Alpha", "Beta", "Gamma"],
                ["Paragraph:Alpha", "TextTable", "TextTable", "Paragraph:Beta", "TextTable", "Paragraph:Gamma"],
            )
        # empty paragraph after table
        text.insertString(cur, "Alpha", False)
        _add_table("T1")
        cur.gotoEnd(False)
        text.insertControlCharacter(cur, 0, False)
        _fill_trailing_para("Gamma")
        return ["Alpha", "Gamma"], ["Paragraph:Alpha", "TextTable", "Paragraph:", "Paragraph:Gamma"]

    for layout in ("middle", "leading", "adjacent_and_later", "empty_after"):
        words, expected_structure = _reset_and_build(layout)
        assert _body_structure() == expected_structure, (layout, _body_structure())
        whole = doc.getText().createTextCursor()
        whole.gotoStart(False)
        whole.gotoEnd(True)
        full_str = get_string_without_tracked_deletions(whole)
        for word in words:
            offset = full_str.index(word)
            html = document_to_content(
                doc, ctx, scope="range", range_start=offset, range_end=offset + len(word)
            )
            plain = re.sub(r"<[^>]+>", "", html).strip()
            if layout == "leading" and offset == 0:
                assert "".join(plain.split()) == "T1" + word, (layout, word, offset, html)
            else:
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
    import uno  # noqa: F401
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
    from plugin.framework.uno_context import enum_has_more

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
    while enum.hasMoreElements():
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
    while enum.hasMoreElements():
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
    while mid_enum.hasMoreElements():
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
    while enum.hasMoreElements():
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
    while mid_enum.hasMoreElements():
        assert mid_enum.nextElement().getPropertyValue("TextPortionType") != "Redline"

    ruby_html = document_to_content(doc, ctx, scope="full")
    assert "<ruby>" not in ruby_html, ruby_html
