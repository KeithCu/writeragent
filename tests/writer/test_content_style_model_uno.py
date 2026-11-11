# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# get_document_content embeds the paragraph style model in the HTML: each named-style block
# carries a COMPACT data-lo-style token (no spaces: "Heading1"), direct char overrides are
# inlined as style="..." (no synthetic classes leak), and synthetic autostyle paragraphs are
# omitted (write treats a missing token as the default body style). Read path = XHTML Writer
# File filter + pure string post-process; write path resolves compact tokens -> UNO names.
# See docs/writer/html-style-model-plan.md.
import uno  # noqa: F401

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import native_writer_tool_context, with_native_doc
from plugin.writer.apply_document_content import ApplyDocumentContent
import plugin.writer.html_export as fmt


def _tool_ctx(doc, ctx):
    # GHA 34683742049: heading-rewrite / structured / table / whitespace
    # leftover Hidden apply skips fired. Next
    # test_write_compact_heading1_resolves_to_spaced_uno hung 30s in
    # html_to_plain_text Hidden _default swriter (leftover_open=3).
    return native_writer_tool_context(
        doc, ctx, skip_reason="apply_document_content Hidden _default swriter"
    )


def _para_style_names(doc):
    text = doc.getText()
    out = []
    e = text.createEnumeration()
    while e.hasMoreElements():
        el = e.nextElement()
        if hasattr(el, "supportsService") and el.supportsService("com.sun.star.text.Paragraph"):
            out.append(el.getPropertyValue("ParaStyleName"))
    return out


# --- Read path: named styles -> compact data-lo-style tokens ---------------

@native_test
@with_native_doc("writer")
def test_read_named_styles_emit_compact_tokens_uno(ctx, doc):
    text = doc.getText()
    text.setString("")
    cur = text.createTextCursor()
    cur.gotoStart(False)
    cur.setPropertyValue("ParaStyleName", "Heading 1")
    text.insertString(cur, "Big Title", False)
    text.insertControlCharacter(cur, 0, False)  # PARAGRAPH_BREAK
    cur.setPropertyValue("ParaStyleName", "Caption")
    text.insertString(cur, "Figure 1 caption", False)
    text.insertControlCharacter(cur, 0, False)  # PARAGRAPH_BREAK
    cur.setPropertyValue("ParaStyleName", "Standard")
    cur.setPropertyValue("CharColor", -1)
    text.insertString(cur, "normal ", False)
    cur.setPropertyValue("CharColor", 0xFF0000)
    text.insertString(cur, "RED", False)
    cur.setPropertyValue("CharColor", -1)
    text.insertString(cur, " end", False)

    content = fmt.document_to_content(doc, ctx, scope="full")

    # Named styles surfaced as COMPACT (space-free) tokens.
    assert 'data-lo-style="Heading1"' in content, content
    assert 'data-lo-style="Caption"' in content, content
    # Direct char override inlined; no synthetic classes leak.
    assert "ff0000" in content.lower(), content
    assert "text-T" not in content, content
    assert "paragraph-" not in content, content


@native_test
@with_native_doc("writer")
def test_read_order_survives_table_uno(ctx, doc):
    """A table between two paragraphs must not desync style assignment, and cells must not leak."""
    text = doc.getText()
    text.setString("")
    cur = text.createTextCursor()
    cur.gotoStart(False)
    cur.setPropertyValue("ParaStyleName", "Heading 2")
    text.insertString(cur, "Before table", False)
    text.insertControlCharacter(cur, 0, False)  # PARAGRAPH_BREAK

    table = doc.createInstance("com.sun.star.text.TextTable")
    table.initialize(2, 2)
    text.insertTextContent(cur, table, False)

    cur.gotoEnd(False)
    cur.setPropertyValue("ParaStyleName", "Caption")
    text.insertString(cur, "After table", False)

    content = fmt.document_to_content(doc, ctx, scope="full")

    assert 'data-lo-style="Heading2"' in content, content
    assert 'data-lo-style="Caption"' in content, content
    i_before = content.index('data-lo-style="Heading2"')
    i_table = content.lower().index("<table")
    i_after = content.index('data-lo-style="Caption"')
    assert i_before < i_table < i_after, "table desynced data-lo-style ordering: %s" % content
    assert "paragraph-" not in content, content


# --- Write path: apply_document_content honors compact data-lo-style -------

