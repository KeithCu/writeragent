# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Regression test: applying a paragraph style via apply_style must not wipe DIRECT
# character formatting (color/bold/highlight) already set on the target text. The fix
# captures the direct char overrides (values that differ from the old style's defaults)
# and restores them after setting ParaStyleName.
import uno  # noqa: F401

from plugin.testing_runner import native_test, setup, teardown
from plugin.writer.styles import ApplyStyle
from plugin.tests.testing_utils import TestingFactory

_test_doc = None
_test_ctx = None


@setup
def my_setup(ctx):
    global _test_doc, _test_ctx
    _test_ctx = ctx
    _test_doc = TestingFactory.create_native_doc(ctx, doc_type="writer")


@teardown
def my_teardown(ctx):
    global _test_doc
    if _test_doc:
        _test_doc.close(True)
    _test_doc = None


@native_test
def test_apply_paragraph_style_preserves_direct_char_format_uno():
    """Applying a PARAGRAPH style must not wipe DIRECT character formatting
    (color/bold) already set on the text."""
    doc = _test_doc
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
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=_test_ctx, env="native")
    res = ApplyStyle().execute(
        tool_ctx, style_name="Standard", family="ParagraphStyles", target="full_document"
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
def test_apply_paragraph_style_via_search_preserves_whole_paragraph_uno():
    """Regression (found via live MCP testing 2026-06-01): when a PARAGRAPH style is
    applied with target='search' matching only a SUBSTRING, the style still resets the
    WHOLE paragraph's direct char formatting. The capture must therefore cover the
    whole paragraph, not just the matched range, so direct formatting OUTSIDE the
    match survives too."""
    doc = _test_doc
    text = doc.getText()
    text.setString("")
    cur = text.createTextCursor()
    text.insertString(cur, "Result figures by a judge final.", False)

    # whole paragraph red + bold (direct)
    fmt = text.createTextCursorByRange(text.getStart())
    fmt.gotoEnd(True)
    fmt.setPropertyValue("CharColor", 0xFF0000)
    fmt.setPropertyValue("CharWeight", 150.0)

    tool_ctx = TestingFactory.create_context(doc=doc, ctx=_test_ctx, env="native")
    # search matches ONLY a substring in the middle of the paragraph
    res = ApplyStyle().execute(
        tool_ctx, style_name="Quotations", family="ParagraphStyles",
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
def test_apply_style_known_limitation_direct_equals_old_default_uno():
    """Characterizes a KNOWN LIMITATION: detection is
    'differs from the style default', not 'was set directly'. So a DIRECT override
    whose value EQUALS the old style's default is NOT preserved, and can become
    visible when a style with a different default is applied.

    Scenario: a Standard paragraph (default CharWeight=100) with CharWeight=100 set
    DIRECTLY; apply Heading 1 (default bold=150). The ideal would be to stay 100, but
    today it becomes 150. This test PINS the current behavior; if we ever improve the
    origin detection, it fails and reminds us to update."""
    doc = _test_doc
    text = doc.getText()
    insert_cur = text.createTextCursor()
    text.insertString(insert_cur, "Directly-normal text.", False)

    fmt = text.createTextCursorByRange(text.getStart())
    fmt.gotoEnd(True)
    fmt.setPropertyValue("CharWeight", 100.0)  # NORMAL, set directly = Standard's default
    assert fmt.getPropertyValue("CharWeight") == 100.0

    tool_ctx = TestingFactory.create_context(doc=doc, ctx=_test_ctx, env="native")
    res = ApplyStyle().execute(
        tool_ctx, style_name="Heading 1", family="ParagraphStyles", target="full_document"
    )
    assert res.get("status") == "ok", f"apply_style failed: {res}"

    chk = text.createTextCursorByRange(text.getStart())
    chk.gotoEnd(True)
    # LIMITATION: the 'normal' override (= Standard's default) was not preserved -> Heading 1 bold.
    assert chk.getPropertyValue("CharWeight") == 150.0, \
        "if this fails, origin detection improved — update the doc/limitation"


@native_test
def test_apply_paragraph_style_preserves_char_back_color_uno():
    """Direct highlight (CharBackColor) must survive a paragraph style change."""
    doc = _test_doc
    text = doc.getText()
    text.setString("")
    cur = text.createTextCursor()
    text.insertString(cur, "Highlighted clause text.", False)

    fmt = text.createTextCursorByRange(text.getStart())
    fmt.gotoEnd(True)
    fmt.setPropertyValue("CharBackColor", 0xFFFF00)  # yellow highlight

    tool_ctx = TestingFactory.create_context(doc=doc, ctx=_test_ctx, env="native")
    res = ApplyStyle().execute(
        tool_ctx, style_name="Quotations", family="ParagraphStyles", target="full_document"
    )
    assert res.get("status") == "ok", f"apply_style failed: {res}"

    chk = text.createTextCursorByRange(text.getStart())
    chk.gotoEnd(True)
    assert int(chk.getPropertyValue("CharBackColor")) == 0xFFFF00, \
        "apply_style wiped the direct highlight (CharBackColor)"


@native_test
def test_apply_paragraph_style_preserves_applied_character_style_uno():
    """An applied character style (CharStyleName) should survive paragraph style change."""
    doc = _test_doc
    text = doc.getText()
    text.setString("")
    cur = text.createTextCursor()
    text.insertString(cur, "Emphasized legal term.", False)

    fmt = text.createTextCursorByRange(text.getStart())
    fmt.gotoEnd(True)
    fmt.setPropertyValue("CharStyleName", "Emphasis")

    tool_ctx = TestingFactory.create_context(doc=doc, ctx=_test_ctx, env="native")
    res = ApplyStyle().execute(
        tool_ctx, style_name="Heading 2", family="ParagraphStyles", target="full_document"
    )
    assert res.get("status") == "ok", f"apply_style failed: {res}"

    chk = text.createTextCursorByRange(text.getStart())
    chk.gotoEnd(True)
    assert chk.getPropertyValue("CharStyleName") == "Emphasis", \
        "apply_style wiped the applied character style"


@native_test
def test_apply_paragraph_style_multi_paragraph_selection_uno():
    """Direct formatting in both paragraphs must survive when style applies to a two-para span."""
    doc = _test_doc
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

    tool_ctx = TestingFactory.create_context(doc=doc, ctx=_test_ctx, env="native")
    # Simulate selection spanning both paragraphs via full_document (same capture path).
    res = ApplyStyle().execute(
        tool_ctx, style_name="Text body", family="ParagraphStyles", target="full_document"
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
