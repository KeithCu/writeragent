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
# See docs/html_style_model_plan.md.
import uno  # noqa: F401

from plugin.testing_runner import native_test, setup, teardown
from plugin.tests.testing_utils import TestingFactory
from plugin.writer.content import ApplyDocumentContent
import plugin.writer.format as fmt

_doc = None
_ctx = None


@setup
def my_setup(ctx):
    global _doc, _ctx
    _ctx = ctx
    _doc = TestingFactory.create_native_doc(ctx, doc_type="writer", hidden=True)


@teardown
def my_teardown(ctx):
    global _doc
    if _doc:
        try:
            _doc.close(True)
        except Exception:
            pass
    _doc = None


def _tool_ctx():
    return TestingFactory.create_context(doc=_doc, ctx=_ctx, env="native")


def _para_style_names():
    text = _doc.getText()
    out = []
    e = text.createEnumeration()
    while e.hasMoreElements():
        el = e.nextElement()
        if hasattr(el, "supportsService") and el.supportsService("com.sun.star.text.Paragraph"):
            out.append(el.getPropertyValue("ParaStyleName"))
    return out


# --- Read path: named styles -> compact data-lo-style tokens ---------------

@native_test
def test_read_named_styles_emit_compact_tokens_uno():
    doc = _doc
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

    content = fmt.document_to_content(doc, _ctx, None, scope="full")

    # Named styles surfaced as COMPACT (space-free) tokens.
    assert 'data-lo-style="Heading1"' in content, content
    assert 'data-lo-style="Caption"' in content, content
    # Direct char override inlined; no synthetic classes leak.
    assert "ff0000" in content.lower(), content
    assert "text-T" not in content, content
    assert "paragraph-" not in content, content


@native_test
def test_read_order_survives_table_uno():
    """A table between two paragraphs must not desync style assignment, and cells must not leak."""
    doc = _doc
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

    content = fmt.document_to_content(doc, _ctx, None, scope="full")

    assert 'data-lo-style="Heading2"' in content, content
    assert 'data-lo-style="Caption"' in content, content
    i_before = content.index('data-lo-style="Heading2"')
    i_table = content.lower().index("<table")
    i_after = content.index('data-lo-style="Caption"')
    assert i_before < i_table < i_after, "table desynced data-lo-style ordering: %s" % content
    assert "paragraph-" not in content, content


# --- Write path: apply_document_content honors compact data-lo-style -------

@native_test
def test_write_compact_token_applies_uno():
    """Compact tokens drive the paragraph style; 'Heading2' resolves to UNO 'Heading 2'."""
    res = ApplyDocumentContent().execute(
        _tool_ctx(), target="full_document",
        content='<p data-lo-style="Caption">cap</p>\n<p data-lo-style="Heading2">head</p>')
    assert res.get("status") == "ok", res
    names = _para_style_names()
    assert "Caption" in names, names
    assert "Heading 2" in names, names


@native_test
def test_write_compact_heading1_resolves_to_spaced_uno():
    res = ApplyDocumentContent().execute(
        _tool_ctx(), target="full_document",
        content='<p data-lo-style="Heading1">title</p>')
    assert res.get("status") == "ok", res
    text = _doc.getText()
    assert text.createTextCursorByRange(text.getStart()).getPropertyValue("ParaStyleName") == "Heading 1", \
        _para_style_names()


@native_test
def test_write_compact_textbody_resolves_to_spaced_uno():
    """Regression: the compact token 'Textbody' (LO name 'Text body', lowercase b) must resolve
    to the real UNO 'Text body' — NOT fall back to Standard. Guards the prompt/code contract."""
    res = ApplyDocumentContent().execute(
        _tool_ctx(), target="full_document",
        content='<p data-lo-style="Textbody">body text</p>')
    assert res.get("status") == "ok", res
    text = _doc.getText()
    assert text.createTextCursorByRange(text.getStart()).getPropertyValue("ParaStyleName") == "Text body", \
        _para_style_names()