@native_test
@with_native_doc("writer")
def test_write_compact_token_applies_uno(ctx, doc):
    """Compact tokens drive the paragraph style; 'Heading2' resolves to UNO 'Heading 2'."""
    res = ApplyDocumentContent().execute(
        _tool_ctx(doc, ctx), target="full_document",
        content='<p data-lo-style="Caption">cap</p>\n<p data-lo-style="Heading2">head</p>')
    assert res.get("status") == "ok", res
    names = _para_style_names(doc)
    assert "Caption" in names, names
    assert "Heading 2" in names, names


@native_test
@with_native_doc("writer")
def test_write_compact_heading1_resolves_to_spaced_uno(ctx, doc):
    res = ApplyDocumentContent().execute(
        _tool_ctx(doc, ctx), target="full_document",
        content='<p data-lo-style="Heading1">title</p>')
    assert res.get("status") == "ok", res
    text = doc.getText()
    assert text.createTextCursorByRange(text.getStart()).getPropertyValue("ParaStyleName") == "Heading 1", \
        _para_style_names(doc)


@native_test
@with_native_doc("writer")
def test_write_compact_textbody_resolves_to_spaced_uno(ctx, doc):
    """Regression: the compact token 'Textbody' (LO name 'Text body', lowercase b) must resolve
    to the real UNO 'Text body' — NOT fall back to Standard. Guards the prompt/code contract."""
    res = ApplyDocumentContent().execute(
        _tool_ctx(doc, ctx), target="full_document",
        content='<p data-lo-style="Textbody">body text</p>')
    assert res.get("status") == "ok", res
    text = doc.getText()
    assert text.createTextCursorByRange(text.getStart()).getPropertyValue("ParaStyleName") == "Text body", \
        _para_style_names(doc)


@native_test
@with_native_doc("writer")
def test_write_spaced_form_still_works_uno(ctx, doc):
    """Back-compat: an agent that passes the spaced UNO name still resolves correctly."""
    res = ApplyDocumentContent().execute(
        _tool_ctx(doc, ctx), target="full_document",
        content='<p data-lo-style="Heading 1">title</p>')
    assert res.get("status") == "ok", res
    text = doc.getText()
    assert text.createTextCursorByRange(text.getStart()).getPropertyValue("ParaStyleName") == "Heading 1", \
        _para_style_names(doc)


@native_test
@with_native_doc("writer")
def test_write_preserves_inline_override_uno(ctx, doc):
    """Applying the named style must not wipe the inline char override the import laid down."""
    res = ApplyDocumentContent().execute(
        _tool_ctx(doc, ctx), target="full_document",
        content='<p data-lo-style="Heading1">title <span style="color: #ff0000">red</span></p>')
    assert res.get("status") == "ok", res
    text = doc.getText()
    assert text.createTextCursorByRange(text.getStart()).getPropertyValue("ParaStyleName") == "Heading 1", \
        _para_style_names(doc)
    found_red = False
    enum = text.createEnumeration()
    while enum.hasMoreElements():
        para = enum.nextElement()
        if not (hasattr(para, "supportsService") and para.supportsService("com.sun.star.text.Paragraph")):
            continue
        pe = para.createEnumeration()
        while pe.hasMoreElements():
            portion = pe.nextElement()
            try:
                if int(portion.getPropertyValue("CharColor")) == 0xFF0000:
                    found_red = True
            except Exception:
                pass
    assert found_red, "inline red override was lost after applying data-lo-style"


@native_test
@with_native_doc("writer")
def test_write_unknown_token_falls_back_to_standard_uno(ctx, doc):
    res = ApplyDocumentContent().execute(
        _tool_ctx(doc, ctx), target="full_document",
        content='<p data-lo-style="NoSuchStyleXYZ">x</p>')
    assert res.get("status") == "ok", res
    assert "Standard" in _para_style_names(doc), _para_style_names(doc)


@native_test
@with_native_doc("writer")
def test_read_write_round_trip_uno(ctx, doc):
    """Read a styled doc -> semantic content (compact tokens) -> write it back -> styles preserved."""
    text = doc.getText()
    text.setString("")
    cur = text.createTextCursor()
    cur.gotoStart(False)
    cur.setPropertyValue("ParaStyleName", "Heading 1")
    text.insertString(cur, "Title", False)
    text.insertControlCharacter(cur, 0, False)  # PARAGRAPH_BREAK
    cur.setPropertyValue("ParaStyleName", "Caption")
    text.insertString(cur, "Cap", False)

    content = fmt.document_to_content(doc, ctx, scope="full")
    assert 'data-lo-style="Heading1"' in content, content  # compact token round-trips
    res = ApplyDocumentContent().execute(_tool_ctx(doc, ctx), target="full_document", content=content)
    assert res.get("status") == "ok", res
    names = _para_style_names(doc)
    assert "Heading 1" in names, names
    assert "Caption" in names, names


