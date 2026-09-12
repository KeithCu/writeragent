# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Regression test: applying a paragraph style via apply_style must not wipe DIRECT
# character emphasis (color/bold/highlight) already set on the target text. Default
# clear_direct is style_props: house font/size win, bold/italic/colour stay. The
# helper captures Char* that differ from the old style default and restores
# everything except STYLE_GOVERNED_CHAR_PROPERTIES (font name/size).
import uno  # noqa: F401

from plugin.testing_runner import native_test
from plugin.writer.styles import ApplyStyle, StyleUpdate
from plugin.tests.testing_utils import (
    TestingFactory,
    skip_windows_leftover_hidden_load,
    with_native_doc,
)

_HOUSE_FONT = "Liberation Sans"
_DIRECT_FONT = "Times New Roman"


@native_test
@with_native_doc("writer")
def test_apply_paragraph_style_preserves_direct_char_format_uno(ctx, doc):
    """Applying a PARAGRAPH style must not wipe DIRECT character formatting
    (color/bold) already set on the text."""
    text = doc.getText()

    # 1) insert text
    insert_cur = text.createTextCursor()
    text.insertString(insert_cur, "Important contract clause.", False)

    # 2) apply DIRECT character formatting over the whole text
    fmt = text.createTextCursorByRange(text.getStart())
    fmt.gotoEnd(True)
    fmt.setPropertyValue("CharColor", 0xFF0000)   # red
    fmt.setPropertyValue("CharWeight", 150.0)     # com.sun.star.awt.FontWeight.BOLD

    # sanity: the direct formatting was actually applied
    assert int(fmt.getPropertyValue("CharColor")) == 0xFF0000
    assert fmt.getPropertyValue("CharWeight") == 150.0

    # 3) apply a PARAGRAPH style via the tool (target = whole document)
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    res = ApplyStyle().execute(
        tool_ctx, style="Standard", family="ParagraphStyles", target="full_document"
    )
    assert res.get("status") == "ok", f"apply_style failed: {res}"

    # 4) EXPECTED: the direct character formatting survives the paragraph style
    chk = text.createTextCursorByRange(text.getStart())
    chk.gotoEnd(True)
    assert int(chk.getPropertyValue("CharColor")) == 0xFF0000, \
        "apply_style wiped the direct character COLOR"
    assert chk.getPropertyValue("CharWeight") == 150.0, \
        "apply_style wiped the direct character BOLD"


@native_test
@with_native_doc("writer")
def test_apply_paragraph_style_via_search_preserves_whole_paragraph_uno(ctx, doc):
    """Regression (found via live MCP testing 2026-06-01): when a PARAGRAPH style is
    applied with target='search' matching only a SUBSTRING, the style still resets the
    WHOLE paragraph's direct char formatting. The capture must therefore cover the
    whole paragraph, not just the matched range, so direct formatting OUTSIDE the
    match survives too."""
    text = doc.getText()
    text.setString("")
    cur = text.createTextCursor()
    text.insertString(cur, "Result figures by a judge final.", False)

    # whole paragraph red + bold (direct)
    fmt = text.createTextCursorByRange(text.getStart())
    fmt.gotoEnd(True)
    fmt.setPropertyValue("CharColor", 0xFF0000)
    fmt.setPropertyValue("CharWeight", 150.0)

    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    # search matches ONLY a substring in the middle of the paragraph
    res = ApplyStyle().execute(
        tool_ctx, style="Quotations", family="ParagraphStyles",
        target="search", old_content="by a judge",
    )
    assert res.get("status") == "ok", f"apply_style failed: {res}"

    # check a portion OUTSIDE the matched range ("Result", the first word): it must
    # keep red+bold. Before the fix, only "by a judge" was preserved.
    lead = text.createTextCursorByRange(text.getStart())
    lead.goRight(6, True)  # "Result"
    assert int(lead.getPropertyValue("CharColor")) == 0xFF0000, \
        "direct COLOR lost outside the matched range"
    assert lead.getPropertyValue("CharWeight") == 150.0, \
        "direct BOLD lost outside the matched range"


