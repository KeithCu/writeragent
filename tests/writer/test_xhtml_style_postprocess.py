# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Pure pytest unit tests (no LibreOffice) for the XHTML style post-process pipeline.
# Fixtures mirror the real "XHTML Writer File" output captured from LibreOffice.
from plugin.writer.xhtml_style_postprocess import (
    compact_lo_style_name,
    decode_lo_css_class_suffix,
    extract_autostyle_parents_from_fodt,
    parse_style_block,
    xhtml_to_semantic_html,
)

# Faithful slice of real LO XHTML export (see docs/html_style_model_plan.md, Phase 0).
# P1 = Standard + a bold child span; P2 = Standard + whole-paragraph center+red overrides.
REFERENCE_XHTML = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" lang="en-US">
<head>
<style>
    table { border-collapse:collapse; }
    h1, h2, h3, h4, h5, h6 { clear:both;}
    .paragraph-Caption{ font-size:12pt; font-family:'Liberation Serif'; writing-mode:horizontal-tb; direction:ltr;margin-top:0.0835in; margin-bottom:0.0835in; font-style:italic; }
    .paragraph-Heading_20_1{ font-size:18pt; margin-bottom:0.0835in; margin-top:0.1665in; font-family:'Liberation Sans'; writing-mode:horizontal-tb; direction:ltr;font-weight:bold; }
    .paragraph-P1{ font-size:12pt; font-family:'Liberation Serif'; writing-mode:horizontal-tb; direction:ltr;font-weight:normal; }
    .paragraph-P2{ font-size:12pt; font-family:'Liberation Serif'; writing-mode:horizontal-tb; direction:ltr;text-align:center ! important; color:#ff0000; }
    .paragraph-Standard{ font-size:12pt; font-family:'Liberation Serif'; writing-mode:horizontal-tb; direction:ltr;}
    .paragraph-Table_20_Contents{ font-size:12pt; font-family:'Liberation Serif'; writing-mode:horizontal-tb; direction:ltr;}
    .paragraph-Text_20_body{ font-size:12pt; font-family:'Liberation Serif'; writing-mode:horizontal-tb; direction:ltr;margin-top:0in; margin-bottom:0.0972in; line-height:115%; }
    .text-T1{ font-weight:bold; }
</style>
</head>
<body dir="ltr" style="max-width:8.2681in;">
<h1 class="paragraph-Heading_20_1"><a id="a__Big_Title"><span/></a>Big Title</h1>
<p class="paragraph-Caption">Figure 1 caption</p>
<p class="paragraph-Text_20_body">A normal text body paragraph.</p>
<p class="paragraph-P1">normal <span class="text-T1">BOLD</span> end</p>
<p class="paragraph-P2">centered red whole paragraph</p>
<p class="paragraph-Standard">plain standard tail</p>
<table border="0" class="table-Table1"><tr><td class="cell-Table1_A1">
<p class="paragraph-Table_20_Contents">MinerU</p>
</td><td class="cell-Table1_B1"><p class="paragraph-Table_20_Contents"> </p></td></tr></table>
<p class="paragraph-Standard"> </p></body>
</html>"""


# --- small helpers ---------------------------------------------------------

def test_decode_lo_css_class_suffix():
    assert decode_lo_css_class_suffix("Heading_20_1") == "Heading 1"
    assert decode_lo_css_class_suffix("Text_20_body") == "Text body"
    assert decode_lo_css_class_suffix("Table_20_Contents") == "Table Contents"
    assert decode_lo_css_class_suffix("Caption") == "Caption"
    assert decode_lo_css_class_suffix("Standard") == "Standard"


def test_compact_lo_style_name():
    assert compact_lo_style_name("Heading 1") == "Heading1"
    assert compact_lo_style_name("Text body") == "Textbody"
    assert compact_lo_style_name("Standard") == "Standard"


def test_parse_style_block_extracts_class_declarations():
    raw, norm = parse_style_block(REFERENCE_XHTML)
    assert raw["text-T1"] == "font-weight:bold"
    assert "font-style:italic" in raw["paragraph-Caption"]
    # Fingerprint is order-independent; P1 (Standard + font-weight:normal) != Standard.
    assert norm["paragraph-P1"] != norm["paragraph-Standard"]


# --- full pipeline ---------------------------------------------------------

def test_named_styles_become_compact_data_lo_style():
    out = xhtml_to_semantic_html(REFERENCE_XHTML)
    assert 'data-lo-style="Heading1"' in out, out
    assert 'data-lo-style="Caption"' in out, out
    assert 'data-lo-style="Textbody"' in out, out
    assert 'data-lo-style="Standard"' in out, out


def test_char_override_inlined_no_synthetic_classes_leak():
    out = xhtml_to_semantic_html(REFERENCE_XHTML)
    assert "font-weight:bold" in out, out
    assert "paragraph-" not in out, out
    assert "text-T" not in out, out


def test_autostyle_paragraphs_omit_data_lo_style():
    """P1/P2 (Standard + direct overrides) don't fingerprint-match a named rule -> omitted.

    The write path treats a missing data-lo-style as the default body style, so this still
    round-trips for Standard. The char override (bold) survives via the inline span."""
    out = xhtml_to_semantic_html(REFERENCE_XHTML)
    # The "normal BOLD end" paragraph (P1) must carry no data-lo-style but keep the bold span.
    line = next(ln for ln in out.splitlines() if "BOLD" in ln)
    assert "data-lo-style" not in line, line
    assert "font-weight:bold" in line, line


def test_trailing_empty_paragraph_dropped():
    import re
    out = xhtml_to_semantic_html(REFERENCE_XHTML)
    # The trailing ghost <p ...> </p> LO appends is gone; the last real block is the table.
    assert out.rstrip().endswith("</table>"), out
    assert re.search(r"<p\b[^>]*>(?:\s|&nbsp;)*</p>\s*$", out) is None, out
    # The empty cell paragraph INSIDE the table is structural and legitimately kept.
    assert "MinerU" in out, out


def test_table_preserved_cell_classes_stripped():
    out = xhtml_to_semantic_html(REFERENCE_XHTML)
    assert "<table" in out, out
    assert "MinerU" in out, out
    # Order: heading before the table, the table, the standard tail before it.
    i_head = out.index('data-lo-style="Heading1"')
    i_table = out.lower().index("<table")
    assert i_head < i_table, out
    # No paragraph-* leaks from cells either.
    assert "paragraph-" not in out, out


# --- autostyle fingerprint resolution (positive + ambiguous) ---------------

_FINGERPRINT_XHTML = """<html><head><style>
.paragraph-Quotations{ margin-left:0.5in; font-style:italic; }
.paragraph-P7{ font-style:italic; margin-left:0.5in; }
</style></head><body>
<p class="paragraph-P7">quoted clause</p>
</body></html>"""


def test_autostyle_uniquely_matching_named_rule_resolves():
    """P7's CSS equals Quotations' (order-independent) -> resolve to that named token."""
    out = xhtml_to_semantic_html(_FINGERPRINT_XHTML)
    assert 'data-lo-style="Quotations"' in out, out


_AMBIGUOUS_XHTML = """<html><head><style>
.paragraph-Foo{ font-style:italic; }
.paragraph-Bar{ font-style:italic; }
.paragraph-P9{ font-style:italic; }
</style></head><body>
<p class="paragraph-P9">ambiguous</p>
</body></html>"""


def test_autostyle_ambiguous_match_omitted():
    """Two named rules share the autostyle's CSS -> omit (do not guess)."""
    out = xhtml_to_semantic_html(_AMBIGUOUS_XHTML)
    assert "data-lo-style" not in out, out
    assert "paragraph-" not in out, out


# --- Issue 1 characterization: whole-paragraph direct overrides are dropped ---

def test_whole_paragraph_direct_override_is_dropped():
    """KNOWN LIMITATION: a Standard paragraph with whole-paragraph center+colour (autostyle P2,
    a superset of the body style) is emitted WITHOUT a token and WITHOUT the override CSS.
    Alignment/whole-paragraph colour do not round-trip in v1 (documented for the maintainer)."""
    out = xhtml_to_semantic_html(REFERENCE_XHTML)
    line = next(ln for ln in out.splitlines() if "centered red whole paragraph" in ln)
    assert "data-lo-style" not in line, line          # base style not recoverable -> omitted
    assert "text-align" not in line and "center" not in line.replace("centered", ""), line
    assert "ff0000" not in line.lower() and "color" not in line, line  # the override is dropped


# --- Issue 2: token collision (two styles compacting to the same token) -------

_COLLISION_XHTML = """<html><head><style>
.paragraph-Heading_20_1{ font-size:18pt; font-weight:bold; }
.paragraph-Heading1{ font-size:14pt; color:#0000ff; }
</style></head><body>
<p class="paragraph-Heading_20_1">spaced heading</p>
<p class="paragraph-Heading1">literal heading</p>
</body></html>"""


def test_div_is_a_transparent_container_on_read():
    """`<div>` is not a styleable block (symmetric with the write path): a wrapper passes
    through and the inner <p> still gets its token. Guards the read/write div symmetry."""
    from plugin.writer.xhtml_style_postprocess import BLOCK_TAGS
    assert "div" not in BLOCK_TAGS
    xhtml = ('<html><head><style>.paragraph-Caption{ font-style:italic; }</style></head>'
             '<body><div><p class="paragraph-Caption">x</p></div></body></html>')
    out = xhtml_to_semantic_html(xhtml)
    assert '<div>' in out, out                       # wrapper preserved verbatim
    assert 'data-lo-style="Caption"' in out, out      # token landed on the <p>, not the <div>
    assert "data-lo-style" not in out.split("<p", 1)[0], out  # nothing on the <div> itself
    assert "paragraph-" not in out, out


def test_colliding_tokens_are_omitted_on_read():
    """`Heading 1` and a literal `Heading1` both compact to `Heading1` -> ambiguous -> omit both
    (the write path could not tell them apart). Issue 2 read side."""
    out = xhtml_to_semantic_html(_COLLISION_XHTML)
    assert "data-lo-style" not in out, out
    assert "paragraph-" not in out, out
    assert "spaced heading" in out and "literal heading" in out, out


# --- FODT autostyle-parent recovery (fixes the write->read round-trip wall) -----

_FODT = (
    "<office:automatic-styles>"
    '<style:style style:name="P1" style:family="paragraph" style:parent-style-name="Caption"/>'
    '<style:style style:name="P2" style:family="paragraph" style:parent-style-name="Text_20_body"/>'
    '<style:style style:name="T1" style:family="text"/>'
    "</office:automatic-styles>"
)


def test_extract_autostyle_parents_from_fodt():
    assert extract_autostyle_parents_from_fodt(_FODT) == {"P1": "Caption", "P2": "Text_20_body"}


# XHTML where P1 (a Caption-derived autostyle, as produced after a StarWriter import) carries
# extra direct char props, so its CSS matches NO named rule -> fingerprint fails (the wall).
_AUTOSTYLE_NO_NAMED_MATCH_XHTML = """<html><head><style>
.paragraph-P1{ font-size:12pt; background-color:transparent; text-decoration:none ! important; }
.paragraph-Standard{ font-size:12pt; }
</style></head><body>
<p class="paragraph-P1">just a caption</p>
</body></html>"""


def test_fodt_parent_recovers_name_when_fingerprint_fails():
    # Without the FODT map: the autostyle resolves to nothing (the round-trip wall).
    assert "data-lo-style" not in xhtml_to_semantic_html(_AUTOSTYLE_NO_NAMED_MATCH_XHTML)
    # With the FODT parent map: the real name is recovered as a compact token.
    out = xhtml_to_semantic_html(_AUTOSTYLE_NO_NAMED_MATCH_XHTML, {"P1": "Caption"})
    assert 'data-lo-style="Caption"' in out, out
    assert "paragraph-P1" not in out, out


def test_fodt_parent_encoded_name_is_decoded_and_compacted():
    out = xhtml_to_semantic_html(_AUTOSTYLE_NO_NAMED_MATCH_XHTML, {"P1": "Text_20_body"})
    assert 'data-lo-style="Textbody"' in out, out  # decode _20_ -> space, then compact
