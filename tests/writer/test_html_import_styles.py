# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Tests for plugin.writer.html_import_styles."""

from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import pytest

from plugin.writer import html_import_styles


def test_written_heading_texts_extraction() -> None:
    """Extract untruncated, whitespace-collapsed headings with entity decoding."""
    long_title = "A" * 130
    html = f"""
    <h2>First &amp; Second</h2>
    <p data-lo-style="Heading2">Second   Paragraph   Heading</p>
    <h1>{long_title}</h1>
    <p>Body paragraph not a heading.</p>
    """
    headings = html_import_styles.written_heading_texts(html)
    assert headings == [
        "First & Second",
        "Second Paragraph Heading",
        long_title,
    ]


def test_written_heading_texts_empty_or_non_markup() -> None:
    """Plain text or empty content returns no written headings."""
    assert html_import_styles.written_heading_texts("") == []
    assert html_import_styles.written_heading_texts("plain text without tags") == []


def test_resolve_heading_links_exact_match_and_quote_preservation() -> None:
    """Exact heading matches rewrite to |outline with the original quote character and HTML escape."""
    content = '<p><a href="#Section &amp; Notes">Link 1</a> <a href=\'#Section &amp; Notes\'>Link 2</a></p>'
    headings = ["Section & Notes"]
    rewritten, diags = html_import_styles.resolve_heading_links(
        content,
        headings,
        is_bookmark=lambda _name: False,
    )
    assert diags == []
    assert rewritten == (
        '<p><a href="#Section &amp; Notes|outline">Link 1</a> '
        '<a href=\'#Section &amp; Notes|outline\'>Link 2</a></p>'
    )


def test_resolve_heading_links_leaves_suffixed_and_empty_untouched() -> None:
    """Fragments that already have a pipe suffix or are empty are left untouched without warnings."""
    content = '<p><a href="#">Empty</a> <a href="#Heading|outline">Outline</a> <a href="#Tbl|table">Table</a></p>'
    rewritten, diags = html_import_styles.resolve_heading_links(
        content,
        ["Heading", "Tbl"],
        is_bookmark=lambda _name: False,
    )
    assert diags == []
    assert rewritten == content


def test_resolve_heading_links_id_and_bookmark_exemptions() -> None:
    """Matching id/name attribute in written content or an existing bookmark prevents rewrite and warning."""
    content = (
        '<h2 id="existing-id">Title</h2>'
        '<a name="anchor-name"></a>'
        '<p><a href="#existing-id">ID link</a>'
        '<a href="#anchor-name">Name link</a>'
        '<a href="#existing-bookmark">Bookmark link</a></p>'
    )
    # Even if "existing-id" were also in headings, id takes precedence and stays unmodified
    headings = ["existing-id"]
    rewritten, diags = html_import_styles.resolve_heading_links(
        content,
        headings,
        is_bookmark=lambda name: name == "existing-bookmark",
    )
    assert diags == []
    assert rewritten == content


def test_resolve_heading_links_percent_and_entity_decoding() -> None:
    """Percent-encoded and entity-encoded href fragments match the decoded heading text."""
    content = '<p><a href="#Getting%20Started">Link 1</a> <a href="#Intro%20%26%20More">Link 2</a></p>'
    headings = ["Getting Started", "Intro & More"]
    rewritten, diags = html_import_styles.resolve_heading_links(
        content,
        headings,
        is_bookmark=lambda _name: False,
    )
    assert diags == []
    assert rewritten == (
        '<p><a href="#Getting Started|outline">Link 1</a> '
        '<a href="#Intro &amp; More|outline">Link 2</a></p>'
    )


def test_resolve_heading_links_matches_document_headings() -> None:
    """Headings existing in the document are resolved when not present in written HTML."""
    content = '<p><a href="#Existing Document Heading">Link</a></p>'
    doc_headings = ["Existing Document Heading"]
    rewritten, diags = html_import_styles.resolve_heading_links(
        content,
        doc_headings,
        is_bookmark=lambda _name: False,
    )
    assert diags == []
    assert rewritten == '<p><a href="#Existing Document Heading|outline">Link</a></p>'