@native_test
@with_native_doc("writer")
def test_apply_style_known_limitation_direct_equals_old_default_uno(ctx, doc):
    """Characterizes a KNOWN LIMITATION: detection is
    'differs from the style default', not 'was set directly'. So a DIRECT override
    whose value EQUALS the old style's default is NOT preserved, and can become
    visible when a style with a different default is applied.

    Scenario: a Standard paragraph (default CharWeight=100) with CharWeight=100 set
    DIRECTLY; apply Heading 1 (default bold=150). The ideal would be to stay 100, but
    today it becomes 150. This test PINS the current behavior; if we ever improve the
    origin detection, it fails and reminds us to update."""
    # GHA 34683742049: leftover writer reuse (after Quotations apply) flipped
    # this canary. Product still compares value vs old style default
    # (format.apply_paragraph_style_preserving_direct_char); Linux still
    # pins CharWeight=150. Not origin detection improved — leftover style
    # table / para state is not a clean Standard→Heading 1 fixture.
    skip_windows_leftover_hidden_load("apply_style origin canary leftover reuse")
    text = doc.getText()
    insert_cur = text.createTextCursor()
    text.insertString(insert_cur, "Directly-normal text.", False)

    fmt = text.createTextCursorByRange(text.getStart())
    fmt.gotoEnd(True)
    fmt.setPropertyValue("CharWeight", 100.0)  # NORMAL, set directly = Standard's default
    assert fmt.getPropertyValue("CharWeight") == 100.0

    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    res = ApplyStyle().execute(
        tool_ctx, style="Heading 1", family="ParagraphStyles", target="full_document"
    )
    assert res.get("status") == "ok", f"apply_style failed: {res}"

    chk = text.createTextCursorByRange(text.getStart())
    chk.gotoEnd(True)
    # LIMITATION: the 'normal' override (= Standard's default) was not preserved -> Heading 1 bold.
    assert chk.getPropertyValue("CharWeight") == 150.0, \
        "if this fails, origin detection improved — update the doc/limitation"


@native_test
@with_native_doc("writer")
def test_apply_paragraph_style_preserves_char_back_color_uno(ctx, doc):
    """Direct highlight (CharBackColor) must survive a paragraph style change."""
    text = doc.getText()
    text.setString("")
    cur = text.createTextCursor()
    text.insertString(cur, "Highlighted clause text.", False)

    fmt = text.createTextCursorByRange(text.getStart())
    fmt.gotoEnd(True)
    fmt.setPropertyValue("CharBackColor", 0xFFFF00)  # yellow highlight

    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    res = ApplyStyle().execute(
        tool_ctx, style="Quotations", family="ParagraphStyles", target="full_document"
    )
    assert res.get("status") == "ok", f"apply_style failed: {res}"

    chk = text.createTextCursorByRange(text.getStart())
    chk.gotoEnd(True)
    assert int(chk.getPropertyValue("CharBackColor")) == 0xFFFF00, \
        "apply_style wiped the direct highlight (CharBackColor)"


@native_test
@with_native_doc("writer")
def test_apply_paragraph_style_preserves_applied_character_style_uno(ctx, doc):
    """An applied character style (CharStyleName) should survive paragraph style change."""
    text = doc.getText()
    text.setString("")
    cur = text.createTextCursor()
    text.insertString(cur, "Emphasized legal term.", False)

    fmt = text.createTextCursorByRange(text.getStart())
    fmt.gotoEnd(True)
    fmt.setPropertyValue("CharStyleName", "Emphasis")

    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    res = ApplyStyle().execute(
        tool_ctx, style="Heading 2", family="ParagraphStyles", target="full_document"
    )
    assert res.get("status") == "ok", f"apply_style failed: {res}"

    chk = text.createTextCursorByRange(text.getStart())
    chk.gotoEnd(True)
    assert chk.getPropertyValue("CharStyleName") == "Emphasis", \
        "apply_style wiped the applied character style"


