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
         patch("plugin.writer.search.describe_match_location", return_value="body"), \
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
         patch("plugin.writer.search.describe_match_location", return_value="body"), \
         patch("plugin.writer.format.content_has_markup", return_value=False):
        res = ApplyDocumentContent().execute(
            ctx, content=["x"], target="search", old_content=r"brav. charl.e", dry_run=True, regex=True)
    assert res["status"] == "ok" and res["count"] == 1
    far.assert_not_called()
    args = frc.call_args[0]
    assert args[1] == r"brav. charl.e" and args[2] is True  # raw pattern, regex on


def test_dry_run_invalid_regex():
    from plugin.writer.content import ApplyDocumentContent

    ctx = _edit_ctx()
    ctx.services.get.return_value = MagicMock()
    with patch("plugin.writer.format.content_has_markup", return_value=False):
        res = ApplyDocumentContent().execute(
            ctx, content=["x"], target="search", old_content="([a-", dry_run=True, regex=True)
    assert res["status"] == "error" and res["code"] == "INVALID_REGEX"


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