def test_resolve_heading_links_case_mismatch_and_deduplication() -> None:
    """Case mismatch fails to match and produces a deduplicated LINK_TARGET_NOT_FOUND diagnostic."""
    content = '<p><a href="#overview">Link 1</a> and <a href="#overview">Link 2</a></p>'
    headings = ["Overview"]
    rewritten, diags = html_import_styles.resolve_heading_links(
        content,
        headings,
        is_bookmark=lambda _name: False,
    )
    assert rewritten == content
    assert len(diags) == 1
    assert diags[0]["code"] == html_import_styles.LINK_TARGET_NOT_FOUND
    assert diags[0]["link_target"] == "#overview"


def test_resolve_heading_links_entity_in_id_exempted() -> None:
    """id attribute containing entities matches decoded fragment without warning."""
    content = '<h2 id="a&amp;b">Title</h2><p><a href="#a&amp;b">Link</a></p>'
    rewritten, diags = html_import_styles.resolve_heading_links(
        content,
        [],
        is_bookmark=lambda _name: False,
    )
    assert diags == []
    assert rewritten == content


def test_resolve_heading_links_data_id_not_matched_as_id() -> None:
    """data-id or data-name attributes are not mistaken for target id/name."""
    content = '<p data-id="target" data-name="target"><a href="#target">Link</a></p>'
    rewritten, diags = html_import_styles.resolve_heading_links(
        content,
        [],
        is_bookmark=lambda _name: False,
    )
    assert rewritten == content
    assert len(diags) == 1
    assert diags[0]["code"] == html_import_styles.LINK_TARGET_NOT_FOUND
    assert diags[0]["link_target"] == "#target"


def test_resolve_heading_links_deduplicates_encoded_and_plain_fragment() -> None:
    """Distinct encoded representations of the same target fragment deduplicate to one warning."""
    content = '<p><a href="#My%20Heading">Link 1</a> and <a href="#My Heading">Link 2</a></p>'
    rewritten, diags = html_import_styles.resolve_heading_links(
        content,
        [],
        is_bookmark=lambda _name: False,
    )
    assert rewritten == content
    assert len(diags) == 1
    assert diags[0]["link_target"] == "#My Heading"


def test_append_warnings() -> None:
    """Diagnostics are attached to successful results and ignored on errors."""
    diags = [
        {
            "code": html_import_styles.LINK_TARGET_NOT_FOUND,
            "link_target": "#nowhere",
            "message": "error msg",
        }
    ]
    err_res = {"status": "error", "message": "Failed"}
    assert html_import_styles.append_warnings(err_res, diags) == err_res
    assert "warnings" not in err_res

    ok_res = {"status": "ok", "message": "Done"}
    updated = html_import_styles.append_warnings(ok_res, diags)
    assert updated.get("warnings") == diags

    # Extends existing warnings list
    existing_res = {
        "status": "ok",
        "warnings": [{"code": "EXISTING", "message": "Prior warning"}],
    }
    updated_existing = html_import_styles.append_warnings(existing_res, diags)
    assert len(updated_existing["warnings"]) == 2
    assert updated_existing["warnings"][0]["code"] == "EXISTING"
    assert updated_existing["warnings"][1]["code"] == html_import_styles.LINK_TARGET_NOT_FOUND


def test_attach_math_objects_all_ok_attaches_list() -> None:
    ok_res = {"status": "ok", "message": "Done"}
    entries = [
        {"kind": "tex", "display_block": False, "snippet": "a", "ok": True},
        {"kind": "mml", "display_block": True, "snippet": "<math></math>", "ok": True},
    ]
    res = html_import_styles.attach_write_report(ok_res, html_import_styles.WriteReport(math=entries))
    assert res["status"] == "ok"
    assert res["math_objects"] == entries
    assert "warnings" not in res


