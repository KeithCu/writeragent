# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""UNO tests for Writer HTML range export (html_export)."""

from typing import Any

import uno  # noqa: F401

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import skip_windows_leftover_hidden_load, with_native_doc


@native_test
@with_native_doc("writer")
def test_range_export_keeps_bold_inside_odd_para_uno(ctx: Any, doc: Any) -> None:
    """Range export must still show emphasis when the source paragraph carries
    unusual Para* values. A refused Para* used to skip Char* paint entirely."""
    # GHA 34690797019: leftover writer reuse then
    # html_export._range_to_content_via_temp_doc Hidden `_default`
    # hung 30s on temp_doc.close(True) after XHTML. SkipTest still
    # runs finally — skip before the factory load.
    skip_windows_leftover_hidden_load("html_export Hidden _default temp_doc")
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

    html = _range_to_content_via_temp_doc(doc, ctx, 0, 16, None, None)
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
    """_copy_cell_xtext used to skip in-cell TextTables; dest must keep the nest."""
    skip_windows_leftover_hidden_load("html_export Hidden _default temp_doc")
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

    from plugin.writer.html_export import _copy_xtext_into_doc, _open_hidden_writer
    from plugin.writer.specialized.tables import TableGetCells, TableList
    from plugin.tests.testing_utils import TestingFactory

    temp_doc = None
    try:
        temp_doc = _open_hidden_writer(ctx)
        _copy_xtext_into_doc(doc, doc.getText(), temp_doc)
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
    skip_windows_leftover_hidden_load("html_export Hidden _default temp_doc")
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

    from plugin.writer.html_export import _copy_xtext_into_doc, _open_hidden_writer
    from plugin.writer.specialized.tables import TableGetCells, TableList
    from plugin.tests.testing_utils import TestingFactory

    temp_doc = None
    try:
        temp_doc = _open_hidden_writer(ctx)
        _copy_xtext_into_doc(doc, doc.getText(), temp_doc)
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
    skip_windows_leftover_hidden_load("html_export Hidden _default temp_doc")
    from plugin.framework.tool import ToolContext
    from plugin.main import get_services
    from plugin.writer.content import GetDocumentContent
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
    ranged = _range_to_content_via_temp_doc(doc, ctx, start, start + len(toc), None, None)
    assert "TOC_ENTRY_TITLE" in ranged, ranged
    assert body not in ranged, ranged
    assert "|outline" in ranged or "%7Coutline" in ranged, ranged


def _hrefs(html: str) -> list[str]:
    import re
    return re.findall(r'href="([^"]*)"', html)


@native_test
@with_native_doc("writer")
def test_split_outline_run_does_not_shift_the_next_href_uno(ctx: Any, doc: Any) -> None:
    """A bold split is two portions and one anchor. The next link keeps its own URL.

    Writing one URL per portion used to put the first target on the second anchor.
    """
    skip_windows_leftover_hidden_load("html_export Hidden _default temp_doc")
    from plugin.framework.tool import ToolContext
    from plugin.main import get_services
    from plugin.writer.content import GetDocumentContent

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
    skip_windows_leftover_hidden_load("html_export Hidden _default temp_doc")
    from plugin.framework.tool import ToolContext
    from plugin.main import get_services
    from plugin.tests.testing_utils import TestingFactory
    from plugin.writer.content import GetDocumentContent
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
    created = IndexesCreate().execute(tctx, kind="toc", title="Contents", target="beginning")
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
    """Full XHTML used to concatenate 漢字+かんじ; the agent must see <ruby>/<rt>."""
    skip_windows_leftover_hidden_load("html_export Hidden _default temp_doc")
    from plugin.tests.testing_utils import TestingFactory
    from plugin.writer.content import GetDocumentContent

    _seed_ruby_paragraph(doc)
    assert doc.getText().getString() == "漢字です"

    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    result = GetDocumentContent().execute(tool_ctx, scope="full")
    assert result.get("status") == "ok", result
    _assert_semantic_ruby(result.get("content") or "")


@native_test
@with_native_doc("writer")
def test_range_get_document_content_preserves_ruby_reading_uno(ctx: Any, doc: Any) -> None:
    """Range copy used to drop RubyText; reading must still appear in <rt>."""
    skip_windows_leftover_hidden_load("html_export Hidden _default temp_doc")
    from plugin.tests.testing_utils import TestingFactory
    from plugin.writer.content import GetDocumentContent

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
    skip_windows_leftover_hidden_load("html_export Hidden _default temp_doc")
    from plugin.framework.tool import ToolContext
    from plugin.main import get_services
    from plugin.writer.content import GetDocumentContent

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
    search_in_document still matches 漢字 and not かんじ.
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