@native_test
@with_native_doc("writer")
def test_write_div_wrapper_does_not_desync_styles_uno(ctx, doc):
    """A <div> is a transparent container: it must not consume a positional style slot, so an
    off-contract wrapper does not shift styles onto the wrong paragraphs."""
    res = ApplyDocumentContent().execute(
        _tool_ctx(doc, ctx), target="full_document",
        content='<div><p data-lo-style="Heading1">first</p><p data-lo-style="Caption">second</p></div>')
    assert res.get("status") == "ok", res
    text = doc.getText()
    pairs = []
    e = text.createEnumeration()
    while e.hasMoreElements():
        el = e.nextElement()
        if hasattr(el, "supportsService") and el.supportsService("com.sun.star.text.Paragraph"):
            pairs.append((el.getPropertyValue("ParaStyleName"), el.getString()))
    by_text = {t: s for s, t in pairs}
    assert by_text.get("first") == "Heading 1", pairs
    assert by_text.get("second") == "Caption", pairs


@native_test
@with_native_doc("writer")
def test_write_ambiguous_token_falls_back_to_standard_uno(ctx, doc):
    """Issue 2: a literal style named 'Heading1' coexisting with built-in 'Heading 1' makes the
    token 'Heading1' ambiguous; the resolver must fail safe to 'Standard', never silently pick
    one of them."""
    fam = doc.getStyleFamilies().getByName("ParagraphStyles")
    created = False
    if not fam.hasByName("Heading1"):
        st = doc.createInstance("com.sun.star.style.ParagraphStyle")
        fam.insertByName("Heading1", st)
        created = True
    try:
        res = ApplyDocumentContent().execute(
            _tool_ctx(doc, ctx), target="full_document",
            content='<p data-lo-style="Heading1">x</p>')
        assert res.get("status") == "ok", res
        text = doc.getText()
        applied = text.createTextCursorByRange(text.getStart()).getPropertyValue("ParaStyleName")
        assert applied == "Standard", "ambiguous token resolved to %r instead of Standard" % applied
    finally:
        # Restore the shared document's style set.
        if created and fam.hasByName("Heading1"):
            fam.removeByName("Heading1")


@native_test
@with_native_doc("writer")
def test_write_data_lo_style_with_math_uno(ctx, doc):
    """A compact token must apply even when the paragraph contains math (math import branch)."""
    res = ApplyDocumentContent().execute(
        _tool_ctx(doc, ctx), target="full_document",
        content='<p data-lo-style="Heading1">Equation \\(x^2\\) here</p>')
    assert res.get("status") == "ok", res
    text = doc.getText()
    assert text.createTextCursorByRange(text.getStart()).getPropertyValue("ParaStyleName") == "Heading 1", \
        _para_style_names(doc)


@native_test
@with_native_doc("writer")
def test_write_then_read_recovers_token_via_fodt_uno(ctx, doc):
    """THE round-trip proof: writing a paragraph makes the StarWriter import stamp direct char
    props, so on re-export the paragraph is an autostyle whose XHTML CSS matches no named rule
    (the wall). The flat-ODF parent map recovers the real style name on re-read."""
    res = ApplyDocumentContent().execute(
        _tool_ctx(doc, ctx), target="full_document",
        content='<p data-lo-style="Caption">just a caption</p>')
    assert res.get("status") == "ok", res
    content = fmt.document_to_content(doc, ctx, scope="full")
    # Before the FODT fix this dropped the token; now it round-trips.
    assert 'data-lo-style="Caption"' in content, content
    assert "paragraph-" not in content, content


def _para_pairs(doc):
    text = doc.getText()
    out = []
    e = text.createEnumeration()
    while e.hasMoreElements():
        el = e.nextElement()
        if hasattr(el, "supportsService") and el.supportsService("com.sun.star.text.Paragraph"):
            out.append((el.getPropertyValue("ParaStyleName"), el.getString()))
    return out


def _accept_all(doc, ctx):
    helper = ctx.getServiceManager().createInstanceWithContext("com.sun.star.frame.DispatchHelper", ctx)
    helper.executeDispatch(doc.getCurrentController().getFrame(), ".uno:AcceptAllTrackedChanges", "", 0, ())


@native_test
@with_native_doc("writer")
def test_write_blank_beginning_applies_heading1_uno(ctx, doc):
    """Blank doc + target=beginning + styled HTML → first para is Heading 1."""
    text = doc.getText()
    text.setString("")
    res = ApplyDocumentContent().execute(
        _tool_ctx(doc, ctx), target="beginning",
        content=['<p data-lo-style="Heading1">Intro</p>', '<p>body</p>'])
    assert res.get("status") == "ok", res
    pairs = _para_pairs(doc)
    assert pairs, pairs
    assert pairs[0][0] == "Heading 1", pairs
    assert "Intro" in pairs[0][1], pairs
    assert any(t.strip() == "body" or t == "body" for _, t in pairs), pairs