@native_test
@with_native_doc("writer")
def test_apply_paragraph_style_multi_paragraph_selection_uno(ctx, doc):
    """Direct formatting in both paragraphs must survive when style applies to a two-para span."""
    text = doc.getText()
    text.setString("")
    cur = text.createTextCursor()
    text.insertString(cur, "First paragraph here.", False)
    cur.gotoEnd(False)
    text.insertControlCharacter(cur, 0, False)  # PARAGRAPH_BREAK
    text.insertString(cur, "Second paragraph here.", False)

    for start_offset in (0, len("First paragraph here.") + 1):
        fmt = text.createTextCursor()
        fmt.gotoStart(False)
        fmt.goRight(start_offset, False)
        fmt.goRight(21 if start_offset == 0 else 22, True)
        fmt.setPropertyValue("CharColor", 0xFF0000)

    sel = text.createTextCursor()
    sel.gotoStart(False)
    sel.gotoEnd(True)

    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    # Simulate selection spanning both paragraphs via full_document (same capture path).
    res = ApplyStyle().execute(
        tool_ctx, style="Text body", family="ParagraphStyles", target="full_document"
    )
    assert res.get("status") == "ok", f"apply_style failed: {res}"

    p1 = text.createTextCursor()
    p1.gotoStart(False)
    p1.goRight(5, True)
    assert int(p1.getPropertyValue("CharColor")) == 0xFF0000, \
        "direct COLOR lost in first paragraph"

    p2 = text.createTextCursor()
    p2.gotoStart(False)
    p2.goRight(len("First paragraph here.") + 1 + 6, True)
    assert int(p2.getPropertyValue("CharColor")) == 0xFF0000, \
        "direct COLOR lost in second paragraph"


@native_test
@with_native_doc("writer")
def test_apply_style_default_shows_house_font_keeps_bold_uno(ctx, doc):
    """Lawyer path: Times + bold → update Standard house font → apply Standard with
    no clear_direct arg → house font shows, bold survives. Small models must not
    need a retry-with-style_props dance."""
    text = doc.getText()
    text.setString("")
    cur = text.createTextCursor()
    text.insertString(cur, "Lawyer clause in Times.", False)

    fmt = text.createTextCursorByRange(text.getStart())
    fmt.gotoEnd(True)
    fmt.setPropertyValue("CharFontName", _DIRECT_FONT)
    fmt.setPropertyValue("CharWeight", 150.0)
    assert fmt.getPropertyValue("CharFontName") == _DIRECT_FONT
    assert fmt.getPropertyValue("CharWeight") == 150.0

    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    upd = StyleUpdate().execute(
        tool_ctx, style="Standard", family="ParagraphStyles",
        property_updates={"CharFontName": _HOUSE_FONT},
    )
    assert upd.get("status") == "ok", upd

    res = ApplyStyle().execute(
        tool_ctx, style="Standard", family="ParagraphStyles",
        target="search", old_content="Lawyer clause in Times.",
    )
    assert res.get("status") == "ok", f"apply_style failed: {res}"
    assert "hint" not in res
    assert res.get("removed_char_overrides", {}).get("CharFontName") == _DIRECT_FONT

    chk = text.createTextCursorByRange(text.getStart())
    chk.gotoEnd(True)
    assert chk.getPropertyValue("CharFontName") == _HOUSE_FONT, (
        "default apply_style left the direct font standing (house font should win): %r"
        % chk.getPropertyValue("CharFontName"))
    assert chk.getPropertyValue("CharWeight") == 150.0, (
        "default apply_style wiped bold; style_props must keep emphasis")