def test_attach_math_objects_any_failure_is_math_import_failed_warning() -> None:
    entries = [
        {"kind": "tex", "display_block": False, "snippet": "a", "ok": True},
        {
            "kind": "tex",
            "display_block": True,
            "snippet": r"\sqrt{",
            "ok": False,
            "error": "NoAvailableTokensError",
        },
    ]
    ok_res = {"status": "ok", "message": "Done", "paragraph_count": 2}
    res = html_import_styles.attach_write_report(ok_res, html_import_styles.WriteReport(math=entries))
    assert res["status"] == "ok"
    assert res["paragraph_count"] == 2
    assert res["math_objects"] == entries
    assert "warnings" in res
    assert len(res["warnings"]) == 1
    diag = res["warnings"][0]
    assert diag["code"] == html_import_styles.MATH_IMPORT_FAILED
    assert r"\sqrt{" in diag["message"]
    assert "NoAvailableTokensError" in diag["message"]

    err_res = {"status": "error", "message": "Failed"}
    assert html_import_styles.attach_write_report(err_res, html_import_styles.WriteReport(math=entries)) == err_res


def test_attach_write_report_style_skip_builds_diagnostic_and_message() -> None:
    """A partial-match style skip with a token builds a diagnostic, suggested call, and updates message."""
    from plugin.framework.errors import suggest_call

    cb_calls: list[tuple[str, int]] = []

    def fake_suggest(token: str, total_matches: int):
        cb_calls.append((token, total_matches))
        return suggest_call("apply_style", style=token, all_matches=True)

    report = html_import_styles.WriteReport(
        style_skips=["STYLE_IGNORED_PARTIAL_MATCH"],
        style_tokens=["Heading2"],
    )
    res = {"status": "ok", "message": "Replaced 2 occurrence(s)."}
    out = html_import_styles.attach_write_report(
        res,
        report,
        total_matches=2,
        suggest_style_fix=fake_suggest,
    )
    assert cb_calls == [("Heading2", 2)]
    assert "warnings" in out
    assert len(out["warnings"]) == 1
    diag = out["warnings"][0]
    assert diag["code"] == "STYLE_IGNORED_PARTIAL_MATCH"
    assert diag["matches_skipped"] == 1
    assert diag["matches_total"] == 2
    assert diag["suggested_call"] == suggest_call("apply_style", style="Heading2", all_matches=True)
    assert out["message"] == f"Replaced 2 occurrence(s). {diag['message']}"


def test_attach_write_report_skip_without_style_token_only_explains_para() -> None:
    """When content declares no data-lo-style token, a style skip only explains data-lo-para."""
    cb_called = False

    def fake_suggest(_token: str, _total: int):
        nonlocal cb_called
        cb_called = True
        return None

    report = html_import_styles.WriteReport(
        unapplied_para=["margin-left"],
        style_skips=["STYLE_IGNORED_RECORDING"],
    )
    res = {"status": "ok", "message": "Inserted content."}
    out = html_import_styles.attach_write_report(
        res,
        report,
        total_matches=1,
        suggest_style_fix=fake_suggest,
    )
    assert not cb_called
    assert "warnings" in out
    assert len(out["warnings"]) == 1
    diag = out["warnings"][0]
    assert diag["code"] == "PARA_IGNORED_RECORDING"
    assert out["message"] == "Inserted content."


def test_write_report_extend_preserves_order_and_unions_tokens() -> None:
    """extend concatenates unapplied_para, style_skips, math and unions style_tokens preserving order."""
    r1 = html_import_styles.WriteReport(
        unapplied_para=["color", "margin-top"],
        style_skips=["STYLE_IGNORED_PARTIAL_MATCH"],
        style_tokens=["Heading1", "Heading2"],
        math=[{"snippet": "a", "ok": True}],
    )
    r2 = html_import_styles.WriteReport(
        unapplied_para=["margin-bottom", "color"],
        style_skips=["STYLE_IGNORED_RECORDING"],
        style_tokens=["Heading2", "Heading3", "Heading1"],
        math=[{"snippet": "b", "ok": False}],
    )
    r1.extend(r2)
    assert r1.unapplied_para == ["color", "margin-top", "margin-bottom", "color"]
    assert r1.style_skips == ["STYLE_IGNORED_PARTIAL_MATCH", "STYLE_IGNORED_RECORDING"]
    assert r1.style_tokens == ["Heading1", "Heading2", "Heading3"]
    assert r1.math == [{"snippet": "a", "ok": True}, {"snippet": "b", "ok": False}]