@native_test
@with_native_doc("writer")
def test_write_beginning_styles_new_heading_keeps_existing_body_uno(ctx, doc):
    """target=beginning: new Heading1 is styled; existing body text + ParaStyleName unchanged."""
    text = doc.getText()
    text.setString("Existing body text")
    _baseline = text.createTextCursor()
    _baseline.gotoStart(False)
    _baseline.gotoEnd(True)
    # Prefer Text body when present; fall back to Standard (both are default body styles).
    try:
        _baseline.setPropertyValue("ParaStyleName", "Text body")
    except Exception:
        _baseline.setPropertyValue("ParaStyleName", "Standard")
    body_style = _baseline.getPropertyValue("ParaStyleName")
    res = ApplyDocumentContent().execute(
        _tool_ctx(doc, ctx), target="beginning",
        content=['<p data-lo-style="Heading1">Title</p>'])
    assert res.get("status") == "ok", res
    pairs = _para_pairs(doc)
    title = next(((s, t) for s, t in pairs if "Title" in t), None)
    assert title is not None and title[0] == "Heading 1", pairs
    existing = next(((s, t) for s, t in pairs if t.startswith("Existing body text")), None)
    assert existing is not None, pairs
    assert existing[0] == body_style, pairs
    assert existing[1] == "Existing body text", pairs


@native_test
@with_native_doc("writer")
def test_write_end_styles_new_paras_keeps_existing_body_uno(ctx, doc):
    """target=end: append styled blocks after existing content; neighbor style/text unchanged."""
    text = doc.getText()
    text.setString("Existing line")
    _baseline = text.createTextCursor()
    _baseline.gotoStart(False)
    _baseline.gotoEnd(True)
    _baseline.setPropertyValue("ParaStyleName", "Standard")
    res = ApplyDocumentContent().execute(
        _tool_ctx(doc, ctx), target="end",
        content=['<p data-lo-style="Caption">CAPX para</p>', '<p data-lo-style="Heading2">HEADX para</p>'])
    assert res.get("status") == "ok", res
    pairs = _para_pairs(doc)
    existing = next(((s, t) for s, t in pairs if t.startswith("Existing line")), None)
    assert existing is not None, pairs
    assert existing[0] == "Standard", pairs
    assert existing[1] == "Existing line", pairs
    cap = next(((s, t) for s, t in pairs if "CAPX" in t), None)
    head = next(((s, t) for s, t in pairs if "HEADX" in t), None)
    assert cap is not None and cap[0] == "Caption", pairs
    assert head is not None and head[0] == "Heading 2", pairs


@native_test
@with_native_doc("writer")
def test_write_search_does_not_restyle_surrounding_text_uno(ctx, doc):
    """target=search: a partial replace leaves neighbor text in the host paragraph, so data-lo-style
    is skipped and the tool warns. Surrounding text stays Standard."""
    text = doc.getText()
    text.setString("MARKER tail")
    _baseline = text.createTextCursor()
    _baseline.gotoStart(False)
    _baseline.gotoEnd(True)
    _baseline.setPropertyValue("ParaStyleName", "Standard")
    res = ApplyDocumentContent().execute(
        _tool_ctx(doc, ctx), target="search", old_content="MARKER",
        content=['<p data-lo-style="Caption">CAPX</p>', '<p data-lo-style="Heading2">HEADX</p>'])
    assert res.get("status") == "ok", res
    assert "warning" not in res
    assert "warnings" in res
    assert res["warnings"][0]["code"] == "STYLE_IGNORED_PARTIAL_MATCH"
    assert "apply_style(target='search', old_content=...)" in res["warnings"][0]["message"]
    assert "apply_style(target='search', old_content=...)" in res["message"]
    pairs = _para_pairs(doc)
    # Nothing got restyled to Caption/Heading 2 (styles are not applied on partial search).
    assert all(s not in ("Caption", "Heading 2") for s, _ in pairs), pairs
    # The content was still inserted and tail is still present.
    assert any("CAPX" in t for _, t in pairs), pairs
    assert any("tail" in t for _, t in pairs), pairs


