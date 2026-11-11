# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Tests for heading extraction, link resolution, and diagnostics in html_import_styles."""

from __future__ import annotations

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
    assert diags[0] == {
        "code": html_import_styles.LINK_TARGET_NOT_FOUND,
        "link_target": "#overview",
        "message": (
            'Link target "#overview" matches no heading or bookmark and will not jump anywhere. '
            'Use the exact heading text, href="#Heading text|outline", or give the target element '
            'an id and link to that.'
        ),
    }


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


def test_attach_link_warnings() -> None:
    """Diagnostics are attached to successful results and ignored on errors."""
    diags = [
        {
            "code": html_import_styles.LINK_TARGET_NOT_FOUND,
            "link_target": "#nowhere",
            "message": "error msg",
        }
    ]
    err_res = {"status": "error", "message": "Failed"}
    assert html_import_styles.attach_link_warnings(err_res, diags) == err_res
    assert "warnings" not in err_res

    ok_res = {"status": "ok", "message": "Done"}
    updated = html_import_styles.attach_link_warnings(ok_res, diags)
    assert updated.get("warnings") == diags

    # Extends existing warnings list
    existing_res = {
        "status": "ok",
        "warnings": [{"code": "EXISTING", "message": "Prior warning"}],
    }
    updated_existing = html_import_styles.attach_link_warnings(existing_res, diags)
    assert len(updated_existing["warnings"]) == 2
    assert updated_existing["warnings"][0]["code"] == "EXISTING"
    assert updated_existing["warnings"][1]["code"] == html_import_styles.LINK_TARGET_NOT_FOUND


def test_attach_math_objects_all_ok_attaches_list() -> None:
    ok_res = {"status": "ok", "message": "Done"}
    entries = [
        {"kind": "tex", "display_block": False, "snippet": "a", "ok": True},
        {"kind": "mml", "display_block": True, "snippet": "<math></math>", "ok": True},
    ]
    res = html_import_styles.attach_math_objects(ok_res, entries)
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
    res = html_import_styles.attach_math_objects(ok_res, entries)
    assert res["status"] == "ok"
    assert res["paragraph_count"] == 2
    assert res["math_objects"] == entries
    assert "warnings" in res
    assert len(res["warnings"]) == 1
    diag = res["warnings"][0]
    assert diag["code"] == html_import_styles.MATH_IMPORT_FAILED
    assert diag["math_objects"] == entries
    assert r"\sqrt{" in diag["message"]
    assert "NoAvailableTokensError" in diag["message"]

    err_res = {"status": "error", "message": "Failed"}
    assert html_import_styles.attach_math_objects(err_res, entries) == err_res