def test_decode_complete_character_references_only_decodes_with_semicolon() -> None:
    decode = html_import_styles.decode_complete_character_references
    assert decode("&sect 2o &amp;lt;") == "&sect 2o &lt;"
    assert decode("&amp; &quot; &#36; &#x24;") == '& " $ $'
    assert decode("&not covered") == "&not covered"


def test_nested_blocks_share_one_style_slot():
    """<blockquote><p> and <li><p> are ONE Writer paragraph: two slots shifted every later style
    ("2. DO DIREITO" lost its heading) and the leftover ones hit old text (relatos #35/#40)."""
    from plugin.writer.html_import_styles import extract_block_lo_styles

    html = ('<h1 data-lo-style="Heading1">T</h1>'
            '<blockquote><p data-lo-style="Quotations">q</p></blockquote>'
            '<ul><li><p data-lo-style="Standard">a</p></li><li><p data-lo-style="Standard">b</p></li></ul>'
            '<h2 data-lo-style="Heading2">D</h2>')
    clean, styles, _paras, _skipped = extract_block_lo_styles(html)
    assert styles == ["Heading1", "Quotations", "Standard", "Standard", "Heading2"]
    assert "data-lo-style" not in clean


def test_second_block_inside_a_block_gets_its_own_slot():
    from plugin.writer.html_import_styles import extract_block_lo_styles

    _clean, styles, _paras, _skipped = extract_block_lo_styles(
        '<li data-lo-style="List"><p>a</p><p data-lo-style="Standard">b</p></li><p data-lo-style="Standard">c</p>')
    assert styles == ["List", "Standard", "Standard"]


def test_block_styles_stop_at_the_end_of_the_import():
    """More styles than imported paragraphs must not style the text after the import (the old
    text a full_document had just deleted: its Delete turned into a Format change)."""

    text = MagicMock()
    walk_cur = MagicMock()
    starts = [0, 1, 2, 3]
    pos = [0]

    def get_start():
        return starts[min(pos[0], len(starts) - 1)]

    def goto_next(expand):
        pos[0] += 1
        return pos[0] < len(starts)

    walk_cur.getStart.side_effect = get_start
    walk_cur.gotoNextParagraph.side_effect = goto_next
    text.createTextCursorByRange.return_value = walk_cur

    end = 2  # the parked cursor sits where paragraph 2 starts
    text.compareRegionStarts.side_effect = lambda a, b: 1 if a < b else (0 if a == b else -1)
    start_cursor = MagicMock()
    with patch("plugin.writer.styles.apply_paragraph_style_preserving_direct_char") as apply:
        html_import_styles.apply_block_lo_styles(
            MagicMock(), text, start_cursor, ["Heading1", "Standard", "Standard", "Heading2"], end=end,
        )
    assert apply.call_count == 2


def test_apply_block_lo_styles_token_cache_reads_element_names_once():
    model = MagicMock()
    fam = MagicMock()
    fam.getElementNames.return_value = ["Heading 1", "Standard"]
    fam.hasByName.return_value = True
    model.getStyleFamilies.return_value.getByName.return_value = fam

    text = MagicMock()
    walk_cur = MagicMock()
    walk_cur.gotoNextParagraph.side_effect = [True, False]
    text.createTextCursorByRange.return_value = walk_cur

    with patch("plugin.writer.styles.apply_paragraph_style_preserving_direct_char") as apply:
        html_import_styles.apply_block_lo_styles(
            model, text, MagicMock(), ["Heading1", "Heading1"],
        )
    fam.getElementNames.assert_called_once()
    assert apply.call_count == 2
    assert apply.call_args_list[0].args[2] == "Heading 1"
    assert apply.call_args_list[1].args[2] == "Heading 1"