@native_test
@with_native_doc("writer")
def test_write_search_whole_paragraph_applies_heading2_uno(ctx, doc):
    """Whole-paragraph search replace: Heading 2 applies; neighbor paragraphs stay Standard."""
    text = doc.getText()
    text.setString("")
    cur = text.createTextCursor()
    cur.gotoStart(False)
    cur.setPropertyValue("ParaStyleName", "Standard")
    text.insertString(cur, "Keep before", False)
    text.insertControlCharacter(cur, 0, False)
    cur.setPropertyValue("ParaStyleName", "Standard")
    text.insertString(cur, "Replace me", False)
    text.insertControlCharacter(cur, 0, False)
    cur.setPropertyValue("ParaStyleName", "Standard")
    text.insertString(cur, "Keep after", False)

    res = ApplyDocumentContent().execute(
        _tool_ctx(doc, ctx), target="search", old_content="Replace me",
        content='<p data-lo-style="Heading2">New heading</p>')
    assert res.get("status") == "ok", res
    assert "warning" not in res, res.get("warning")
    pairs = _para_pairs(doc)
    before = next(((s, t) for s, t in pairs if "Keep before" in t), None)
    new_head = next(((s, t) for s, t in pairs if "New heading" in t), None)
    after = next(((s, t) for s, t in pairs if "Keep after" in t), None)
    assert before is not None and before[0] == "Standard", pairs
    assert new_head is not None and new_head[0] == "Heading 2", pairs
    assert after is not None and after[0] == "Standard", pairs


@native_test
@with_native_doc("writer")
def test_write_search_whole_paragraph_applies_data_lo_para_uno(ctx, doc):
    """Whole-paragraph search replace applies data-lo-para properties to the inserted paragraph."""
    text = doc.getText()
    text.setString("")
    cur = text.createTextCursor()
    cur.gotoStart(False)
    cur.setPropertyValue("ParaStyleName", "Standard")
    text.insertString(cur, "Keep before", False)
    text.insertControlCharacter(cur, 0, False)
    cur.setPropertyValue("ParaStyleName", "Standard")
    text.insertString(cur, "Replace me", False)
    text.insertControlCharacter(cur, 0, False)
    cur.setPropertyValue("ParaStyleName", "Standard")
    text.insertString(cur, "Keep after", False)

    content = '<p data-lo-para="margin-left:2cm; text-align:center; background-color:#ffe4c4; font-size:12pt">New para</p>'
    res = ApplyDocumentContent().execute(
        _tool_ctx(doc, ctx), target="search", old_content="Replace me", content=content
    )
    assert res.get("status") == "ok", res
    assert res.get("warnings") == [
        {"code": "PARA_PROPERTY_UNSUPPORTED", "message": "data-lo-para property 'font-size' was not applied"}
    ]

    enum = doc.getText().createEnumeration()
    p1 = enum.nextElement()
    p2 = enum.nextElement()
    p3 = enum.nextElement()

    assert p1.getPropertyValue("ParaLeftMargin") == 0
    assert p1.getPropertyValue("ParaAdjust") == 5
    assert p1.getPropertyValue("FillStyle").value == "NONE"

    assert p2.getPropertyValue("ParaLeftMargin") == 2000
    assert p2.getPropertyValue("ParaAdjust") == 3
    assert p2.getPropertyValue("FillStyle").value == "SOLID"
    assert p2.getPropertyValue("FillColor") == 0xFFE4C4

    assert p3.getPropertyValue("ParaLeftMargin") == 0
    assert p3.getPropertyValue("ParaAdjust") == 5
    assert p3.getPropertyValue("FillStyle").value == "NONE"


@native_test
@with_native_doc("writer")
def test_write_selection_whole_paragraph_applies_heading2_uno(ctx, doc):
    """Whole-paragraph selection replace: Heading 2 applies; neighbor paragraphs stay Standard."""
    text = doc.getText()
    text.setString("")
    cur = text.createTextCursor()
    cur.gotoStart(False)
    cur.setPropertyValue("ParaStyleName", "Standard")
    text.insertString(cur, "Keep before", False)
    text.insertControlCharacter(cur, 0, False)
    cur.setPropertyValue("ParaStyleName", "Standard")
    text.insertString(cur, "Replace me", False)
    text.insertControlCharacter(cur, 0, False)
    cur.setPropertyValue("ParaStyleName", "Standard")
    text.insertString(cur, "Keep after", False)

    # Select the middle paragraph without including the following break
    cur.gotoStart(False)
    cur.gotoNextParagraph(False)
    cur.gotoStartOfParagraph(False)
    cur.gotoEndOfParagraph(True)
    doc.getCurrentController().select(cur)

    res = ApplyDocumentContent().execute(
        _tool_ctx(doc, ctx), target="selection",
        content='<p data-lo-style="Heading2">New heading</p>')
    assert res.get("status") == "ok", res
    assert "warning" not in res, res.get("warning")
    pairs = _para_pairs(doc)
    before = next(((s, t) for s, t in pairs if "Keep before" in t), None)
    new_head = next(((s, t) for s, t in pairs if "New heading" in t), None)
    after = next(((s, t) for s, t in pairs if "Keep after" in t), None)
    assert before is not None and before[0] == "Standard", pairs
    assert new_head is not None and new_head[0] == "Heading 2", pairs
    assert after is not None and after[0] == "Standard", pairs