@native_test
def test_write_spaced_form_still_works_uno():
    """Back-compat: an agent that passes the spaced UNO name still resolves correctly."""
    res = ApplyDocumentContent().execute(
        _tool_ctx(), target="full_document",
        content='<p data-lo-style="Heading 1">title</p>')
    assert res.get("status") == "ok", res
    text = _doc.getText()
    assert text.createTextCursorByRange(text.getStart()).getPropertyValue("ParaStyleName") == "Heading 1", \
        _para_style_names()


@native_test
def test_write_preserves_inline_override_uno():
    """Applying the named style must not wipe the inline char override the import laid down."""
    res = ApplyDocumentContent().execute(
        _tool_ctx(), target="full_document",
        content='<p data-lo-style="Heading1">title <span style="color: #ff0000">red</span></p>')
    assert res.get("status") == "ok", res
    text = _doc.getText()
    assert text.createTextCursorByRange(text.getStart()).getPropertyValue("ParaStyleName") == "Heading 1", \
        _para_style_names()
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
def test_write_unknown_token_falls_back_to_standard_uno():
    res = ApplyDocumentContent().execute(
        _tool_ctx(), target="full_document",
        content='<p data-lo-style="NoSuchStyleXYZ">x</p>')
    assert res.get("status") == "ok", res
    assert "Standard" in _para_style_names(), _para_style_names()


@native_test
def test_read_write_round_trip_uno():
    """Read a styled doc -> semantic content (compact tokens) -> write it back -> styles preserved."""
    text = _doc.getText()
    text.setString("")
    cur = text.createTextCursor()
    cur.gotoStart(False)
    cur.setPropertyValue("ParaStyleName", "Heading 1")
    text.insertString(cur, "Title", False)
    text.insertControlCharacter(cur, 0, False)  # PARAGRAPH_BREAK
    cur.setPropertyValue("ParaStyleName", "Caption")
    text.insertString(cur, "Cap", False)

    content = fmt.document_to_content(_doc, _ctx, None, scope="full")
    assert 'data-lo-style="Heading1"' in content, content  # compact token round-trips
    res = ApplyDocumentContent().execute(_tool_ctx(), target="full_document", content=content)
    assert res.get("status") == "ok", res
    names = _para_style_names()
    assert "Heading 1" in names, names
    assert "Caption" in names, names


@native_test
def test_write_div_wrapper_does_not_desync_styles_uno():
    """A <div> is a transparent container: it must not consume a positional style slot, so an
    off-contract wrapper does not shift styles onto the wrong paragraphs."""
    res = ApplyDocumentContent().execute(
        _tool_ctx(), target="full_document",
        content='<div><p data-lo-style="Heading1">first</p><p data-lo-style="Caption">second</p></div>')
    assert res.get("status") == "ok", res
    text = _doc.getText()
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
def test_write_ambiguous_token_falls_back_to_standard_uno():
    """Issue 2: a literal style named 'Heading1' coexisting with built-in 'Heading 1' makes the
    token 'Heading1' ambiguous; the resolver must fail safe to 'Standard', never silently pick
    one of them."""
    fam = _doc.getStyleFamilies().getByName("ParagraphStyles")
    created = False
    if not fam.hasByName("Heading1"):
        st = _doc.createInstance("com.sun.star.style.ParagraphStyle")
        fam.insertByName("Heading1", st)
        created = True
    try:
        res = ApplyDocumentContent().execute(
            _tool_ctx(), target="full_document",
            content='<p data-lo-style="Heading1">x</p>')
        assert res.get("status") == "ok", res
        text = _doc.getText()
        applied = text.createTextCursorByRange(text.getStart()).getPropertyValue("ParaStyleName")
        assert applied == "Standard", "ambiguous token resolved to %r instead of Standard" % applied
    finally:
        # Restore the shared document's style set (setup/teardown are per-module).
        if created and fam.hasByName("Heading1"):
            fam.removeByName("Heading1")


