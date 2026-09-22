# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Search-replace keeps an outline hyperlink target in step with the visible title.

Bookmark targets are a different kind of TOC link and must survive a title replace.
The HTML path deletes the matched range, so the URL has to be written back onto
the new text rather than merely preserved.
"""
import uno  # noqa: F401

from plugin.testing_runner import native_test
from plugin.writer.content import ApplyDocumentContent
from plugin.tests.testing_utils import TestingFactory, with_native_doc


_OUTLINE = "#1.Old title|outline"
_OUTLINE_NEW = "#1.New title|outline"
_BOOKMARK = "#__RefHeading___Toc123"
_SHORT = "Hi"
_OUTLINE_SHORT = "#1.Hi|outline"
_LONG = "A much longer section heading"
_OUTLINE_LONG = "#1." + _LONG + "|outline"
_OTHER = "#1.Other|outline"


def _clear(doc):
    text = doc.getText()
    cursor = text.createTextCursor()
    cursor.gotoStart(False)
    cursor.gotoEnd(True)
    cursor.setString("")
    cursor.gotoStart(False)
    return text, cursor


def _append_linked(text, cursor, chunk, url, name="", target=""):
    """Insert *chunk* at *cursor* and, when *url* is set, make that run a hyperlink.

    After insertString the cursor sits at the end of the new text, so goLeft
    selects that run. A cursor saved before the insert moves with the insertion
    and would select nothing. *name* and *target* are optional HyperLinkName /
    HyperLinkTarget values on that same run.
    """
    text.insertString(cursor, chunk, False)
    if not url and not name and not target:
        return
    sel = text.createTextCursorByRange(cursor.getEnd())
    if not sel.goLeft(len(chunk), True):
        raise AssertionError("could not select inserted run %r" % chunk)
    if url:
        sel.setPropertyValue("HyperLinkURL", url)
    if name:
        sel.setPropertyValue("HyperLinkName", name)
    if target:
        sel.setPropertyValue("HyperLinkTarget", target)


def _read_prop(portion, name):
    try:
        return portion.getPropertyValue(name) or ""
    except Exception:
        return ""


def _paragraph_rows(doc):
    """Per paragraph: (text, HyperLinkURL, HyperLinkName, HyperLinkTarget)."""
    text = doc.getText()
    enum = text.createEnumeration()
    paragraphs = []
    while enum.hasMoreElements():
        para = enum.nextElement()
        try:
            if hasattr(para, "supportsService") and not para.supportsService(
                    "com.sun.star.text.Paragraph"):
                continue
            portions = para.createEnumeration()
        except Exception:
            continue
        rows = []
        while portions.hasMoreElements():
            portion = portions.nextElement()
            try:
                chunk = portion.getString() or ""
            except Exception:
                chunk = ""
            url = _read_prop(portion, "HyperLinkURL")
            name = _read_prop(portion, "HyperLinkName")
            target = _read_prop(portion, "HyperLinkTarget")
            if chunk or url or name or target:
                rows.append((chunk, url, name, target))
        paragraphs.append(rows)
    return paragraphs


def _portion_urls(doc):
    """(text, HyperLinkURL) for every portion that has either."""
    return [
        (chunk, url)
        for rows in _paragraph_rows(doc)
        for chunk, url, _name, _target in rows
        if chunk or url
    ]


def _body_paragraphs(doc):
    """Paragraphs that contain visible text. A cleared Writer doc still has an empty one."""
    return [
        rows for rows in _paragraph_rows(doc)
        if any(chunk for chunk, _url, _name, _target in rows)
    ]


def _apply(doc, ctx, **kwargs):
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    return ApplyDocumentContent().execute(tool_ctx, target="search", **kwargs)


def _overlapping(rows, needle):
    """Rows whose text overlaps *needle* in the joined paragraph.

    A character replace splits a run at the first changed character, so the new
    title is often two portions. Searching for the phrase inside one portion misses it.
    """
    text = ""
    spans = []
    for row in rows:
        chunk = row[0]
        spans.append((len(text), len(text) + len(chunk), row))
        text += chunk
    start = text.find(needle)
    assert start >= 0, (needle, text, rows)
    end = start + len(needle)
    return [row for left, right, row in spans if left < end and right > start]


def _urls_over(rows, needle):
    """HyperLinkURL values of portions that overlap *needle*."""
    return [row[1] for row in _overlapping(rows, needle)]


def _append_linked_look(text, cursor, chunk, url, color, underline):
    """Insert a hyperlink and then paint black / no-underline on top of it.

    Setting HyperLinkURL first matches a customized TOC: the Internet-link
    style is already there, then direct Char* hide the chrome.
    """
    _append_linked(text, cursor, chunk, url)
    sel = text.createTextCursorByRange(cursor.getEnd())
    if not sel.goLeft(len(chunk), True):
        raise AssertionError("could not select inserted run %r" % chunk)
    sel.setPropertyValue("CharColor", color)
    sel.setPropertyValue("CharUnderline", underline)


def _portion_looks(doc):
    """(text, HyperLinkURL, CharColor, CharUnderline) for every visible portion."""
    rows = []
    enum = doc.getText().createEnumeration()
    while enum.hasMoreElements():
        para = enum.nextElement()
        try:
            if hasattr(para, "supportsService") and not para.supportsService(
                    "com.sun.star.text.Paragraph"):
                continue
            portions = para.createEnumeration()
        except Exception:
            continue
        while portions.hasMoreElements():
            portion = portions.nextElement()
            try:
                chunk = portion.getString() or ""
            except Exception:
                chunk = ""
            if not chunk:
                continue
            url = _read_prop(portion, "HyperLinkURL")
            try:
                color = portion.getPropertyValue("CharColor")
            except Exception:
                color = None
            try:
                underline = portion.getPropertyValue("CharUnderline")
            except Exception:
                underline = None
            rows.append((chunk, url, color, underline))
    return rows


@native_test
@with_native_doc("writer")
def test_outline_url_write_keeps_black_and_no_underline_uno(ctx, doc):
    """A customized black / no-underline TOC line must not pick up link chrome.

    Discussion #819: HyperLinkURL assignment reapplies navy + single underline
    on new fragments. Numbering, title, punctuation/tab, and page stay black.
    """
    text, cursor = _clear(doc)
    _append_linked_look(text, cursor, "2.3.4. Old title.\t42", _OUTLINE, 0, 0)
    before = _portion_looks(doc)
    assert before, before
    assert all(color == 0 and underline == 0 for _chunk, _url, color, underline in before), before
    res = _apply(doc, ctx, old_content="Old title", content=_LONG)
    assert res.get("status") == "ok", res
    assert res.get("hyperlink_url_after") == _OUTLINE_LONG, res
    rows = _portion_looks(doc)
    body = "".join(chunk for chunk, _url, _color, _underline in rows)
    assert "2.3.4. " + _LONG in body, rows
    assert "42" in body, rows
    assert all(url == _OUTLINE_LONG for _chunk, url, _color, _underline in rows if url), rows
    assert all(color == 0 for _chunk, _url, color, _underline in rows), rows
    assert all(underline == 0 for _chunk, _url, _color, underline in rows), rows


@native_test
@with_native_doc("writer")
def test_search_replace_rewrites_outline_url_and_keeps_bookmark_uno(ctx, doc):
    text, cursor = _clear(doc)
    _append_linked(text, cursor, "Old title", _OUTLINE)
    _append_linked(text, cursor, " tail", _BOOKMARK)
    res = _apply(doc, ctx, old_content="Old title", content="New title")
    assert res.get("status") == "ok", res
    assert res.get("hyperlink_url") == _OUTLINE, res
    assert res.get("hyperlink_url_after") == _OUTLINE_NEW, res
    assert res.get("hyperlink_updated") is True, res
    rows = _portion_urls(doc)
    body = "".join(chunk for chunk, _url in rows)
    assert "New title" in body, rows
    assert "Old title" not in body, rows
    assert _OUTLINE not in [url for _chunk, url in rows], rows
    assert set(_urls_over(rows, "New title")) == {_OUTLINE_NEW}, rows
    assert set(_urls_over(rows, "tail")) == {_BOOKMARK}, rows


@native_test
@with_native_doc("writer")
def test_search_replace_rewrites_the_whole_outline_span_uno(ctx, doc):
    """The title is only part of the link. The number and page number must get the new URL too."""
    text, cursor = _clear(doc)
    _append_linked(text, cursor, "2.3.4. Old title\t42", _OUTLINE)
    res = _apply(doc, ctx, old_content="Old title", content="New title")
    assert res.get("status") == "ok", res
    rows = _portion_urls(doc)
    body = "".join(chunk for chunk, _url in rows)
    assert "2.3.4. New title" in body, rows
    assert "42" in body, rows
    linked = [url for chunk, url in rows if chunk]
    assert linked, rows
    assert all(url == _OUTLINE_NEW for url in linked), rows


@native_test
@with_native_doc("writer")
def test_html_replace_reapplies_outline_url_uno(ctx, doc):
    text, cursor = _clear(doc)
    _append_linked(text, cursor, "Old title", _OUTLINE)
    res = _apply(doc, ctx, old_content="Old title", content="<b>New title</b>")
    assert res.get("status") == "ok", res
    assert res.get("hyperlink_url_after") == _OUTLINE_NEW, res
    rows = _portion_urls(doc)
    assert _OUTLINE not in [url for _chunk, url in rows], rows
    assert set(_urls_over(rows, "New title")) == {_OUTLINE_NEW}, rows


@native_test
@with_native_doc("writer")
def test_dry_run_reports_outline_url_without_editing_uno(ctx, doc):
    text, cursor = _clear(doc)
    _append_linked(text, cursor, "Old title", _OUTLINE)
    res = _apply(doc, ctx, old_content="Old title", content="New title", dry_run=True)
    assert res.get("status") == "ok", res
    assert res.get("dry_run") is True, res
    match = res["matches"][0]
    assert match.get("hyperlink_url") == _OUTLINE, res
    assert match.get("hyperlink_url_after") == _OUTLINE_NEW, res
    rows = _portion_urls(doc)
    assert any(chunk == "Old title" and url == _OUTLINE for chunk, url in rows), rows


@native_test
@with_native_doc("writer")
def test_hyperlink_url_override_uno(ctx, doc):
    text, cursor = _clear(doc)
    _append_linked(text, cursor, "Old title", _OUTLINE)
    res = _apply(
        doc, ctx, old_content="Old title", content="New title",
        hyperlink_url="#9.Custom|outline")
    assert res.get("status") == "ok", res
    assert res.get("hyperlink_url_after") == "#9.Custom|outline", res
    rows = _portion_urls(doc)
    assert set(_urls_over(rows, "New title")) == {"#9.Custom|outline"}, rows
    assert _OUTLINE_NEW not in [url for _chunk, url in rows], rows


@native_test
@with_native_doc("writer")
def test_bookmark_only_replace_does_not_rewrite_the_target_uno(ctx, doc):
    text, cursor = _clear(doc)
    _append_linked(text, cursor, "Old title", _BOOKMARK)
    res = _apply(doc, ctx, old_content="Old title", content="New title")
    assert res.get("status") == "ok", res
    assert "hyperlink_url" not in res, res
    rows = _portion_urls(doc)
    assert set(_urls_over(rows, "New title")) == {_BOOKMARK}, rows


@native_test
@with_native_doc("writer")
def test_shorter_title_does_not_paint_the_following_bookmark_uno(ctx, doc):
    """A shorter replace pushes cursors at the match start onto the next run.

    replace_preserving_format calls setString per changed character. Painting from
    a cursor saved at the match start writes the outline URL onto the bookmark.
    """
    text, cursor = _clear(doc)
    _append_linked(text, cursor, "Old title", _OUTLINE)
    _append_linked(text, cursor, " tail", _BOOKMARK)
    res = _apply(doc, ctx, old_content="Old title", content=_SHORT)
    assert res.get("status") == "ok", res
    assert res.get("hyperlink_url_after") == _OUTLINE_SHORT, res
    rows = _portion_urls(doc)
    body = "".join(chunk for chunk, _url in rows)
    assert _SHORT in body, rows
    assert "Old title" not in body, rows
    assert set(_urls_over(rows, _SHORT)) == {_OUTLINE_SHORT}, rows
    assert set(_urls_over(rows, "tail")) == {_BOOKMARK}, rows


@native_test
@with_native_doc("writer")
def test_longer_title_rewrites_the_whole_toc_span_uno(ctx, doc):
    """Number, title, and page stay one outline link when the title gets longer.

    HyperLinkName and HyperLinkTarget follow only because they contain the matched title.
    """
    text, cursor = _clear(doc)
    _append_linked(
        text, cursor, "2.3.4. Old title\t42", _OUTLINE, name="Old title", target="Old title")
    res = _apply(doc, ctx, old_content="Old title", content=_LONG)
    assert res.get("status") == "ok", res
    assert res.get("hyperlink_url_after") == _OUTLINE_LONG, res
    rows = _body_paragraphs(doc)[0]
    body = "".join(chunk for chunk, _url, _name, _target in rows)
    assert "2.3.4. " + _LONG in body, rows
    assert "42" in body, rows
    linked = [row for row in rows if row[0]]
    assert linked, rows
    assert all(url == _OUTLINE_LONG for _chunk, url, _name, _target in linked), rows
    assert all(name == _LONG for _chunk, _url, name, _target in linked), rows
    assert all(target == _LONG for _chunk, _url, _name, target in linked), rows


@native_test
@with_native_doc("writer")
def test_html_shorter_title_does_not_paint_the_following_bookmark_uno(ctx, doc):
    """setString('') drops the URL, and the write-back must not cover the next bookmark."""
    text, cursor = _clear(doc)
    _append_linked(text, cursor, "Old title", _OUTLINE)
    _append_linked(text, cursor, " tail", _BOOKMARK)
    res = _apply(doc, ctx, old_content="Old title", content="<b>" + _SHORT + "</b>")
    assert res.get("status") == "ok", res
    assert res.get("hyperlink_url_after") == _OUTLINE_SHORT, res
    rows = _portion_urls(doc)
    assert set(_urls_over(rows, _SHORT)) == {_OUTLINE_SHORT}, rows
    assert set(_urls_over(rows, "tail")) == {_BOOKMARK}, rows


@native_test
@with_native_doc("writer")
def test_all_matches_updates_each_outline_url_uno(ctx, doc):
    """Two TOC entries replaced together keep their own targets after a longer title."""
    text, cursor = _clear(doc)
    _append_linked(text, cursor, "Old title", "#1.Old title|outline")
    text.insertControlCharacter(cursor, 0, False)  # PARAGRAPH_BREAK
    _append_linked(text, cursor, "Old title", "#2.Old title|outline")
    res = _apply(doc, ctx, old_content="Old title", content=_LONG, all_matches=True)
    assert res.get("status") == "ok", res
    assert res.get("replaced_count") == 2, res
    after = {report.get("hyperlink_url_after") for report in res.get("hyperlinks") or []}
    assert after == {"#1." + _LONG + "|outline", "#2." + _LONG + "|outline"}, res
    titled = [
        rows for rows in _body_paragraphs(doc)
        if _LONG in "".join(chunk for chunk, _url, _name, _target in rows)
    ]
    assert len(titled) == 2, _body_paragraphs(doc)
    assert set(_urls_over(titled[0], _LONG)) == {"#1." + _LONG + "|outline"}, titled
    assert set(_urls_over(titled[1], _LONG)) == {"#2." + _LONG + "|outline"}, titled


@native_test
@with_native_doc("writer")
def test_unrelated_outline_url_is_put_back_unchanged_uno(ctx, doc):
    """A length change clears HyperLinkURL on the new characters. Put the old target back.

    The matched title is not in this URL, so it must not be rewritten.
    """
    text, cursor = _clear(doc)
    _append_linked(text, cursor, "Old title", _OTHER)
    res = _apply(doc, ctx, old_content="Old title", content=_SHORT)
    assert res.get("status") == "ok", res
    assert res.get("hyperlink_updated") is False, res
    assert res.get("hyperlink_url_after") == _OTHER, res
    rows = _portion_urls(doc)
    assert set(_urls_over(rows, _SHORT)) == {_OTHER}, rows


@native_test
@with_native_doc("writer")
def test_outline_replace_is_one_undo_uno(ctx, doc):
    """The text replace and the URL write are one Ctrl+Z."""
    text, cursor = _clear(doc)
    _append_linked(text, cursor, "Old title", _OUTLINE)
    _append_linked(text, cursor, " tail", _BOOKMARK)
    res = _apply(doc, ctx, old_content="Old title", content=_LONG)
    assert res.get("status") == "ok", res
    um = doc.getUndoManager()
    assert um is not None and um.isUndoPossible(), res
    um.undo()
    rows = _portion_urls(doc)
    body = "".join(chunk for chunk, _url in rows)
    assert "Old title" in body, rows
    assert _LONG not in body, rows
    assert set(_urls_over(rows, "Old title")) == {_OUTLINE}, rows
    assert set(_urls_over(rows, "tail")) == {_BOOKMARK}, rows


@native_test
@with_native_doc("writer")
def test_title_word_does_not_rewrite_the_outline_suffix_uno(ctx, doc):
    """'line' is also inside |outline. Only the title copy is replaced."""
    text, cursor = _clear(doc)
    _append_linked(text, cursor, "The line.", "#1.The line.|outline")
    res = _apply(doc, ctx, old_content="line", content="row")
    assert res.get("status") == "ok", res
    assert res.get("hyperlink_url_after") == "#1.The row.|outline", res
    rows = _portion_urls(doc)
    assert set(_urls_over(rows, "row")) == {"#1.The row.|outline"}, rows
    assert all(not url or url.endswith("|outline") for _chunk, url in rows), rows


@native_test
@with_native_doc("writer")
def test_hyperlink_url_with_all_matches_is_rejected_uno(ctx, doc):
    """One explicit URL must not be stamped onto every TOC entry."""
    text, cursor = _clear(doc)
    _append_linked(text, cursor, "Old title", "#1.Old title|outline")
    text.insertControlCharacter(cursor, 0, False)  # PARAGRAPH_BREAK
    _append_linked(text, cursor, "Old title", "#2.Old title|outline")
    res = _apply(
        doc, ctx, old_content="Old title", content=_LONG, all_matches=True,
        hyperlink_url="#9.Custom|outline")
    assert res.get("status") == "error", res
    assert res.get("code") == "INVALID_PARAM", res
    titled = [
        rows for rows in _body_paragraphs(doc)
        if "Old title" in "".join(chunk for chunk, _url, _name, _target in rows)
    ]
    assert len(titled) == 2, _body_paragraphs(doc)
    assert set(_urls_over(titled[0], "Old title")) == {"#1.Old title|outline"}, titled
    assert set(_urls_over(titled[1], "Old title")) == {"#2.Old title|outline"}, titled


@native_test
@with_native_doc("writer")
def test_hyperlink_url_on_a_bookmark_span_is_rejected_uno(ctx, doc):
    """The override must not split number / title / page into two targets."""
    text, cursor = _clear(doc)
    _append_linked(text, cursor, "2.3.4. Old title\t42", _BOOKMARK)
    res = _apply(
        doc, ctx, old_content="Old title", content="New title",
        hyperlink_url="#9.Custom|outline")
    assert res.get("status") == "error", res
    assert res.get("code") == "INVALID_PARAM", res
    rows = _portion_urls(doc)
    body = "".join(chunk for chunk, _url in rows)
    assert "Old title" in body, rows
    assert "New title" not in body, rows
    linked = [url for chunk, url in rows if chunk]
    assert linked and all(url == _BOOKMARK for url in linked), rows


@native_test
@with_native_doc("writer")
def test_hyperlink_url_on_plain_text_is_rejected_uno(ctx, doc):
    text, cursor = _clear(doc)
    text.insertString(cursor, "Old title", False)
    res = _apply(
        doc, ctx, old_content="Old title", content="New title",
        hyperlink_url="#9.Custom|outline")
    assert res.get("status") == "error", res
    assert res.get("code") == "INVALID_PARAM", res
    rows = _portion_urls(doc)
    body = "".join(chunk for chunk, _url in rows)
    assert "Old title" in body, rows
    assert set(_urls_over(rows, "Old title")) == {""}, rows


@native_test
@with_native_doc("writer")
def test_deleting_the_whole_outline_link_succeeds_uno(ctx, doc):
    """Clearing the only linked characters used to roll the delete back."""
    text, cursor = _clear(doc)
    _append_linked(text, cursor, "Old title", _OUTLINE)
    res = _apply(doc, ctx, old_content="Old title", content="")
    assert res.get("status") == "ok", res
    body = doc.getText().getString()
    assert "Old title" not in body, body


@native_test
@with_native_doc("writer")
def test_dry_run_before_does_not_preview_an_outline_rewrite_uno(ctx, doc):
    text, cursor = _clear(doc)
    _append_linked(text, cursor, "Old title", _OUTLINE)
    res = _apply(
        doc, ctx, old_content="Old title", content="Hi", dry_run=True, position="before")
    assert res.get("status") == "ok", res
    assert "hyperlink_url" not in res["matches"][0], res
    rows = _portion_urls(doc)
    assert any(chunk == "Old title" and url == _OUTLINE for chunk, url in rows), rows
    rejected = _apply(
        doc, ctx, old_content="Old title", content="Hi", dry_run=True, position="before",
        hyperlink_url="#9.Custom|outline")
    assert rejected.get("status") == "error", rejected
    assert rejected.get("code") == "INVALID_PARAM", rejected


@native_test
@with_native_doc("writer")
def test_spanning_two_outline_links_does_not_stamp_the_first_url_uno(ctx, doc):
    """A phrase that crosses two links must not give the second the first target."""
    text, cursor = _clear(doc)
    _append_linked(text, cursor, "Left", "#1.Left|outline")
    _append_linked(text, cursor, "Right", "#2.Right|outline")
    res = _apply(doc, ctx, old_content="LeftRight", content="LeftZZZZ")
    assert res.get("status") == "ok", res
    rows = _portion_urls(doc)
    assert "#1.Left|outline" not in _urls_over(rows, "ZZZZ"), rows
    assert "#2.Right|outline" not in _urls_over(rows, "Left"), rows


@native_test
@with_native_doc("writer")
def test_hyperlink_url_across_two_outline_links_is_rejected_uno(ctx, doc):
    text, cursor = _clear(doc)
    _append_linked(text, cursor, "Left", "#1.Left|outline")
    _append_linked(text, cursor, "Right", "#2.Right|outline")
    res = _apply(
        doc, ctx, old_content="LeftRight", content="LeftZZZZ",
        hyperlink_url="#9.Custom|outline")
    assert res.get("status") == "error", res
    assert res.get("code") == "INVALID_PARAM", res
    rows = _portion_urls(doc)
    body = "".join(chunk for chunk, _url in rows)
    assert "LeftRight" in body, rows
    assert "LeftZZZZ" not in body, rows
    assert set(_urls_over(rows, "Left")) == {"#1.Left|outline"}, rows
    assert set(_urls_over(rows, "Right")) == {"#2.Right|outline"}, rows


@native_test
@with_native_doc("writer")
def test_identical_replace_sets_outline_url_uno(ctx, doc):
    """The visible title is already current, so the searched text is not in the URL.

    Substitution cannot repair ``#1.Old title|outline``. ``hyperlink_url`` on an
    identical replace sets that one target and leaves the following bookmark.
    """
    text, cursor = _clear(doc)
    _append_linked(text, cursor, "New title", _OUTLINE)
    _append_linked(text, cursor, " tail", _BOOKMARK)
    preview = _apply(
        doc, ctx, old_content="New title", content="New title",
        hyperlink_url=_OUTLINE_NEW, dry_run=True)
    assert preview.get("status") == "ok", preview
    assert preview.get("dry_run") is True, preview
    match = preview["matches"][0]
    assert match.get("hyperlink_url") == _OUTLINE, preview
    assert match.get("hyperlink_url_after") == _OUTLINE_NEW, preview
    assert set(_urls_over(_portion_urls(doc), "New title")) == {_OUTLINE}
    res = _apply(
        doc, ctx, old_content="New title", content="New title",
        hyperlink_url=_OUTLINE_NEW)
    assert res.get("status") == "ok", res
    assert res.get("hyperlink_url_after") == _OUTLINE_NEW, res
    rows = _portion_urls(doc)
    body = "".join(chunk for chunk, _url in rows)
    assert body == "New title tail", rows
    assert set(_urls_over(rows, "New title")) == {_OUTLINE_NEW}, rows
    assert set(_urls_over(rows, "tail")) == {_BOOKMARK}, rows