def test_apply_block_lo_styles_ambiguous_token_resolves_to_standard():
    model = MagicMock()
    fam = MagicMock()
    # "Heading1" matches both literal "Heading1" and compact form of "Heading 1" -> ambiguous
    fam.getElementNames.return_value = ["Heading1", "Heading 1", "Standard"]
    fam.hasByName.return_value = True
    model.getStyleFamilies.return_value.getByName.return_value = fam

    text = MagicMock()
    walk_cur = MagicMock()
    walk_cur.gotoNextParagraph.return_value = False
    text.createTextCursorByRange.return_value = walk_cur

    with patch("plugin.writer.styles.apply_paragraph_style_preserving_direct_char") as apply:
        html_import_styles.apply_block_lo_styles(
            model, text, MagicMock(), ["Heading1"],
        )
    assert apply.call_args.args[2] == "Standard"


def test_apply_block_lo_styles_unknown_token_resolves_to_standard():
    model = MagicMock()
    fam = MagicMock()
    fam.getElementNames.return_value = ["Standard"]
    fam.hasByName.return_value = False
    model.getStyleFamilies.return_value.getByName.return_value = fam

    text = MagicMock()
    walk_cur = MagicMock()
    walk_cur.getPropertyValue.return_value = None
    walk_cur.gotoNextParagraph.return_value = False
    text.createTextCursorByRange.return_value = walk_cur

    with patch("plugin.writer.styles.apply_paragraph_style_preserving_direct_char") as apply:
        html_import_styles.apply_block_lo_styles(
            model, text, MagicMock(), ["Nope"],
        )
    assert apply.call_args.args[2] == "Standard"


def test_apply_block_lo_styles_skips_when_style_already_matches():
    model = MagicMock()
    fam = MagicMock()
    fam.getElementNames.return_value = ["Heading 1", "Standard"]
    fam.hasByName.return_value = True
    model.getStyleFamilies.return_value.getByName.return_value = fam

    text = MagicMock()
    walk_cur = MagicMock()
    walk_cur.getPropertyValue.return_value = "Heading 1"
    walk_cur.gotoNextParagraph.return_value = False
    text.createTextCursorByRange.return_value = walk_cur

    with patch("plugin.writer.styles.apply_paragraph_style_preserving_direct_char") as apply:
        res = html_import_styles.apply_block_lo_styles(
            model, text, MagicMock(), ["Heading1"],
        )
    apply.assert_not_called()
    assert res == []


@pytest.mark.parametrize(
    "value, value_2",
    [
        pytest.param('<div data-lo-style="Standard">Pelotas.</div><div><p data-lo-style="Standard">Adv</p></div>', "Standard", id="test_styled_div_is_a_slot_and_plain_div_is_not"),
        pytest.param('<p data-lo-style="Standard">a<p data-lo-style="Heading2">b</p>', "Heading2", id="test_omitted_paragraph_end_tag_does_not_nest_the_next_block"),
    ],
)
def test_styled_div_is_a_slot_and_plain_div_is_not(value, value_2):
    from plugin.writer.html_import_styles import extract_block_lo_styles

    _clean, styles, _paras, _skipped = extract_block_lo_styles(
        value)
    assert styles == ["Standard", value_2]


def test_parse_data_lo_para_text_align_center():
    from plugin.writer.html_import_styles import parse_data_lo_para

    props, dropped = parse_data_lo_para("text-align:center")
    assert props == {"text-align": ("ParaAdjust", 3)}
    assert dropped == []


def test_apply_block_lo_styles_returns_failures():
    """apply_block_lo_styles returns property names for dropped, failed, or unreached slots."""
    model = MagicMock()
    model.getStyleFamilies.return_value.getByName.return_value = None

    text = MagicMock()
    walk_cur = MagicMock()
    # Fails on the first paragraph advance so the second slot is unreached
    walk_cur.gotoNextParagraph.return_value = False

    para_cur = MagicMock()
    para_el = MagicMock()
    # Raises on setPropertyValue so the applied property fails
    para_el.setPropertyValue.side_effect = RuntimeError("locked")
    para_cur.createEnumeration.return_value.hasMoreElements.return_value = True
    para_cur.createEnumeration.return_value.nextElement.return_value = para_el
    text.createTextCursorByRange.side_effect = [walk_cur, para_cur]

    failed = html_import_styles.apply_block_lo_styles(
        model,
        text,
        MagicMock(),
        [None, None],
        paras=["margin-left:2cm; font-size:12pt", "text-align:right"],
    )
    # font-size dropped by parser, margin-left failed at runtime, text-align unreached
    assert failed == ["font-size", "margin-left", "text-align"]
    para_el.setPropertyValue.assert_called()