@native_test
@with_native_doc("writer")
def test_write_selection_whole_paragraph_applies_data_lo_para_uno(ctx, doc):
    """Whole-paragraph selection replace applies data-lo-para properties to the inserted paragraph."""
    text = doc.getText()
    text.setString("")
    cur = text.createTextCursor()
    cur.gotoStart(False)
    cur.setPropertyValue("ParaStyleName", "Standard")
    text.insertString(cur, "Keep before", False)
    text.insertControlCharacter(cur, 0, False)
    cur.setPropertyValue("ParaStyleName", "Standard")
    text.insertString(cur, "Replace me", False)
    text.insertControlCharacter(cur, 0, False)
    cur.setPropertyValue("ParaStyleName", "Standard")
    text.insertString(cur, "Keep after", False)

    cur.gotoStart(False)
    cur.gotoNextParagraph(False)
    cur.gotoStartOfParagraph(False)
    cur.gotoEndOfParagraph(True)
    doc.getCurrentController().select(cur)

    content = '<p data-lo-para="margin-left:2cm; text-align:center; background-color:#ffe4c4; font-size:12pt">New para</p>'
    res = ApplyDocumentContent().execute(
        _tool_ctx(doc, ctx), target="selection", content=content
    )
    assert res.get("status") == "ok", res
    assert res.get("warnings") == [
        {"code": "PARA_PROPERTY_UNSUPPORTED", "message": "data-lo-para property 'font-size' was not applied"}
    ]

    enum = doc.getText().createEnumeration()
    p1 = enum.nextElement()
    p2 = enum.nextElement()
    p3 = enum.nextElement()

    assert p1.getPropertyValue("ParaLeftMargin") == 0
    assert p1.getPropertyValue("ParaAdjust") == 5
    assert p1.getPropertyValue("FillStyle").value == "NONE"

    assert p2.getPropertyValue("ParaLeftMargin") == 2000
    assert p2.getPropertyValue("ParaAdjust") == 3
    assert p2.getPropertyValue("FillStyle").value == "SOLID"
    assert p2.getPropertyValue("FillColor") == 0xFFE4C4

    assert p3.getPropertyValue("ParaLeftMargin") == 0
    assert p3.getPropertyValue("ParaAdjust") == 5
    assert p3.getPropertyValue("FillStyle").value == "NONE"


@native_test
@with_native_doc("writer")
def test_write_selection_partial_paragraph_warns_and_skips_style_uno(ctx, doc):
    """Partial selection replace: style skipped and warning returned; neighbor stays Standard."""
    text = doc.getText()
    text.setString("")
    cur = text.createTextCursor()
    cur.gotoStart(False)
    cur.setPropertyValue("ParaStyleName", "Standard")
    text.insertString(cur, "MARKER tail", False)

    # Select only "MARKER"
    cur.gotoStart(False)
    cur.goRight(len("MARKER"), True)
    doc.getCurrentController().select(cur)

    res = ApplyDocumentContent().execute(
        _tool_ctx(doc, ctx), target="selection",
        content='<p data-lo-style="Heading2">New heading</p>')
    assert res.get("status") == "ok", res
    assert "warning" not in res
    assert "warnings" in res
    assert res["warnings"][0]["code"] == "STYLE_IGNORED_PARTIAL_MATCH"
    assert "apply_style(target='search', old_content=...)" in res["warnings"][0]["message"]
    assert "apply_style(target='search', old_content=...)" in res["message"]
    pairs = _para_pairs(doc)
    assert all(s != "Heading 2" for s, _ in pairs), pairs
    assert any("tail" in t for _, t in pairs), pairs


