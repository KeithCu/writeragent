# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""The 6 smaller R5 fixes: dry_run, apply_style all/occurrence, track_changes_list text/location,
add_comment span/occurrence/author, insert_page_break anchored, regex/case in apply. No LibreOffice."""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from plugin.tests.testing_utils import setup_uno_mocks
setup_uno_mocks()


def _edit_ctx():
    ctx = MagicMock()
    ctx.doc.getUndoManager.return_value.isLocked.return_value = False
    return ctx


# ---- D1: dry_run ------------------------------------------------------------

def test_dry_run_reports_matches_without_mutating():
    from plugin.writer import content as content_mod
    from plugin.writer.content import ApplyDocumentContent

    r1, r2 = MagicMock(), MagicMock()
    r1.getString.return_value = "clause 3.2 text"
    r2.getString.return_value = "clause 3.2 again"
    ctx = _edit_ctx()
    with patch.object(content_mod, "_find_all_ranges", return_value=[r1, r2]) as fa, \
         patch("plugin.writer.search._describe_match_location", return_value="body"), \
         patch.object(content_mod, "_normalize_search_string_for_find", side_effect=lambda s: s), \
         patch("plugin.writer.format.content_has_markup", return_value=False):
        res = ApplyDocumentContent().execute(ctx, content=["x"], target="search", old_content="clause 3.2", dry_run=True)
    assert res["status"] == "ok" and res["dry_run"] is True and res["count"] == 2
    assert res["matches"][0]["location"] == "body"
    # No session/undo context opened for a dry run.
    ctx.doc.getUndoManager.assert_not_called()


def test_dry_run_requires_search_target():
    from plugin.writer.content import ApplyDocumentContent
    res = ApplyDocumentContent().execute(_edit_ctx(), content=["x"], target="end", dry_run=True)
    assert res["status"] == "error" and "search" in res["message"]


def test_dry_run_honors_regex_via_the_same_matcher_as_the_edit():
    """A preview that uses a different matcher than the commit is worse than none: with
    regex=true, dry_run must route through _find_ranges_regex_case with the RAW pattern."""
    from plugin.writer import content as content_mod
    from plugin.writer.content import ApplyDocumentContent

    r = MagicMock()
    r.getString.return_value = "bravo charlie"
    ctx = _edit_ctx()
    with patch.object(content_mod, "_find_ranges_regex_case", return_value=[r]) as frc, \
         patch.object(content_mod, "_find_all_ranges") as far, \
         patch("plugin.writer.search._describe_match_location", return_value="body"), \
         patch("plugin.writer.format.content_has_markup", return_value=False):
        res = ApplyDocumentContent().execute(
            ctx, content=["x"], target="search", old_content=r"brav. charl.e", dry_run=True, regex=True)
    assert res["status"] == "ok" and res["count"] == 1
    far.assert_not_called()
    args = frc.call_args[0]
    assert args[1] == r"brav. charl.e" and args[2] is True  # raw pattern, regex on


# ---- D5: page break happy paths + last-paragraph edge ------------------------

def _pagebreak_doc(found_next=True):
    import types as _types
    import sys as _sys
    bt = _types.ModuleType("com.sun.star.style.BreakType")
    bt.PAGE_BEFORE = "PAGE_BEFORE"
    _sys.modules["com.sun.star.style.BreakType"] = bt
    doc = MagicMock()
    found = MagicMock()
    para = MagicMock()
    para.gotoNextParagraph.return_value = found_next
    found.getText.return_value.createTextCursorByRange.return_value = para
    doc.findFirst.return_value = found
    return doc, para


def test_page_break_before_text_sets_break_on_match_paragraph():
    from plugin.writer.page import InsertPageBreak

    doc, para = _pagebreak_doc()
    res = InsertPageBreak().execute(SimpleNamespace(doc=doc), before_text="Assinaturas")
    assert res["status"] == "ok" and "before" in res["message"]
    para.setPropertyValue.assert_called_once_with("BreakType", "PAGE_BEFORE")
    para.gotoNextParagraph.assert_not_called()


def test_page_break_after_text_breaks_on_following_paragraph():
    from plugin.writer.page import InsertPageBreak

    doc, para = _pagebreak_doc(found_next=True)
    res = InsertPageBreak().execute(SimpleNamespace(doc=doc), after_text="clausula")
    assert res["status"] == "ok" and "after" in res["message"]
    para.gotoNextParagraph.assert_called_once()
    para.setPropertyValue.assert_called_once_with("BreakType", "PAGE_BEFORE")


def test_page_break_after_text_last_paragraph_errors_instead_of_wrong_direction():
    from plugin.writer.page import InsertPageBreak

    doc, para = _pagebreak_doc(found_next=False)
    res = InsertPageBreak().execute(SimpleNamespace(doc=doc), after_text="assinatura final")
    assert res["status"] == "error" and "last paragraph" in res["message"]
    para.setPropertyValue.assert_not_called()  # never the wrong-direction silent break


# ---- D2: apply_style all_matches / occurrence -------------------------------

def _style_ctx():
    ctx = MagicMock()
    fam = MagicMock()
    fam.hasByName.return_value = True
    ctx.doc.getStyleFamilies.return_value.getByName.return_value = fam
    return ctx


def test_apply_style_all_matches_applies_to_each():
    from plugin.writer.styles import ApplyStyle

    ranges = [MagicMock(), MagicMock(), MagicMock()]
    with patch("plugin.writer.content._find_all_ranges", return_value=ranges), \
         patch("plugin.writer.content._normalize_search_string_for_find", side_effect=lambda s: s), \
         patch("plugin.writer.format.content_has_markup", return_value=False), \
         patch("plugin.writer.styles.apply_paragraph_style_preserving_direct_char") as ap, \
         patch("plugin.writer.edit_review.review_recording_enabled", return_value=False):
        res = ApplyStyle().execute(_style_ctx(), style_name="Heading 1", target="search",
                                   old_content="Title", all_matches=True)
    assert res["status"] == "ok" and res["applied_count"] == 3
    assert ap.call_count == 3


def test_apply_style_occurrence_out_of_range():
    from plugin.writer.styles import ApplyStyle

    with patch("plugin.writer.content._find_all_ranges", return_value=[MagicMock()]), \
         patch("plugin.writer.content._normalize_search_string_for_find", side_effect=lambda s: s), \
         patch("plugin.writer.format.content_has_markup", return_value=False):
        res = ApplyStyle().execute(_style_ctx(), style_name="Heading 1", target="search",
                                   old_content="Title", occurrence=5)
    assert res["status"] == "error" and "out of range" in res["message"]


# ---- D3: track_changes_list text + location ---------------------------------

def test_track_changes_list_adds_text_and_location():
    from plugin.writer.tracking import TrackChangesList

    # Redlines from getRedlines() are property sets: RedlineStart/RedlineEnd (XTextRange) and
    # RedlineText (removed content) — NOT getAnchor. For an insertion the live span carries the text.
    start = MagicMock()
    span_cursor = MagicMock()
    span_cursor.getString.return_value = "inserted clause"
    start.getText.return_value.createTextCursorByRange.return_value = span_cursor
    props = {"RedlineType": "Insert", "RedlineAuthor": "X", "RedlineStart": start, "RedlineEnd": MagicMock()}
    redline = MagicMock()
    redline.getPropertyValue.side_effect = lambda p: props.get(p, "")
    enum = MagicMock()
    enum.hasMoreElements.side_effect = [True, False]
    enum.nextElement.return_value = redline
    doc = MagicMock()
    doc.getPropertyValue.return_value = True
    doc.getRedlines.return_value.createEnumeration.return_value = enum
    ctx = SimpleNamespace(doc=doc)
    with patch("plugin.writer.search._describe_match_location", return_value="body"):
        res = TrackChangesList().execute(ctx)
    assert res["status"] == "ok" and res["count"] == 1
    e = res["changes"][0]
    assert e["text"] == "inserted clause" and e["location"] == "body"


def test_track_changes_list_deletion_falls_back_to_redlinetext():
    from plugin.writer.tracking import TrackChangesList

    start = MagicMock()
    # Deletion: the live span is collapsed (empty), so RedlineText supplies the removed text.
    start.getText.return_value.createTextCursorByRange.return_value.getString.return_value = ""
    rtext = MagicMock()
    rtext.getString.return_value = "removed words"
    props = {"RedlineType": "Delete", "RedlineStart": start, "RedlineEnd": MagicMock(), "RedlineText": rtext}
    redline = MagicMock()
    redline.getPropertyValue.side_effect = lambda p: props.get(p, "")
    enum = MagicMock()
    enum.hasMoreElements.side_effect = [True, False]
    enum.nextElement.return_value = redline
    doc = MagicMock()
    doc.getPropertyValue.return_value = True
    doc.getRedlines.return_value.createEnumeration.return_value = enum
    with patch("plugin.writer.search._describe_match_location", return_value="body"):
        res = TrackChangesList().execute(SimpleNamespace(doc=doc))
    assert res["changes"][0]["text"] == "removed words"


# ---- list_comments: paragraph-context fallback for empty anchor previews ----

def test_read_annotation_falls_back_to_paragraph_context():
    from plugin.writer.specialized.comments import _read_annotation

    anchor = MagicMock()
    anchor.getString.return_value = ""  # annotation anchors read as empty points
    field = MagicMock()
    field.getAnchor.return_value = anchor
    field.getPropertyValue.side_effect = lambda p: {"Author": "Rev", "Content": "note"}.get(p, "")
    doc_svc = MagicMock()
    doc_svc.find_paragraph_for_range.return_value = 7
    with patch("plugin.writer.search._enclosing_paragraph_text", return_value="  the clause the comment covers  "):
        e = _read_annotation(field, [], MagicMock(), doc_svc)
    assert e["anchor_preview"] == "the clause the comment covers"
    assert e["anchor_is_paragraph_context"] is True
    assert e["paragraph_index"] == 7


def test_read_annotation_keeps_real_span_when_present():
    from plugin.writer.specialized.comments import _read_annotation

    anchor = MagicMock()
    anchor.getString.return_value = "covered words"
    field = MagicMock()
    field.getAnchor.return_value = anchor
    field.getPropertyValue.side_effect = lambda p: {"Author": "Rev"}.get(p, "")
    e = _read_annotation(field, [], MagicMock(), MagicMock())
    assert e["anchor_preview"] == "covered words"
    assert "anchor_is_paragraph_context" not in e


# ---- track_changes_list: agent_review_mode alongside the doc toggle ----------

def test_track_changes_list_reports_agent_review_mode():
    from plugin.writer.tracking import TrackChangesList

    enum = MagicMock()
    enum.hasMoreElements.return_value = False
    doc = MagicMock()
    doc.getPropertyValue.return_value = False  # document toggle off...
    doc.getRedlines.return_value.createEnumeration.return_value = enum
    ctx = SimpleNamespace(doc=doc, ctx=MagicMock())
    with patch("plugin.writer.edit_review.get_agent_edit_review_mode", return_value="record"):
        res = TrackChangesList().execute(ctx)
    assert res["recording"] is False
    assert res["agent_review_mode"] == "record"  # ...but agent edits ARE being tracked


# ---- accept/reject select via RedlineStart/End (redlines have no getAnchor) --

def test_accept_selects_via_redline_start_end_not_getanchor():
    from plugin.writer.tracking import TrackChangesAccept

    start = MagicMock(spec=["getText"])
    span = MagicMock()
    start.getText.return_value.createTextCursorByRange.return_value = span
    props = {"RedlineStart": start, "RedlineEnd": MagicMock(), "RedlineComment": "user change"}
    # spec-limited: no getAnchor attribute at all, like the real UNO redline property set.
    redline = MagicMock(spec=["getPropertyValue"])
    redline.getPropertyValue.side_effect = lambda p: props.get(p, "")
    enum = MagicMock()
    enum.hasMoreElements.side_effect = [True, False]
    enum.nextElement.return_value = redline
    doc = MagicMock()
    doc.getRedlines.return_value.createEnumeration.return_value = enum
    ctx = MagicMock()
    ctx.doc = doc
    with patch("plugin.writer.inline_review.redline_is_agent_change", return_value=(False, True)):
        res = TrackChangesAccept().execute(ctx, index=0)
    assert res["status"] == "ok", res
    doc.getCurrentController.return_value.select.assert_called_once_with(span)
    span.gotoRange.assert_called_once()  # expanded to RedlineEnd


def test_accept_still_refuses_agent_own_changes():
    from plugin.writer.tracking import TrackChangesAccept

    redline = MagicMock(spec=["getPropertyValue"])
    redline.getPropertyValue.return_value = "wa-review:abc:0"
    enum = MagicMock()
    enum.hasMoreElements.side_effect = [True, False]
    enum.nextElement.return_value = redline
    ctx = MagicMock()
    ctx.doc.getRedlines.return_value.createEnumeration.return_value = enum
    with patch("plugin.writer.inline_review.redline_is_agent_change", return_value=(True, True)):
        res = TrackChangesAccept().execute(ctx, index=0)
    assert res["status"] == "error" and "must not accept" in res["message"]
    ctx.doc.getCurrentController.return_value.select.assert_not_called()


# ---- D4: add_comment span / occurrence / author -----------------------------

def test_add_comment_occurrence_author_and_span():
    from plugin.writer.specialized.comments import AddComment

    doc = MagicMock()
    first, second = MagicMock(), MagicMock()
    second.getString.return_value = "second hit"
    second.getText.return_value = mtext = MagicMock()
    doc.findFirst.return_value = first
    doc.findNext.return_value = second
    ctx = SimpleNamespace(doc=doc)
    with patch("plugin.writer.specialized.comments._set_annotation_date"):
        res = AddComment().execute(ctx, content="note", search_text="hit", occurrence=1, author="Rev")
    assert res["status"] == "ok" and res["author"] == "Rev" and res["anchor_text"] == "second hit"
    # Spans the match: insertTextContent called with absorb=True.
    assert mtext.insertTextContent.call_args[0][2] is True


def test_add_comment_not_found_at_occurrence():
    from plugin.writer.specialized.comments import AddComment

    doc = MagicMock()
    doc.findFirst.return_value = MagicMock()
    doc.findNext.return_value = None
    res = AddComment().execute(SimpleNamespace(doc=doc), content="n", search_text="x", occurrence=3)
    assert res["status"] == "error" and res["comment_added"] is False


# ---- D5: insert_page_break anchored -----------------------------------------

def test_page_break_both_anchors_rejected():
    from plugin.writer.page import InsertPageBreak
    res = InsertPageBreak().execute(SimpleNamespace(doc=MagicMock()), before_text="a", after_text="b")
    assert res["status"] == "error" and "only one" in res["message"]


def test_page_break_anchor_not_found():
    from plugin.writer.page import InsertPageBreak
    doc = MagicMock()
    doc.findFirst.return_value = None
    # com.sun.star.style.BreakType is a mocked module; the import inside execute resolves via the uno mock.
    res = InsertPageBreak().execute(SimpleNamespace(doc=doc), before_text="Signature")
    assert res["status"] == "error" and "not found" in res["message"]


# ---- D6: regex / case in the edit path --------------------------------------

def test_find_ranges_regex_case_builds_descriptor():
    from plugin.writer.content import _find_ranges_regex_case

    doc = MagicMock()
    sd = MagicMock()
    doc.createSearchDescriptor.return_value = sd
    doc.findFirst.return_value = None
    _find_ranges_regex_case(doc, r"cl\w+", True, False, all_matches=False)
    assert sd.SearchString == r"cl\w+"
    assert sd.SearchRegularExpression is True and sd.SearchCaseSensitive is False


def test_find_ranges_regex_case_all_matches_walks_next():
    from plugin.writer.content import _find_ranges_regex_case

    doc = MagicMock()
    doc.createSearchDescriptor.return_value = MagicMock()
    a, b = MagicMock(), MagicMock()
    doc.findFirst.return_value = a
    doc.findNext.side_effect = [b, None]
    out = _find_ranges_regex_case(doc, "x", False, True, all_matches=True)
    assert out == [a, b]