def test_parse_data_lo_para_margins():
    from plugin.writer.html_import_styles import parse_data_lo_para

    props_left, dropped_left = parse_data_lo_para("margin-left:3.25cm")
    assert props_left == {"margin-left": ("ParaLeftMargin", 3250)}
    assert dropped_left == []

    props_right, dropped_right = parse_data_lo_para("margin-right:1in")
    assert props_right == {"margin-right": ("ParaRightMargin", 2540)}
    assert dropped_right == []


def test_parse_data_lo_para_background_color():
    from plugin.writer.html_import_styles import parse_data_lo_para

    props_hex, dropped_hex = parse_data_lo_para("background-color:#ffe4c4")
    assert props_hex == {"background-color": ("ParaBackColor", 0xFFE4C4)}
    assert dropped_hex == []

    props_trans, dropped_trans = parse_data_lo_para("background-color:transparent")
    assert props_trans == {"background-color": ("ParaBackColor", -1)}
    assert dropped_trans == []


def test_parse_data_lo_para_vertical_margins_and_indent():
    from plugin.writer.html_import_styles import parse_data_lo_para

    props, dropped = parse_data_lo_para("margin-top:0.5cm; margin-bottom:12pt; text-indent:-0.5cm")
    assert props == {
        "margin-top": ("ParaTopMargin", 500),
        "margin-bottom": ("ParaBottomMargin", 423),
        "text-indent": ("ParaFirstLineIndent", -500),
    }
    assert dropped == []


class _FakeLineSpacing:
    """Stands in for com.sun.star.style.LineSpacing (pytest has no real UNO structs)."""

    def __init__(self) -> None:
        self.Mode = 0
        self.Height = 0


@pytest.mark.parametrize(
    ("css", "mode", "height"),
    [
        pytest.param("line-height:150%", 0, 150, id="percent_is_proportional"),
        pytest.param("line-height:115.4%", 0, 115, id="percent_rounds"),
        pytest.param("line-height:normal", 0, 100, id="normal_is_single"),
        pytest.param("line-height:0.5cm", 3, 500, id="length_is_fixed"),
    ],
)
def test_parse_data_lo_para_line_height(monkeypatch, css, mode, height):
    import sys

    from plugin.writer.html_import_styles import parse_data_lo_para

    # setitem on __dict__ (not setattr) so undo removes the attribute instead of leaving a
    # materialised MagicMock child; other tests check that createUnoStruct is absent.
    monkeypatch.setitem(sys.modules["uno"].__dict__, "createUnoStruct", lambda _name: _FakeLineSpacing())
    props, dropped = parse_data_lo_para(css)
    assert dropped == []
    uno_prop, spacing = props["line-height"]
    assert uno_prop == "ParaLineSpacing"
    assert (spacing.Mode, spacing.Height) == (mode, height)


@pytest.mark.parametrize("value", ["tall", "0cm", "-1cm", "%", "0%", "0.0%"])
def test_parse_data_lo_para_bad_line_height_is_dropped(value):
    from plugin.writer.html_import_styles import parse_data_lo_para

    props, dropped = parse_data_lo_para("line-height:%s" % value)
    assert props == {}
    assert dropped == ["line-height"]


def test_every_read_para_name_has_a_writer_or_is_listed_read_only():
    """Every CSS name the read side emits in data-lo-para is either written or listed.

    A property added to the read table without a writer would otherwise only show up as
    one more warning; this makes it a test failure that forces a decision.
    """
    from plugin.writer import xhtml_style_postprocess

    read_names = {css for _odf, css in xhtml_style_postprocess._FODT_OVERRIDE_ATTRS}
    writers = set(html_import_styles._para_writers())
    read_only = html_import_styles._READ_ONLY_PARA_NAMES
    assert not writers & read_only
    assert read_names - writers - read_only == set()
    assert writers <= read_names, "a writer for a name reads never emit cannot round-trip"