@native_test
@with_native_doc("writer")
def test_write_search_partial_match_skips_data_lo_para_uno(ctx, doc):
    """Partial search match skips data-lo-para, warns all names, and leaves margin 0."""
    text = doc.getText()
    text.setString("")
    cur = text.createTextCursor()
    cur.gotoStart(False)
    cur.setPropertyValue("ParaStyleName", "Standard")
    text.insertString(cur, "Replace me tail", False)

    content = '<p data-lo-para="margin-left:2cm; text-align:center; background-color:#ffe4c4">New para</p>'
    res = ApplyDocumentContent().execute(
        _tool_ctx(doc, ctx), target="search", old_content="Replace me", content=content
    )
    assert res.get("status") == "ok", res
    assert "warnings" in res
    codes = [w["code"] for w in res["warnings"]]
    assert "STYLE_IGNORED_PARTIAL_MATCH" not in codes
    assert res["warnings"] == [
        {"code": "PARA_PROPERTY_UNSUPPORTED", "message": "data-lo-para property 'margin-left' was not applied"},
        {"code": "PARA_PROPERTY_UNSUPPORTED", "message": "data-lo-para property 'text-align' was not applied"},
        {"code": "PARA_PROPERTY_UNSUPPORTED", "message": "data-lo-para property 'background-color' was not applied"},
    ]

    enum = doc.getText().createEnumeration()
    p = enum.nextElement()
    assert p.getPropertyValue("ParaLeftMargin") == 0


@native_test
@with_native_doc("writer")
def test_write_search_whole_paragraph_recording_warns_uno(ctx, doc):
    """Whole-paragraph search replace while RecordChanges is True skips style and warns.

    Applying paragraph styles on a paragraph containing tracked deletions can turn
    the deletion into a format change or corrupt redlines. While recording, style
    must be skipped, a warning emitted, and accepting changes leaves only the new text.
    """
    text = doc.getText()
    text.setString("")
    cur = text.createTextCursor()
    cur.gotoStart(False)
    cur.setPropertyValue("ParaStyleName", "Standard")
    text.insertString(cur, "Keep before", False)
    text.insertControlCharacter(cur, 0, False)
    cur.setPropertyValue("ParaStyleName", "Standard")
    text.insertString(cur, "Replace me", False)
    text.insertControlCharacter(cur, 0, False)
    cur.setPropertyValue("ParaStyleName", "Standard")
    text.insertString(cur, "Keep after", False)

    content = (
        '<p data-lo-style="Heading2" '
        'data-lo-para="margin-left:2cm; text-align:center; background-color:#ffe4c4">New heading</p>'
    )
    doc.setPropertyValue("RecordChanges", True)
    try:
        res = ApplyDocumentContent().execute(
            _tool_ctx(doc, ctx), target="search", old_content="Replace me",
            content=content)
        assert res.get("status") == "ok", res
        assert "warning" not in res
        assert "warnings" in res
        assert res["warnings"][0]["code"] == "STYLE_IGNORED_RECORDING"
        assert "tracked changes are recording" in res["warnings"][0]["message"]
        assert "tracked changes are recording" in res["message"]
        para_warns = [w for w in res["warnings"] if w["code"] == "PARA_PROPERTY_UNSUPPORTED"]
        assert para_warns == [
            {"code": "PARA_PROPERTY_UNSUPPORTED", "message": "data-lo-para property 'margin-left' was not applied"},
            {"code": "PARA_PROPERTY_UNSUPPORTED", "message": "data-lo-para property 'text-align' was not applied"},
            {"code": "PARA_PROPERTY_UNSUPPORTED", "message": "data-lo-para property 'background-color' was not applied"},
        ]
    finally:
        doc.setPropertyValue("RecordChanges", False)

    _accept_all(doc, ctx)

    pairs = _para_pairs(doc)
    before = next(((s, t) for s, t in pairs if "Keep before" in t), None)
    new_head = next(((s, t) for s, t in pairs if "New heading" in t), None)
    after = next(((s, t) for s, t in pairs if "Keep after" in t), None)
    assert before is not None and before[0] == "Standard", pairs
    assert new_head is not None and new_head[0] == "Standard", pairs
    assert after is not None and after[0] == "Standard", pairs
    assert "Replace me" not in text.getString(), text.getString()
    assert "New heading" in text.getString(), text.getString()
    enum = doc.getText().createEnumeration()
    while enum.hasMoreElements():
        p = enum.nextElement()
        if hasattr(p, "supportsService") and p.supportsService("com.sun.star.text.Paragraph"):
            if "New heading" in p.getString():
                assert p.getPropertyValue("ParaLeftMargin") == 0