@native_test
def test_write_data_lo_style_with_math_uno():
    """A compact token must apply even when the paragraph contains math (math import branch)."""
    res = ApplyDocumentContent().execute(
        _tool_ctx(), target="full_document",
        content='<p data-lo-style="Heading1">Equation \\(x^2\\) here</p>')
    assert res.get("status") == "ok", res
    text = _doc.getText()
    assert text.createTextCursorByRange(text.getStart()).getPropertyValue("ParaStyleName") == "Heading 1", \
        _para_style_names()


@native_test
def test_write_then_read_recovers_token_via_fodt_uno():
    """THE round-trip proof: writing a paragraph makes the StarWriter import stamp direct char
    props, so on re-export the paragraph is an autostyle whose XHTML CSS matches no named rule
    (the wall). The flat-ODF parent map recovers the real style name on re-read."""
    res = ApplyDocumentContent().execute(
        _tool_ctx(), target="full_document",
        content='<p data-lo-style="Caption">just a caption</p>')
    assert res.get("status") == "ok", res
    content = fmt.document_to_content(_doc, _ctx, None, scope="full")
    # Before the FODT fix this dropped the token; now it round-trips.
    assert 'data-lo-style="Caption"' in content, content
    assert "paragraph-" not in content, content


def _para_pairs():
    text = _doc.getText()
    out = []
    e = text.createEnumeration()
    while e.hasMoreElements():
        el = e.nextElement()
        if hasattr(el, "supportsService") and el.supportsService("com.sun.star.text.Paragraph"):
            out.append((el.getPropertyValue("ParaStyleName"), el.getString()))
    return out


@native_test
def test_write_end_does_not_restyle_existing_text_uno():
    """data-lo-style is applied only on full_document. For target=end the first block merges into
    the existing paragraph, so we DON'T apply the named style (it would restyle the existing
    text). The pre-existing paragraph must stay Standard; the content is still inserted."""
    text = _doc.getText()
    text.setString("Existing line")
    _baseline = text.createTextCursor()
    _baseline.gotoStart(False)
    _baseline.gotoEnd(True)
    _baseline.setPropertyValue("ParaStyleName", "Standard")  # shared _doc: clear prior-test style
    res = ApplyDocumentContent().execute(
        _tool_ctx(), target="end",
        content=['<p data-lo-style="Caption">CAPX para</p>', '<p data-lo-style="Heading2">HEADX para</p>'])
    assert res.get("status") == "ok", res
    pairs = _para_pairs()
    # The pre-existing text must NOT be restyled to Caption (no corruption).
    existing = next(s for s, t in pairs if t.startswith("Existing line"))
    assert existing == "Standard", pairs
    # The content was still inserted.
    assert any("CAPX" in t for _, t in pairs), pairs
    assert any("HEADX" in t for _, t in pairs), pairs


@native_test
def test_write_search_does_not_restyle_surrounding_text_uno():
    """target=search: a replace splits the matched paragraph, so applying data-lo-style would
    restyle the surrounding text. We don't apply on search; the surrounding text stays Standard."""
    text = _doc.getText()
    text.setString("MARKER tail")
    _baseline = text.createTextCursor()
    _baseline.gotoStart(False)
    _baseline.gotoEnd(True)
    _baseline.setPropertyValue("ParaStyleName", "Standard")  # shared _doc: clear prior-test style
    res = ApplyDocumentContent().execute(
        _tool_ctx(), target="search", old_content="MARKER",
        content=['<p data-lo-style="Caption">CAPX</p>', '<p data-lo-style="Heading2">HEADX</p>'])
    assert res.get("status") == "ok", res
    pairs = _para_pairs()
    # Nothing got restyled to Caption/Heading 2 (styles are not applied on search).
    assert all(s not in ("Caption", "Heading 2") for s, _ in pairs), pairs
    # The content was still inserted.
    assert any("CAPX" in t for _, t in pairs), pairs