def test_round_trip_read_fodt_autostyle_value():
    from plugin.writer import html_import_styles
    from plugin.writer.xhtml_style_postprocess import parse_fodt_autostyles

    fodt = """<?xml version="1.0" encoding="UTF-8"?>
<office:document xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0"
 xmlns:fo="urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0">
 <style:style style:name="P2" style:family="paragraph" style:parent-style-name="Standard">
  <style:paragraph-properties fo:margin-left="3.25cm" fo:text-indent="0cm" fo:text-align="start"/>
 </style:style>
</office:document>"""
    emitted = parse_fodt_autostyles(fodt)[1]["P2"]
    assert emitted == "margin-left:3.25cm; text-indent:0cm; text-align:left"

    props, dropped = html_import_styles.parse_data_lo_para(emitted)
    assert props == {
        "margin-left": ("ParaLeftMargin", 3250),
        "text-indent": ("ParaFirstLineIndent", 0),
        "text-align": ("ParaAdjust", 0),
    }
    assert dropped == []

    html = f'<p data-lo-para="{emitted}">Clause text</p>'
    clean, styles, paras, skipped = html_import_styles.extract_block_lo_styles(html)
    assert clean == "<p>Clause text</p>"
    assert paras == [emitted]

    model = MagicMock()
    fam = MagicMock()
    fam.getElementNames.return_value = ["Standard"]
    model.getStyleFamilies.return_value.getByName.return_value = fam

    text = MagicMock()
    walk_cur = MagicMock()
    walk_cur.getPropertyValue.return_value = "Standard"
    walk_cur.gotoNextParagraph.return_value = False

    para_cur = MagicMock()
    para_cur.createEnumeration.return_value.hasMoreElements.return_value = True
    text.createTextCursorByRange.side_effect = [walk_cur, para_cur]

    failed = html_import_styles.apply_block_lo_styles(
        model, text, MagicMock(), styles, paras=paras
    )
    assert failed == []
    para_el = para_cur.createEnumeration.return_value.nextElement.return_value
    para_el.setPropertyValue.assert_has_calls([
        call("ParaLeftMargin", 3250),
        call("ParaFirstLineIndent", 0),
        call("ParaAdjust", 0),
    ], any_order=True)


@pytest.mark.parametrize(
    "para_decl, has_para_el, expected_failed, expected_para_calls_fn",
    [
        pytest.param(
            "background-color:#ffe4c4",
            True,
            [],
            lambda solid, _none: [call("FillColor", 0xFFE4C4), call("FillStyle", solid)],
            id="solid_color",
        ),
        pytest.param(
            "text-align:justify; background-color:#ffe4c4",
            True,
            [],
            lambda solid, _none: [call("ParaAdjust", 2), call("FillColor", 0xFFE4C4), call("FillStyle", solid)],
            id="solid_and_align",
        ),
        pytest.param(
            "background-color:transparent",
            True,
            [],
            lambda _solid, none: [call("FillStyle", none)],
            id="transparent",
        ),
        pytest.param(
            "background-color:#ffe4c4",
            False,
            ["background-color"],
            lambda _solid, _none: [],
            id="missing_para_obj",
        ),
    ],
)
def test_apply_block_lo_styles_background_color(para_decl, has_para_el, expected_failed, expected_para_calls_fn):
    model = MagicMock()
    fam = MagicMock()
    fam.getElementNames.return_value = ["Standard"]
    model.getStyleFamilies.return_value.getByName.return_value = fam

    text = MagicMock()
    walk_cur = MagicMock()
    walk_cur.getPropertyValue.return_value = "Standard"
    walk_cur.gotoNextParagraph.return_value = False

    para_cur = MagicMock()
    text.createTextCursorByRange.side_effect = [walk_cur, para_cur]

    para_el = MagicMock()
    if has_para_el:
        penum = MagicMock()
        penum.hasMoreElements.side_effect = [True, False]
        penum.nextElement.return_value = para_el
        para_cur.createEnumeration.return_value = penum
    else:
        # The paragraph cursor enumerates nothing, so there is no paragraph to format.
        para_cur.createEnumeration.return_value.hasMoreElements.return_value = False

    solid_style, none_style = html_import_styles._get_fill_style_enums()

    failed = html_import_styles.apply_block_lo_styles(
        model, text, MagicMock(), [None], paras=[para_decl]
    )
    assert failed == expected_failed

    expected_calls = expected_para_calls_fn(solid_style, none_style)
    para_el.setPropertyValue.assert_has_calls(expected_calls, any_order=True)
    # Paragraph formatting and fill properties belong on the paragraph element, never on the cursor.
    cursor_set_props = {c.args[0] for c in para_cur.setPropertyValue.call_args_list}
    assert cursor_set_props.isdisjoint({"FillStyle", "ParaBackColor", "ParaAdjust"})