@native_test
@with_native_doc("writer")
def test_write_search_all_matches_mixed_whole_and_partial_uno(ctx, doc):
    """all_matches with one whole-paragraph and one partial match applies style only to whole."""
    text = doc.getText()
    text.setString("")
    cur = text.createTextCursor()
    cur.gotoStart(False)
    cur.setPropertyValue("ParaStyleName", "Standard")
    text.insertString(cur, "Keep before", False)
    text.insertControlCharacter(cur, 0, False)
    cur.setPropertyValue("ParaStyleName", "Standard")
    text.insertString(cur, "Target", False)
    text.insertControlCharacter(cur, 0, False)
    cur.setPropertyValue("ParaStyleName", "Standard")
    text.insertString(cur, "Target tail", False)
    text.insertControlCharacter(cur, 0, False)
    cur.setPropertyValue("ParaStyleName", "Standard")
    text.insertString(cur, "Keep after", False)

    res = ApplyDocumentContent().execute(
        _tool_ctx(doc, ctx), target="search", old_content="Target", all_matches=True,
        content='<p data-lo-style="Heading2">Replacement</p>')
    assert res.get("status") == "ok", res
    assert res.get("replaced_count") == 2, res
    assert "warning" not in res
    assert "warnings" in res
    assert res["warnings"][0]["code"] == "STYLE_IGNORED_PARTIAL_MATCH"
    assert "1 of 2 matches" in res["warnings"][0]["message"]
    assert "1 of 2 matches" in res["message"]

    pairs = _para_pairs(doc)
    before = next(((s, t) for s, t in pairs if "Keep before" in t), None)
    after = next(((s, t) for s, t in pairs if "Keep after" in t), None)
    tail_para = next(((s, t) for s, t in pairs if "tail" in t), None)

    assert before is not None and before[0] == "Standard", pairs
    assert after is not None and after[0] == "Standard", pairs
    # Neighbor text in the partial match paragraph stays Standard (not Heading 2)
    assert tail_para is not None and tail_para[0] == "Standard", pairs
    # Whole-paragraph match was styled Heading 2
    assert any(s == "Heading 2" and "Replacement" in t for s, t in pairs), pairs
    # Exactly one paragraph got Heading 2 (the whole-paragraph match, not the partial one)
    assert sum(1 for s, _ in pairs if s == "Heading 2") == 1, pairs
    assert any("tail" in t for _, t in pairs), pairs


@native_test
@with_native_doc("writer")
def test_partial_match_suggested_call_replay_uno(ctx, doc):
    """Replaying suggested_call from partial match restyles the host paragraph while neighbors keep styles."""
    from plugin.writer.styles import ApplyStyle

    text = doc.getText()
    text.setString("")
    cur = text.createTextCursor()
    cur.gotoStart(False)
    cur.setPropertyValue("ParaStyleName", "Standard")
    text.insertString(cur, "Keep before", False)
    text.insertControlCharacter(cur, 0, False)
    cur.setPropertyValue("ParaStyleName", "Standard")
    text.insertString(cur, "Prefix TARGET Suffix", False)
    text.insertControlCharacter(cur, 0, False)
    cur.setPropertyValue("ParaStyleName", "Standard")
    text.insertString(cur, "Keep after", False)

    res = ApplyDocumentContent().execute(
        _tool_ctx(doc, ctx),
        target="search",
        old_content="TARGET",
        content='<p data-lo-style="Heading2">Replacement</p>',
    )
    assert res.get("status") == "ok", res
    assert "warnings" in res
    assert "warning" not in res
    diag = res["warnings"][0]
    assert diag["code"] == "STYLE_IGNORED_PARTIAL_MATCH"
    assert diag["matches_skipped"] == 1
    assert diag["matches_total"] == 1
    assert "suggested_call" in diag
    suggested = diag["suggested_call"]
    assert suggested["tool"] == "apply_style"
    assert suggested["arguments"]["target"] == "search"
    assert suggested["arguments"]["old_content"] == "Replacement"
    assert suggested["arguments"]["style"] == "Heading 2"
    assert suggested["arguments"]["case_sensitive"] is True
    from tests.framework.suggested_call_access import assert_valid_suggested_call
    assert_valid_suggested_call(suggested)

    # Replay the suggested call against LibreOffice
    style_res = ApplyStyle().execute(_tool_ctx(doc, ctx), **suggested["arguments"])
    assert style_res.get("status") == "ok", style_res

    # Host paragraph is restyled to Heading 2, while neighbouring paragraphs remain Standard
    pairs = _para_pairs(doc)
    before = next(((s, t) for s, t in pairs if "Keep before" in t), None)
    host = next(((s, t) for s, t in pairs if "Replacement" in t), None)
    after = next(((s, t) for s, t in pairs if "Keep after" in t), None)
    assert before is not None and before[0] == "Standard", pairs
    assert host is not None and host[0] == "Heading 2", pairs
    assert after is not None and after[0] == "Standard", pairs