@native_test
@with_native_doc("writer")
def test_apply_style_explicit_none_keeps_direct_font_uno(ctx, doc):
    """clear_direct='none' is the opt-in that still masks Times over the house font."""
    text = doc.getText()
    text.setString("")
    cur = text.createTextCursor()
    text.insertString(cur, "Keep Times on this clause.", False)

    fmt = text.createTextCursorByRange(text.getStart())
    fmt.gotoEnd(True)
    fmt.setPropertyValue("CharFontName", _DIRECT_FONT)
    fmt.setPropertyValue("CharWeight", 150.0)

    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    upd = StyleUpdate().execute(
        tool_ctx, style="Standard", family="ParagraphStyles",
        property_updates={"CharFontName": _HOUSE_FONT},
    )
    assert upd.get("status") == "ok", upd

    res = ApplyStyle().execute(
        tool_ctx, style="Standard", family="ParagraphStyles",
        target="search", old_content="Keep Times on this clause.",
        clear_direct="none",
    )
    assert res.get("status") == "ok", f"apply_style failed: {res}"
    assert "hint" not in res
    assert res.get("preserved_char_overrides", {}).get("CharFontName") == _DIRECT_FONT

    chk = text.createTextCursorByRange(text.getStart())
    chk.gotoEnd(True)
    assert chk.getPropertyValue("CharFontName") == _DIRECT_FONT, (
        "explicit none must keep the direct font, got %r" % chk.getPropertyValue("CharFontName"))
    assert chk.getPropertyValue("CharWeight") == 150.0


@native_test
@with_native_doc("writer")
def test_apply_style_reapply_drops_direct_para_indent_uno(ctx, doc):
    """Honest pin: setting ParaStyleName drops direct Para* (quote indents). Keith's F1
    probe — do not promise that re-applying a style keeps a hand-set margin, even on none."""
    text = doc.getText()
    text.setString("")
    cur = text.createTextCursor()
    text.insertString(cur, "Indented quote lookalike.", False)

    fmt = text.createTextCursorByRange(text.getStart())
    fmt.gotoEnd(True)
    fmt.setPropertyValue("ParaLeftMargin", 3251)  # ~3.25 cm
    assert int(fmt.getPropertyValue("ParaLeftMargin")) == 3251

    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    res = ApplyStyle().execute(
        tool_ctx, style="Standard", family="ParagraphStyles",
        target="search", old_content="Indented quote lookalike.",
        clear_direct="none",
    )
    assert res.get("status") == "ok", f"apply_style failed: {res}"

    chk = text.createTextCursorByRange(text.getStart())
    chk.gotoEnd(True)
    assert int(chk.getPropertyValue("ParaLeftMargin")) != 3251, (
        "if this fails, re-apply started keeping direct Para* — update the docs")


@native_test
@with_native_doc("writer")
def test_apply_style_returns_structured_fields_uno(ctx, doc):
    """apply_style returns target/applied; matched=True only when target='search'."""
    text = doc.getText()
    text.setString("")
    cur = text.createTextCursor()
    text.insertString(cur, "Mark this clause.", False)
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")

    res = ApplyStyle().execute(tool_ctx, style="Standard", family="ParagraphStyles", target="full_document")
    assert res.get("status") == "ok", res
    assert res.get("applied") is True, res
    assert res.get("target") == "full_document", res
    assert "matched" not in res, res  # full_document is not a search

    res2 = ApplyStyle().execute(tool_ctx, style="Standard", family="ParagraphStyles", target="search", old_content="Mark this clause.")
    assert res2.get("status") == "ok", res2
    assert res2.get("target") == "search", res2
    assert res2.get("matched") is True, res2


@native_test
@with_native_doc("writer")
def test_apply_style_search_miss_reports_not_matched_uno(ctx, doc):
    """A target='search' miss returns an error carrying matched=False / applied!=True
    (no silent ok), consistent with apply_document_content's no-match."""
    text = doc.getText()
    text.setString("")
    cur = text.createTextCursor()
    text.insertString(cur, "Some clause text.", False)
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    res = ApplyStyle().execute(
        tool_ctx, style="Standard", family="ParagraphStyles",
        target="search", old_content="NOT-PRESENT-XYZ",
    )
    assert res.get("status") == "error", res
    assert res.get("matched") is False, res
    assert res.get("applied") is not True, res