def test_declared_para_names_flattens_lists_in_order_and_skips_none():
    """Unapplied declarations are reported in document order across all lists; blocks
    without data-lo-para (None) and empty strings contribute nothing."""
    from plugin.writer.html_import_styles import declared_para_names

    assert declared_para_names(
        ["margin-left:1cm; text-align:center", None, ""],
        ["color:#ff0000"],
    ) == ["margin-left", "text-align", "color"]
    assert declared_para_names([]) == []


def test_parse_data_lo_para_duplicate_properties_keeps_first():
    from plugin.writer.html_import_styles import parse_data_lo_para

    props, dropped = parse_data_lo_para("margin-left:1cm; margin-left:2cm; text-align:center; text-align:right")
    assert props == {
        "margin-left": ("ParaLeftMargin", 1000),
        "text-align": ("ParaAdjust", 3),
    }
    assert dropped == []


def test_extractor_handles_data_lo_para():
    from plugin.writer.html_import_styles import extract_block_lo_styles

    html = ('<blockquote data-lo-style="Quotations">'
            '<p data-lo-para="margin-left:1cm">Indented quote</p>'
            '</blockquote>'
            '<p data-lo-para="text-align:right">Only para</p>')
    clean, styles, paras, skipped = extract_block_lo_styles(html)
    assert "data-lo-para" not in clean
    assert "data-lo-style" not in clean
    assert styles == ["Quotations", None]
    assert paras == ["margin-left:1cm", "text-align:right"]
    assert skipped == []


def test_apply_block_lo_styles_cursor_walk_no_prefix_enumeration():
    text = MagicMock()
    next_calls = []
    text.createEnumeration.return_value.hasMoreElements.side_effect = lambda: True
    text.createEnumeration.return_value.nextElement.side_effect = lambda: next_calls.append(1) or MagicMock()

    cur = MagicMock()
    walk_cur = MagicMock()
    text.createTextCursorByRange.return_value = walk_cur
    walk_cur.gotoNextParagraph.return_value = False

    html_import_styles.apply_block_lo_styles(MagicMock(), text, cur, ["Heading1"])

    assert len(next_calls) == 0, "createEnumeration should not be called when cursor is provided"
    walk_cur.gotoStartOfParagraph.assert_called_once_with(False)


def test_resolve_paragraph_style_token_fallback_and_disposal():
    from plugin.framework.errors import DocumentDisposedError
    from plugin.writer.html_import_styles import resolve_paragraph_style_token

    class DisposedException(Exception):
        pass

    # (i) Case-insensitive fallback
    fam = MagicMock()
    names = ("Heading 1", "Standard")
    fam.getElementNames.return_value = names
    fam.hasByName.return_value = True
    assert resolve_paragraph_style_token(fam, "heading 1", element_names=names) == "Heading 1"

    # (ii) Unknown fallback
    fam.hasByName.return_value = False
    assert resolve_paragraph_style_token(fam, "heading 1", element_names=names) == "Standard"

    # (iii) DisposedException on hasByName
    fam.getElementNames.return_value = []
    fam.hasByName.side_effect = DisposedException("closed")
    with pytest.raises(DocumentDisposedError):
        resolve_paragraph_style_token(fam, "heading 1", element_names=[])
