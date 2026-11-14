# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Search descriptor helpers. No LibreOffice."""
from unittest.mock import MagicMock, patch
import pytest

# ---- D6: regex / case in the edit path --------------------------------------

def test_find_ranges_regex_case_builds_descriptor():
    from plugin.writer.search import find_ranges_regex_case

    doc = MagicMock()
    sd = MagicMock()
    doc.createSearchDescriptor.return_value = sd
    doc.findFirst.return_value = None
    find_ranges_regex_case(doc, r"cl\w+", True, False, all_matches=False)
    assert sd.SearchString == r"cl\w+"
    assert sd.SearchRegularExpression is True and sd.SearchCaseSensitive is False


def test_find_ranges_regex_case_all_matches_walks_next():
    from plugin.writer.search import find_ranges_regex_case

    doc = MagicMock()
    doc.createSearchDescriptor.return_value = MagicMock()
    a, b = MagicMock(), MagicMock()
    doc.findFirst.return_value = a
    doc.findNext.side_effect = [b, None]
    out = find_ranges_regex_case(doc, "x", False, True, all_matches=True)
    assert out == [a, b]

# ---- 3) invalid regex is an error, not a clean miss ---------------------------

def _search_zero_hits(pattern, use_regex, icu_ok: bool = True, ctx=None):
    """Run SearchInDocument against a doc that finds nothing."""
    from plugin.writer.search import SearchInDocument

    doc = MagicMock()
    doc.createSearchDescriptor.return_value = MagicMock()
    doc.findFirst.return_value = None
    doc.getDrawPage.return_value = []
    doc.getTextFields.return_value.createEnumeration.return_value.hasMoreElements.return_value = False
    if ctx is None:
        ctx = MagicMock()
    ctx.doc = doc
    ctx.ctx.ServiceManager.createInstanceWithContext.return_value.searchForward.return_value.subRegExpressions = (
        1 if icu_ok else 0
    )
    return SearchInDocument().execute(ctx, pattern=pattern, regex=use_regex)


def test_invalid_regex_zero_hits_is_error():
    res = _search_zero_hits("([a-", True, icu_ok=False)
    assert res["status"] == "error" and res["code"] == "INVALID_REGEX"
    assert "regex=false" in res["message"]


def test_valid_regex_zero_hits_stays_ok():
    res = _search_zero_hits("nunca_existe_\\d+", True)
    assert res["status"] == "ok" and res["count"] == 0


def test_literal_zero_hits_stays_ok():
    ctx = MagicMock()
    res = _search_zero_hits("([a-", False, ctx=ctx)  # literal search for weird chars is legitimate
    assert res["status"] == "ok" and res["count"] == 0
    ctx.ctx.ServiceManager.createInstanceWithContext.assert_not_called()


def test_validate_icu_regex_accepts_valid_and_rejects_invalid():
    from plugin.writer.search import ICU_REGEX_REJECTED, validate_icu_regex

    ctx = MagicMock()
    ts = ctx.ServiceManager.createInstanceWithContext.return_value
    ts.searchForward.return_value.subRegExpressions = 1
    assert validate_icu_regex(ctx, r"\p{Lu}+") is None

    ts.searchForward.return_value.subRegExpressions = 0
    assert validate_icu_regex(ctx, "([a-") == ICU_REGEX_REJECTED


def test_search_in_document_icu_only_regex_warns_on_shape_skip():
    from plugin.writer.search import SearchInDocument

    doc = MagicMock()
    doc.createSearchDescriptor.return_value = MagicMock()
    doc.findFirst.return_value = None
    doc.getDrawPage.return_value = []
    doc.getTextFields.return_value.createEnumeration.return_value.hasMoreElements.return_value = False
    ctx = MagicMock()
    ctx.doc = doc
    ctx.ctx.ServiceManager.createInstanceWithContext.return_value.searchForward.return_value.subRegExpressions = 1

    res = SearchInDocument().execute(ctx, pattern=r"\p{Lu}\w+", regex=True)
    assert res["status"] == "ok"
    assert res.get("warning") == "shape/comment text not searched: pattern uses ICU-only syntax"


def test_shape_sweep_skip_reason():
    from plugin.writer.search import shape_sweep_skip_reason

    assert shape_sweep_skip_reason(r"\p{Lu}", True) == (
        "shape/comment text not searched: pattern uses ICU-only syntax"
    )
    assert shape_sweep_skip_reason(r"\p{Lu}", False) is None
    assert shape_sweep_skip_reason("a.c", True) is None


def test_search_return_offsets_rejects_regex():
    from plugin.writer.search import SearchInDocument

    res = SearchInDocument().execute(MagicMock(doc=MagicMock()), pattern="a+", regex=True, return_offsets=True)
    assert res["status"] == "error" and res["code"] == "INVALID_PARAM"


def test_find_ranges_regex_case_caps_at_max_search_replacements():
    from plugin.writer.search import find_ranges_regex_case, MAX_SEARCH_REPLACEMENTS

    doc = MagicMock()
    doc.createSearchDescriptor.return_value = MagicMock()
    doc.findFirst.return_value = MagicMock()
    doc.findNext.side_effect = lambda found, sd: MagicMock()
    out = find_ranges_regex_case(doc, "x", True, True, all_matches=True)
    assert len(out) == MAX_SEARCH_REPLACEMENTS


def test_find_all_ranges_caps_at_max_search_replacements():
    from plugin.writer.search import find_all_ranges, MAX_SEARCH_REPLACEMENTS

    doc = MagicMock()
    doc.createSearchDescriptor.return_value = MagicMock()
    doc.findFirst.return_value = MagicMock()
    doc.findNext.side_effect = lambda found, sd: MagicMock()
    out = find_all_ranges(doc, "x")
    assert len(out) == MAX_SEARCH_REPLACEMENTS


def test_not_found_hints_at_pending_tracked_deletions():
    """#43: visible text across a struck word is never found; say why instead of just 'shorter'."""
    from plugin.writer.search import build_search_not_found_response

    doc = MagicMock()
    doc.getDrawPage.return_value = None
    with patch("plugin.writer.search.document_has_tracked_deletions", return_value=True):
        msg = build_search_not_found_response(doc, "needle")["message"]
    assert "pending tracked deletions" in msg
    doc2 = MagicMock()
    doc2.getDrawPage.return_value = None
    assert "pending tracked" not in build_search_not_found_response(doc2, "needle")["message"]


def test_search_in_document_zero_width_sets_truncated():
    from plugin.writer.search import SearchInDocument

    doc = MagicMock()
    found = MagicMock()
    found.getString.return_value = ""
    doc.findFirst.return_value = found
    del doc.getDrawPage
    doc.getTextFields.return_value.createEnumeration.return_value.hasMoreElements.return_value = False
    ctx = MagicMock(doc=doc)

    res = SearchInDocument().execute(ctx, pattern="^", regex=True)
    doc.findNext.assert_not_called()
    assert res["status"] == "ok"
    assert res["count"] == 0
    assert res["matches"] == []
    assert res["truncated"] is True
    assert res["warning"] == "Search stopped on a zero-width match; count may be incomplete."


def test_search_in_document_getstring_failure_is_truncated(caplog):
    import logging
    from plugin.writer.search import SearchInDocument

    doc = MagicMock()
    found = MagicMock()
    found.getString.side_effect = RuntimeError("read error")
    doc.findFirst.return_value = found
    del doc.getDrawPage
    doc.getTextFields.return_value.createEnumeration.return_value.hasMoreElements.return_value = False
    ctx = MagicMock(doc=doc)

    with caplog.at_level(logging.ERROR, logger="writeragent.writer"):
        res = SearchInDocument().execute(ctx, pattern="hello")

    assert res["status"] == "ok"
    assert res["count"] == 0
    assert res["truncated"] is True
    assert res["warning"] == "Text-model search ended early after a getString failure; count may be incomplete."
    assert any(r.levelno >= logging.ERROR and r.exc_info for r in caplog.records)


@pytest.mark.parametrize("bad_val", [0, -1])
def test_search_in_document_max_results_rejects_non_positive(bad_val):
    from plugin.writer.search import SearchInDocument

    doc = MagicMock()
    ctx = MagicMock(doc=doc)
    res = SearchInDocument().execute(ctx, pattern="test", max_results=bad_val)
    assert res["status"] == "error"
    assert res["code"] == "INVALID_PARAM"
    doc.createSearchDescriptor.assert_not_called()


@pytest.mark.parametrize("bad_val", [True, False, "20", 1.5])
def test_search_in_document_max_results_wrong_type_is_rejected_before_execute(bad_val):
    from types import SimpleNamespace

    from plugin.framework.tool import prepare_tool_call
    from plugin.writer.search import SearchInDocument

    ctx = SimpleNamespace(doc_type="writer", caller="test")
    prep = prepare_tool_call(SearchInDocument(), ctx, "search_in_document", {"pattern": "test", "max_results": bad_val})
    assert prep.error is not None
    assert prep.error["code"] == "VALIDATION_ERROR"
    assert prep.error["message"].startswith("Invalid type for max_results: expected integer")


def test_search_in_document_null_max_results_uses_the_default():
    from types import SimpleNamespace

    from plugin.framework.tool import prepare_tool_call
    from plugin.writer.search import SearchInDocument

    ctx = SimpleNamespace(doc_type="writer", caller="test")
    prep = prepare_tool_call(SearchInDocument(), ctx, "search_in_document", {"pattern": "test", "max_results": None})
    assert prep.error is None
    assert prep.kwargs == {"pattern": "test"}


def test_search_in_document_default_max_results_is_20():
    from plugin.writer.search import SearchInDocument

    doc = MagicMock()
    hits = [MagicMock(**{"getString.return_value": f"hit_{i}"}) for i in range(25)]
    doc.findFirst.return_value = hits[0]
    doc.findNext.side_effect = hits[1:] + [None]
    del doc.getDrawPage
    doc.getTextFields.return_value.createEnumeration.return_value.hasMoreElements.return_value = False
    ctx = MagicMock(doc=doc)

    res = SearchInDocument().execute(ctx, pattern="hit")
    assert res["status"] == "ok"
    assert res["returned"] == 20
    assert res["count"] == 25
    assert "truncated" not in res
    assert "hint" in res
    assert "25" in res["hint"]


def test_find_next_after_match_stops_on_empty_string():
    from plugin.writer.search import find_next_after_match

    doc = MagicMock()
    found = MagicMock()
    found.getString.return_value = ""
    sd = MagicMock()
    res = find_next_after_match(doc, found, sd)
    assert res is None
    doc.findNext.assert_not_called()

    # Second case: getString raises, find_next_after_match propagates
    doc.reset_mock()
    found = MagicMock()
    found.getString.side_effect = RuntimeError("fail")
    with pytest.raises(RuntimeError, match="fail"):
        find_next_after_match(doc, found, sd)
    doc.findNext.assert_not_called()


def test_find_ranges_regex_case_stops_after_one_empty_match():
    from plugin.writer.search import find_ranges_regex_case

    doc = MagicMock()
    found = MagicMock()
    found.getString.return_value = ""
    doc.findFirst.return_value = found
    out = find_ranges_regex_case(doc, "^", True, False, all_matches=True)
    assert out == [found]
    doc.findNext.assert_not_called()


def test_shape_text_hits_case_insensitive_after_dotted_capital_i():
    from plugin.writer.search import shape_text_hits

    assert shape_text_hits("İa", "a", False, False) == ["a"]
    assert shape_text_hits("İ", "i", False, False) == ["İ"]
    assert shape_text_hits("İstanbul", "stan", False, False) == ["stan"]
    assert shape_text_hits("İa", "a", False, True) == ["a"]


def test_shape_text_hits_regex_drops_empty_groups_only():
    from plugin.writer.search import shape_text_hits

    assert shape_text_hits("ab", r"a*", True, True) == ["a"]


def test_shape_text_spans_and_hits_regex():
    from plugin.writer.search import (
        iter_shape_text_spans,
        shape_text_hits,
        shape_text_span,
    )

    assert list(iter_shape_text_spans("say hello!", "hel+o", case_sensitive=False, use_regex=True)) == [(4, 9)]
    assert shape_text_span("say hello!", "hel+o", case_sensitive=False, use_regex=True) == (4, 9)
    assert shape_text_hits("say hello!", "hel+o", True, False) == ["hello"]

    assert list(iter_shape_text_spans("say hello!", "h.+o", case_sensitive=False, use_regex=True)) == [(4, 9)]
    assert shape_text_span("say hello!", "h.+o", case_sensitive=False, use_regex=True) == (4, 9)
    assert shape_text_hits("say hello!", "h.+o", True, False) == ["hello"]

    assert shape_text_span("say hello!", "Hel+o", case_sensitive=True, use_regex=True) is None
    assert shape_text_hits("say hello!", "Hel+o", True, True) == []
    assert shape_text_span("say hello!", "Hel+o", case_sensitive=False, use_regex=True) == (4, 9)
    assert shape_text_hits("say hello!", "Hel+o", True, False) == ["hello"]

    assert list(iter_shape_text_spans("ab", r"a*", case_sensitive=True, use_regex=True)) == [(0, 1)]
    assert shape_text_span("ab", r"a*", case_sensitive=True, use_regex=True) == (0, 1)

    assert list(iter_shape_text_spans("ab", r"b*", case_sensitive=True, use_regex=True)) == [(1, 2)]
    assert shape_text_span("ab", r"b*", case_sensitive=True, use_regex=True) == (1, 2)
    assert shape_text_hits("ab", r"b*", True, True) == ["b"]

    assert list(iter_shape_text_spans("hello", r"x*", case_sensitive=True, use_regex=True)) == []
    assert shape_text_span("hello", r"x*", case_sensitive=True, use_regex=True) is None
    assert shape_text_hits("hello", r"x*", True, True) == []

    assert list(iter_shape_text_spans("hello", r"^", case_sensitive=True, use_regex=True)) == []
    assert shape_text_span("hello", r"^", case_sensitive=True, use_regex=True) is None
    assert shape_text_hits("hello", r"^", True, True) == []

    assert shape_text_hits("İstanbul", "i", True, False) == ["İ"]
    assert shape_text_span("İstanbul", "i", case_sensitive=False, use_regex=True) == (0, 1)

    assert shape_text_hits("He said \u201cHi,\u201d", '"Hi,"', True, False) == []
    assert shape_text_span("He said \u201cHi,\u201d", '"Hi,"', case_sensitive=False, use_regex=True) is None


def test_shape_text_hits_folds_quotes_and_returns_document_text():
    from plugin.writer.search import (
        _DOUBLE_QUOTE_CODEPOINTS,
        _SINGLE_QUOTE_CODEPOINTS,
        shape_text_hits,
        shape_text_span,
    )

    assert shape_text_hits("He said \u201cHi,\u201d", '"hi,"', False, False) == ["\u201cHi,\u201d"]
    assert shape_text_hits("He said \u201cHi,\u201d", '"Hi,"', False, True) == ["\u201cHi,\u201d"]
    assert shape_text_hits('He said "Hi,"', "\u201chi,\u201d", False, False) == ['"Hi,"']
    assert shape_text_hits("He said \u201cHi,\u201d", '"Hi,"', True, True) == []
    assert shape_text_hits("end\u2014ash", "end-ash", False, False) == []
    assert shape_text_hits("end\u2014ash", "end-ash", False, True) == []
    assert shape_text_hits("İ“x”", '"X"', False, False) == ["“x”"]

    assert shape_text_span("He said \u201cHi,\u201d", '"hi,"', False) == (8, 13)
    assert shape_text_span("He said \u201cHi,\u201d", '"Hi,"', True) == (8, 13)
    assert shape_text_span("He said \u201cHi,\u201d", '"hi,"', True) is None
    assert shape_text_span("İ“x”", '"X"', False) == (1, 4)

    for cp in _SINGLE_QUOTE_CODEPOINTS:
        ch = chr(cp)
        assert shape_text_hits(f"a{ch}b", "a'b", False, True) == [f"a{ch}b"]
        assert shape_text_hits(f"a{ch}b", "a'b", False, False) == [f"a{ch}b"]

    for cp in _DOUBLE_QUOTE_CODEPOINTS:
        ch = chr(cp)
        assert shape_text_hits(f'a{ch}b', 'a"b', False, True) == [f'a{ch}b']
        assert shape_text_hits(f'a{ch}b', 'a"b', False, False) == [f'a{ch}b']

    for cp in (0x2032, 0x02BC, 0x00AB):
        ch = chr(cp)
        assert shape_text_hits(f"a{ch}b", "a'b", False, False) == []
        assert shape_text_hits(f'a{ch}b', 'a"b', False, False) == []


def test_find_text_ranges_limit_zero_and_bad_types_return_empty():
    from plugin.writer.search import find_text_ranges

    for bad_limit in (0, -3, "1", True):
        model = MagicMock()
        res = find_text_ranges(model, "foo", limit=bad_limit)
        assert res == []
        model.createSearchDescriptor.assert_not_called()

    model = MagicMock()
    model.findNext.return_value = None
    res = find_text_ranges(model, "foo", limit=None)
    model.createSearchDescriptor.assert_called_once()


# Offsets are measured on the visible text; these fakes have no tracked deletions.
@patch("plugin.writer.search.get_string_without_tracked_deletions", side_effect=lambda r: r.getString())
def test_find_text_ranges_body_only_skips_other_xtext(_visible):
    from plugin.writer.search import find_text_ranges

    class BodyTextObj:
        def createTextCursor(self):
            c = MagicMock()
            c.getString.return_value = "pre"
            return c

    class OtherTextObj:
        def createTextCursor(self):
            c = MagicMock()
            c.getString.return_value = "nested_pre"
            return c

    b_obj = BodyTextObj()
    o_obj = OtherTextObj()

    model = MagicMock()
    model.getText.return_value = b_obj

    hit_body = MagicMock()
    hit_body.getText.return_value = b_obj
    hit_body.getString.return_value = "hit1"

    hit_nested = MagicMock()
    hit_nested.getText.return_value = o_obj
    hit_nested.getString.return_value = "hit2"

    # body_only=True -> only body hit
    model.findNext.side_effect = [hit_body, hit_nested, None]
    res_body_only = find_text_ranges(model, "hit", body_only=True)
    assert len(res_body_only) == 1
    assert res_body_only[0]["text"] == "hit1"

    # body_only=False -> both
    model.findNext.side_effect = [hit_body, hit_nested, None]
    res_all = find_text_ranges(model, "hit", body_only=False)
    assert len(res_all) == 2
    assert res_all[0]["text"] == "hit1"
    assert res_all[1]["text"] == "hit2"
    assert res_all[1]["start"] == len("nested_pre")


def test_build_page_map_rebuilds_and_unlocks(caplog):
    import logging
    from plugin.writer.search import _build_page_map

    doc = MagicMock()
    controller = MagicMock()
    vc = MagicMock()
    doc.getCurrentController.return_value = controller
    controller.getViewCursor.return_value = vc

    para = MagicMock()
    enum = MagicMock()
    enum.hasMoreElements.side_effect = [True, False, True, False]
    enum.nextElement.return_value = para
    doc.getText.return_value.createEnumeration.return_value = enum
    vc.getPage.return_value = 3

    # Call twice -> rebuilt twice, no cache
    pm1 = _build_page_map(doc)
    pm2 = _build_page_map(doc)
    assert pm1 == {0: 3}
    assert pm2 == {0: 3}
    assert doc.getText.return_value.createEnumeration.call_count == 2
    assert doc.lockControllers.call_count == 2
    assert doc.unlockControllers.call_count == 2

    # gotoRange raises on restore
    doc.reset_mock()
    enum = MagicMock()
    enum.hasMoreElements.side_effect = [True, False]
    enum.nextElement.return_value = para
    doc.getText.return_value.createEnumeration.return_value = enum
    vc.gotoRange.side_effect = [None, None, RuntimeError("restore failed")]
    vc.getPage.return_value = 5

    pm3 = _build_page_map(doc)
    assert pm3 == {0: 5}
    doc.unlockControllers.assert_called_once()

    # Controller failure returns {} and does not lock, logs exception
    doc.reset_mock()
    doc.getCurrentController.side_effect = RuntimeError("no controller")
    with caplog.at_level(logging.ERROR, logger="writeragent.writer"):
        pm_err = _build_page_map(doc)
    assert pm_err == {}
    doc.lockControllers.assert_not_called()
    assert any(r.levelno >= logging.ERROR and r.exc_info for r in caplog.records)


def test_advanced_search_missing_query():
    from plugin.writer.search import AdvancedSearch

    idx_svc = MagicMock()
    ctx = MagicMock()
    ctx.services.writer_index = idx_svc

    tool = AdvancedSearch()
    for bad_q in ({}, {"query": None}, {"query": "  "}, {"query": 1}):
        res = tool.execute(ctx, **bad_q)
        assert res["status"] == "error"
        assert res["code"] == "INVALID_PARAM"
        assert "query is required" in res["message"]
        idx_svc.search_boolean.assert_not_called()

    idx_svc.search_boolean.return_value = {"matches": []}
    res_ok = tool.execute(ctx, query="climate")
    assert res_ok["status"] == "ok"
    idx_svc.search_boolean.assert_called_once()


def test_advanced_search_invalid_syntax_returns_invalid_param():
    from plugin.writer.search import AdvancedSearch

    idx_svc = MagicMock()
    idx_svc.search_boolean.side_effect = ValueError("Boolean query syntax error near: AND")
    ctx = MagicMock()
    ctx.services.writer_index = idx_svc

    tool = AdvancedSearch()
    res = tool.execute(ctx, query="AND")
    assert res["status"] == "error"
    assert res["code"] == "INVALID_PARAM"
    assert "Boolean query syntax error" in res["message"]


def test_search_in_document_missing_pattern_returns_invalid_param():
    from plugin.writer.search import SearchInDocument

    ctx = MagicMock()
    tool = SearchInDocument()
    res = tool.execute(ctx, pattern="")
    assert res["status"] == "error"
    assert res["code"] == "INVALID_PARAM"
    assert "pattern is required" in res["message"]


def test_build_search_not_found_response_codes():
    from plugin.writer.search import build_search_not_found_response

    doc = MagicMock()
    doc.getDrawPage.return_value = None

    # Standard miss
    res_standard = build_search_not_found_response(doc, "needle")
    assert res_standard["status"] == "error"
    assert res_standard["code"] == "OLD_CONTENT_NOT_FOUND"
    assert res_standard["replaced_count"] == 0

    # Custom subject miss
    res_subject = build_search_not_found_response(doc, "needle", subject="search_text")
    assert res_subject["message"].startswith("search_text not found")

    # Shape miss
    with patch("plugin.writer.search.drawing_shape_containing", return_value="Box1"):
        res_shape = build_search_not_found_response(doc, "needle")
        assert res_shape["status"] == "error"
        assert res_shape["code"] == "SHAPE_TEXT_NEEDS_DELEGATION"
        assert res_shape["replaced_count"] == 0
        assert "Box1" in res_shape["message"]
        assert "old_content" in res_shape["message"]

        # Review mode shape miss
        res_review = build_search_not_found_response(doc, "needle", review=True)
        assert "In review mode" in res_review["message"]

    # Oversized refusal (opt-in via refuse_oversized=True)
    doc_oversized = MagicMock()
    doc_oversized.getDrawPage.return_value = None
    doc_oversized.getText.return_value.getString.return_value = "short"
    with patch("plugin.writer.search.old_content_covers_most", return_value=True):
        res_oversized = build_search_not_found_response(doc_oversized, "longneedle", refuse_oversized=True)
        res_non_oversized = build_search_not_found_response(doc_oversized, "longneedle")
        res_non_old_content = build_search_not_found_response(doc_oversized, "longneedle", subject="search_text")
    assert res_oversized["status"] == "error"
    assert res_oversized["code"] == "OLD_CONTENT_TOO_LARGE"
    assert res_oversized["oversized"] is True
    assert res_oversized["replaced_count"] == 0
    assert res_non_oversized["code"] == "OLD_CONTENT_NOT_FOUND"
    assert res_non_old_content["code"] == "OLD_CONTENT_NOT_FOUND"


def test_search_hit_cap_includes_shapes():
    from unittest.mock import patch
    from plugin.writer.search import SearchInDocument

    doc = MagicMock()
    body_hit = MagicMock(**{"getString.return_value": "hit_body"})
    doc.findFirst.return_value = body_hit
    doc.findNext.return_value = None

    # Floating shape
    shape = MagicMock()
    shape.getString.return_value = "hit_s1 hit_s2 hit_s3"
    shape.getPropertyValue.return_value = 0  # AnchorType not as-char, TextBox false
    shape.supportsService.return_value = False
    doc.getDrawPage.return_value.getCount.return_value = 1
    doc.getDrawPage.return_value.getByIndex.return_value = shape

    del doc.getTextFields

    ctx = MagicMock(doc=doc)
    with patch("plugin.writer.search.MAX_SEARCH_HITS", 2):
        res = SearchInDocument().execute(ctx, pattern="hit", max_results=10)

    assert res["status"] == "ok"
    assert res["count"] == 2
    assert res["truncated"] is True
    assert res["warning"] == "Search stopped after 2 matches; count may be incomplete."


def test_find_text_ranges_does_not_report_failure_as_no_hits():
    from plugin.writer.search import find_text_ranges

    model = MagicMock()
    model.createSearchDescriptor.side_effect = RuntimeError("search dead")
    with pytest.raises(RuntimeError, match="search dead"):
        find_text_ranges(model, "foo")


def test_return_offsets_does_not_report_failure_as_zero_hits():
    from plugin.framework.errors import DocumentDisposedError
    from plugin.writer.search import SearchInDocument

    doc = MagicMock()
    doc.createSearchDescriptor.side_effect = DocumentDisposedError("closed", object_type="Document")
    ctx = MagicMock(doc=doc)
    with pytest.raises(DocumentDisposedError):
        SearchInDocument().execute(ctx, pattern="hello", return_offsets=True)


def test_shape_text_hits_regex_timeout():
    import threading
    from plugin.writer import search as search_mod

    started = threading.Event()
    release = threading.Event()

    def _slow(*_args, **_kwargs):
        started.set()
        release.wait(2)
        return iter(())

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(search_mod.re_mod, "finditer", _slow)
    monkeypatch.setattr(search_mod, "_SHAPE_REGEX_TIMEOUT_SEC", 0.05)
    search_mod._shape_regex_inflight = None
    try:
        assert search_mod.shape_text_hits("abc", "a+", True, True) == []
        assert started.wait(1)
    finally:
        release.set()
        handle = search_mod._shape_regex_inflight
        if handle is not None:
            handle.join(2)
        search_mod._shape_regex_inflight = None
        monkeypatch.undo()


def test_find_first_range_chain_combine_failure_propagates():
    from plugin.writer.search import find_first_range

    doc = MagicMock()
    found = MagicMock()
    found.getString.return_value = "Hello"
    text = MagicMock()
    found.getText.return_value = text
    n = {"i": 0}

    def _make(_arg):
        n["i"] += 1
        cursor = MagicMock()
        cursor.getStart.return_value.getText.return_value = text
        cursor.getEnd.return_value.getText.return_value = text
        if n["i"] == 8:
            raise RuntimeError("combine failed")
        return cursor

    text.createTextCursorByRange.side_effect = _make
    doc.findFirst.return_value = found
    with patch("plugin.writer.search._iter_lo_matches", return_value=iter(())), \
         patch("plugin.writer.search.get_string_without_tracked_deletions", side_effect=["Hello", "World"]):
        with pytest.raises(RuntimeError, match="combine failed"):
            find_first_range(doc, "Hello\nWorld")


def test_find_first_range_chain_cross_text_is_skipped():
    from plugin.writer.search import find_first_range

    doc = MagicMock()
    found = MagicMock()
    found.getString.return_value = "Hello"
    text = MagicMock()
    found.getText.return_value = text
    doc.findNext.return_value = None
    doc.findFirst.side_effect = [found, None]
    body = object()
    other = object()
    n = {"i": 0}

    def _make(_arg):
        n["i"] += 1
        cursor = MagicMock()
        if n["i"] == 3:
            cursor.getStart.return_value.getText.return_value = body
        if n["i"] == 6:
            cursor.getEnd.return_value.getText.return_value = other
        return cursor

    text.createTextCursorByRange.side_effect = _make
    with patch("plugin.writer.search._iter_lo_matches", return_value=iter(())), \
         patch("plugin.writer.search.get_string_without_tracked_deletions", side_effect=["Hello", "World"]):
        assert find_first_range(doc, "Hello\nWorld") is None
    assert n["i"] == 7


def test_occurrence_range_or_error_payloads():
    from plugin.writer.search import occurrence_range_or_error

    doc = MagicMock()
    doc.getDrawPage.return_value = None

    # Bad occurrence
    rng, err = occurrence_range_or_error(doc, "text", "bad", case_sensitive=False, subject="test_param")
    assert rng is None
    assert err is not None
    assert err["code"] == "INVALID_PARAM"

    # Out of range (IndexError)
    with patch("plugin.writer.search.find_occurrence_range", side_effect=IndexError("occurrence 2 out of range")):
        rng, err = occurrence_range_or_error(doc, "text", 2, case_sensitive=False, subject="test_param")
        assert rng is None
        assert err is not None
        assert err["code"] == "OCCURRENCE_OUT_OF_RANGE"
        assert "occurrence 2 out of range" in err["message"]

    # Not found (None)
    with patch("plugin.writer.search.find_occurrence_range", return_value=None):
        rng, err = occurrence_range_or_error(doc, "text", 0, case_sensitive=True, subject="test_param")
        assert rng is None
        assert err is not None
        assert err["code"] == "OLD_CONTENT_NOT_FOUND"
        assert err["message"].startswith("test_param not found in document")

    # Hit
    mock_found = MagicMock()
    with patch("plugin.writer.search.find_occurrence_range", return_value=mock_found) as mock_find:
        rng, err = occurrence_range_or_error(doc, "text", "1", case_sensitive=True, subject="test_param")
        assert rng is mock_found
        assert err is None
        mock_find.assert_called_once_with(doc, "text", occurrence=1, case_sensitive=True)


def test_uno_read_probes_raise_on_disposal_and_default_otherwise():
    from plugin.framework.errors import DocumentDisposedError
    from plugin.writer.search import _prop, _safe_name, describe_match_location, enclosing_paragraph_text

    class DisposedException(Exception):
        pass

    class RuntimeException(Exception):
        pass

    closed = MagicMock()
    closed.getText.side_effect = DisposedException("closed")
    with pytest.raises(DocumentDisposedError):
        describe_match_location(closed, MagicMock())

    odd = MagicMock()
    odd.getText.side_effect = RuntimeException("uno")
    odd.getString.return_value = "raw"
    assert describe_match_location(odd, MagicMock()) == "body"
    assert enclosing_paragraph_text(odd) == "raw"

    # getName/getPropertyValue failures still fall back to the attribute.
    obj = MagicMock()
    obj.getName.side_effect = RuntimeException("uno")
    obj.Name = "Shape 1"
    assert _safe_name(obj) == "Shape 1"
    obj.getPropertyValue.side_effect = RuntimeException("uno")
    obj.CellName = "B2"
    assert _prop(obj, "CellName") == "B2"


def test_find_occurrence_range_repeated_text_and_past_end():
    from plugin.writer.search import find_occurrence_range

    doc = MagicMock()
    sd = MagicMock()
    doc.createSearchDescriptor.return_value = sd

    first = MagicMock()
    first.getString.return_value = "hit"
    second = MagicMock()
    second.getString.return_value = "hit"

    doc.findFirst.return_value = first
    doc.findNext.side_effect = [second, None]

    # occurrence 0
    res0 = find_occurrence_range(doc, "hit", occurrence=0)
    assert res0 is first
    doc.findNext.assert_not_called()

    # occurrence 1
    doc.findFirst.return_value = first
    doc.findNext.side_effect = [second, None]
    res1 = find_occurrence_range(doc, "hit", occurrence=1)
    assert res1 is second

    # occurrence past end
    doc.findFirst.return_value = first
    doc.findNext.side_effect = [second, None]
    with pytest.raises(IndexError, match=r"occurrence 5 out of range \(found 2 match\(es\), use 0\.\.1\)\."):
        find_occurrence_range(doc, "hit", occurrence=5)


def test_find_occurrence_range_case_sensitive_flags():
    from plugin.writer.search import find_occurrence_range

    doc = MagicMock()
    sd = MagicMock()
    doc.createSearchDescriptor.return_value = sd
    hit = MagicMock()
    hit.getString.return_value = "Word"
    doc.findFirst.return_value = hit

    # case_sensitive = True
    find_occurrence_range(doc, "Word", case_sensitive=True)
    assert sd.SearchCaseSensitive is True

    # case_sensitive = False
    find_occurrence_range(doc, "Word", case_sensitive=False)
    assert sd.SearchCaseSensitive is False


def test_find_occurrence_range_escapes_literal_dot():
    from plugin.writer.search import find_occurrence_range

    doc = MagicMock()
    sd = MagicMock()
    doc.createSearchDescriptor.return_value = sd
    doc.findFirst.return_value = None

    find_occurrence_range(doc, "file.txt")
    assert r"\." in sd.SearchString


def test_find_occurrence_range_normalizes_nbsp_and_multispace():
    from plugin.writer.search import find_occurrence_range

    doc = MagicMock()
    sd = MagicMock()
    doc.createSearchDescriptor.return_value = sd
    doc.findFirst.return_value = None

    find_occurrence_range(doc, "hello\u00a0world    again")
    assert sd.SearchRegularExpression is True
    assert "hello" in sd.SearchString and "world" in sd.SearchString


def test_find_occurrence_range_newline_passage():
    from plugin.writer.search import find_occurrence_range

    doc = MagicMock()
    sd = MagicMock()
    doc.createSearchDescriptor.return_value = sd

    doc.findFirst.side_effect = [None, None, None]

    assert find_occurrence_range(doc, "Para 1\nPara 2") is None


def test_find_first_range_default_case_insensitive():
    from plugin.writer.search import find_first_range

    doc = MagicMock()
    sd = MagicMock()
    doc.createSearchDescriptor.return_value = sd

    hit = MagicMock()
    hit.getString.return_value = "lower"

    doc.findFirst.return_value = hit

    res = find_first_range(doc, "LOWER")
    assert res is hit
    assert sd.SearchCaseSensitive is False
    assert doc.findFirst.call_count == 1


def test_parse_non_negative_int():
    from plugin.writer.search import parse_non_negative_int

    # None returns None, None
    assert parse_non_negative_int(None) == (None, None)

    # Without coerce: only strict non-negative int
    assert parse_non_negative_int(0) == (0, None)
    assert parse_non_negative_int(2) == (2, None)
    assert parse_non_negative_int(-1) == (None, "occurrence must be a non-negative integer.")
    assert parse_non_negative_int("2") == (None, "occurrence must be a non-negative integer.")
    assert parse_non_negative_int(1.0) == (None, "occurrence must be a non-negative integer.")
    assert parse_non_negative_int("") == (None, "occurrence must be a non-negative integer.")

    # With coerce=True: int() coercion allowed for comments, footnotes, page style
    assert parse_non_negative_int("2", coerce=True) == (2, None)
    assert parse_non_negative_int(1.0, coerce=True) == (1, None)
    assert parse_non_negative_int("", coerce=True) == (0, None)
    assert parse_non_negative_int(0, coerce=True) == (0, None)
    assert parse_non_negative_int(5, coerce=True) == (5, None)
    assert parse_non_negative_int(-1, coerce=True) == (None, "occurrence must be a non-negative integer.")
    assert parse_non_negative_int("-2", coerce=True) == (None, "occurrence must be a non-negative integer.")
    assert parse_non_negative_int("abc", coerce=True) == (None, "occurrence must be a non-negative integer.")


def test_search_in_document_disposed_get_string_propagates():
    from plugin.framework.errors import DocumentDisposedError
    from plugin.writer.search import SearchInDocument

    doc = MagicMock()
    found = MagicMock()
    found.getString.side_effect = DocumentDisposedError("gone")
    doc.findFirst.return_value = found
    res = SearchInDocument().execute_safe(MagicMock(doc=doc), pattern="x")
    assert res.get("status") == "error"
    assert res.get("code") == "DOCUMENT_DISPOSED"


def test_iter_floating_shape_texts_disposed_draw_page_propagates():
    from plugin.framework.errors import DocumentDisposedError
    from plugin.writer.search import iter_floating_shape_texts

    doc = MagicMock()
    doc.getDrawPage.side_effect = DocumentDisposedError("gone")
    with pytest.raises(DocumentDisposedError):
        list(iter_floating_shape_texts(doc))


def test_comment_matches_disposed_enumeration_propagates():
    from plugin.framework.errors import DocumentDisposedError
    from plugin.writer.search import iter_comment_hits

    doc = MagicMock()
    doc.getTextFields.return_value.createEnumeration.side_effect = DocumentDisposedError("gone")
    with pytest.raises(DocumentDisposedError):
        list(iter_comment_hits(doc, "x", False, False))


def test_header_footer_label_disposed_style_walk_propagates():
    from plugin.framework.errors import DocumentDisposedError
    from plugin.writer.search import _header_footer_label

    fake_text = MagicMock()
    fake_text.ImplementationName = "SwXHeadFootText"
    doc = MagicMock()
    doc.getStyleFamilies.side_effect = DocumentDisposedError("gone")
    with pytest.raises(DocumentDisposedError):
        _header_footer_label(fake_text, doc)


def test_search_in_document_and_sweep_shape_matches_parity():
    from plugin.writer.search import SearchInDocument, sweep_draw_shape_preview_matches

    doc = MagicMock()
    doc.findFirst.return_value = None
    shape = MagicMock()
    shape.getString.return_value = "needle here"
    shape.getPropertyValue.return_value = 0
    shape.supportsService.return_value = False
    shape.Name = "Box 1"
    draw_page = MagicMock()
    draw_page.getCount.return_value = 1
    draw_page.getByIndex.return_value = shape
    doc.getDrawPage.return_value = draw_page
    del doc.getTextFields

    ctx = MagicMock(doc=doc)
    tool_matches = SearchInDocument().execute(ctx, pattern="needle", regex=False, case_sensitive=False)["matches"]
    sweep_matches = sweep_draw_shape_preview_matches(doc, "needle", False, False)

    tool_pairs = [(m["text"], m["location"]) for m in tool_matches]
    sweep_pairs = [(m["text"], m["location"]) for m in sweep_matches]
    assert tool_pairs == sweep_pairs == [("needle", "shape 'Box 1'")]


def test_search_in_document_and_sweep_comment_matches_parity():
    from plugin.writer.search import SearchInDocument, sweep_comment_preview_matches

    doc = MagicMock()
    draw_page = MagicMock()
    draw_page.getCount.return_value = 0
    doc.getDrawPage.return_value = draw_page
    doc.findFirst.return_value = None

    field = MagicMock()
    field.supportsService.return_value = True
    field.Content = "needle in comment"
    field.Author = "Ana"

    def make_enum():
        enum = MagicMock()
        enum.hasMoreElements.side_effect = [True, False]
        enum.nextElement.return_value = field
        return enum

    doc.getTextFields.return_value.createEnumeration.side_effect = make_enum

    ctx = MagicMock(doc=doc)
    tool_matches = SearchInDocument().execute(ctx, pattern="needle", regex=False, case_sensitive=False)["matches"]
    sweep_matches = sweep_comment_preview_matches(doc, "needle", False, False)

    tool_pairs = [(m["text"], m["location"]) for m in tool_matches]
    sweep_pairs = [(m["text"], m["location"]) for m in sweep_matches]
    assert tool_pairs == sweep_pairs == [("needle", "comment by 'Ana'")]


def test_search_in_document_reads_each_hit_once():
    from plugin.writer.search import SearchInDocument

    doc = MagicMock()
    found = MagicMock()
    found.getString.return_value = "hit"
    doc.findFirst.return_value = found
    doc.findNext.return_value = None
    del doc.getDrawPage
    del doc.getTextFields

    ctx = MagicMock(doc=doc)
    res = SearchInDocument().execute(ctx, pattern="hit", regex=False, case_sensitive=False)
    assert res["status"] == "ok"
    assert res["count"] == 1
    found.getString.assert_called_once()


def test_build_page_map_propagates_disposed_document():
    from plugin.framework.errors import DocumentDisposedError
    from plugin.writer.search import _build_page_map

    doc = MagicMock()
    enum = doc.getText.return_value.createEnumeration.return_value
    enum.hasMoreElements.side_effect = [True, False]
    vc = doc.getCurrentController.return_value.getViewCursor.return_value
    vc.getPage.side_effect = type("DisposedException", (Exception,), {})("gone")
    with pytest.raises(DocumentDisposedError):
        _build_page_map(doc)


def test_pick_end_after_start_selection():
    from plugin.writer.search import pick_end_after_start

    shared_text = MagicMock()
    start_range = MagicMock()
    start_range.getText.return_value = shared_text
    start_boundary = MagicMock()
    start_boundary.getText.return_value = shared_text
    start_range.getEnd.return_value = start_boundary
    start_point = MagicMock()
    start_range.getStart.return_value = start_point

    # compareRegionStarts returns 1 (starts before boundary) then -1 (starts after boundary).
    # The first candidate (e.g. from chaining) is skipped, second range wins.
    cand1 = MagicMock()
    cand1.getText.return_value = shared_text
    cand2 = MagicMock()
    cand2.getText.return_value = shared_text
    end2_point = MagicMock()
    cand2.getEnd.return_value = end2_point

    shared_text.compareRegionStarts.side_effect = [1, -1]
    created_cursor = MagicMock()
    shared_text.createTextCursorByRange.return_value = created_cursor

    cursor, err = pick_end_after_start(start_range, [cand1, cand2])
    assert err is None
    assert cursor is created_cursor
    shared_text.createTextCursorByRange.assert_called_with(start_point)
    created_cursor.gotoRange.assert_called_with(end2_point, True)


def test_pick_end_after_start_zero_accepted():
    from plugin.writer.search import pick_end_after_start

    shared_text = MagicMock()
    start_range = MagicMock()
    start_range.getText.return_value = shared_text
    start_boundary = MagicMock()
    start_boundary.getText.return_value = shared_text
    start_range.getEnd.return_value = start_boundary
    start_range.getStart.return_value = MagicMock()

    cand = MagicMock()
    cand.getText.return_value = shared_text
    shared_text.compareRegionStarts.return_value = 0
    created_cursor = MagicMock()
    shared_text.createTextCursorByRange.return_value = created_cursor

    cursor, err = pick_end_after_start(start_range, [cand])
    assert err is None
    assert cursor is created_cursor


def test_pick_end_after_start_different_xtext_skips_to_valid_candidate():
    from plugin.writer.search import pick_end_after_start

    start_text = MagicMock()
    different_text = MagicMock()
    start_range = MagicMock()
    start_range.getText.return_value = start_text
    start_boundary = MagicMock()
    start_boundary.getText.return_value = start_text
    start_range.getEnd.return_value = start_boundary

    cand1 = MagicMock()
    cand1.getText.return_value = different_text
    cand2 = MagicMock()
    cand2.getText.return_value = start_text
    start_text.compareRegionStarts.return_value = 0
    created_cursor = MagicMock()
    start_text.createTextCursorByRange.return_value = created_cursor

    ends = iter([cand1, cand2])
    cursor, err = pick_end_after_start(start_range, ends)
    assert cursor is created_cursor
    assert err is None


def test_pick_end_after_start_different_xtext_stops():
    from plugin.writer.search import pick_end_after_start

    start_text = MagicMock()
    different_text = MagicMock()
    start_range = MagicMock()
    start_range.getText.return_value = start_text
    start_boundary = MagicMock()
    start_boundary.getText.return_value = start_text
    start_range.getEnd.return_value = start_boundary

    cand1 = MagicMock()
    cand1.getText.return_value = different_text

    ends = iter([cand1])
    cursor, err = pick_end_after_start(start_range, ends)
    assert cursor is None
    assert err == "different_text"


def test_find_anchor_span_missing_start():
    from unittest.mock import patch
    from plugin.writer.search import find_anchor_span

    doc = MagicMock()
    with patch("plugin.writer.search.find_occurrence_range", return_value=None):
        cursor, err = find_anchor_span(doc, "missing", "end")
        assert cursor is None
        assert err == "start"


def test_pick_end_after_start_empty_iterator():
    from plugin.writer.search import pick_end_after_start

    start_range = MagicMock()
    cursor, err = pick_end_after_start(start_range, [])
    assert cursor is None
    assert err == "end"


def test_pick_end_after_start_compare_error_treated_as_end():
    from plugin.writer.search import pick_end_after_start

    shared_text = MagicMock()
    start_range = MagicMock()
    start_range.getText.return_value = shared_text
    start_boundary = MagicMock()
    start_boundary.getText.return_value = shared_text
    start_range.getEnd.return_value = start_boundary

    cand = MagicMock()
    cand.getText.return_value = shared_text
    shared_text.compareRegionStarts.side_effect = RuntimeError("compare failed")

    cursor, err = pick_end_after_start(start_range, [cand])
    assert cursor is None
    assert err == "end"


def test_iter_descriptor_hits_with_after():
    from plugin.writer.search import _iter_descriptor_hits

    doc = MagicMock()
    sd = MagicMock()
    after_range = MagicMock()
    first_hit = MagicMock()
    first_hit.getString.return_value = "hit1"
    end_range = MagicMock()
    first_hit.getEnd.return_value = end_range
    doc.findNext.side_effect = [first_hit, None]

    hits = list(_iter_descriptor_hits(doc, sd, after=after_range))
    assert hits == [first_hit]
    doc.findFirst.assert_not_called()
    doc.findNext.assert_any_call(after_range, sd)


def test_iter_literal_ranges_yields_candidate_matches():
    from plugin.writer.search import iter_literal_ranges

    doc = MagicMock()
    hit1 = MagicMock()
    hit1.getString.return_value = "hello"
    hit1.getEnd.return_value = MagicMock()
    doc.findFirst.return_value = hit1
    doc.findNext.return_value = None

    hits = list(iter_literal_ranges(doc, "hello", case_sensitive=True))
    assert hits == [hit1]


def test_iter_literal_ranges_all_candidates_yields_all():
    from unittest.mock import patch
    from plugin.writer.search import iter_literal_ranges

    doc = MagicMock()
    frame_hit = MagicMock()
    body_hit = MagicMock()
    chain_hit = MagicMock()

    def fake_iter_lo(doc, candidate, case_sensitive=False, after=None):
        if "\n" in candidate:
            yield frame_hit
        else:
            yield body_hit

    with patch("plugin.writer.search._iter_lo_matches", side_effect=fake_iter_lo), \
         patch("plugin.writer.search._iter_chain_matches", return_value=iter([chain_hit])):
        # 1. all_candidates=False: stops on the first candidate (yielding the frame hit)
        hits_default = list(iter_literal_ranges(doc, "Hello\nWorld", all_candidates=False))
        assert hits_default == [frame_hit]

        # 2. all_candidates=True: yields hits across all candidates, then chain matches
        hits_all = list(iter_literal_ranges(doc, "Hello\nWorld", all_candidates=True))
        assert hits_all == [frame_hit, body_hit, chain_hit]


def test_find_anchor_span_skips_foreign_candidate_to_find_same_text():
    from unittest.mock import patch
    from plugin.writer.search import find_anchor_span

    doc = MagicMock()
    body_text = MagicMock()
    frame_text = MagicMock()

    start_match = MagicMock()
    start_match.getText.return_value = body_text
    start_match.getStart.return_value = MagicMock()
    start_boundary = MagicMock()
    start_boundary.getText.return_value = body_text
    start_match.getEnd.return_value = start_boundary

    frame_hit = MagicMock()
    frame_hit.getText.return_value = frame_text

    body_hit = MagicMock()
    body_hit.getText.return_value = body_text
    body_hit.getStart.return_value = MagicMock()
    body_hit.getEnd.return_value = MagicMock()

    body_text.compareRegionStarts.return_value = 0
    created_cursor = MagicMock()
    body_text.createTextCursorByRange.return_value = created_cursor

    def fake_iter_lo(doc, candidate, case_sensitive=False, after=None):
        if "\n" in candidate:
            yield frame_hit
        else:
            yield body_hit

    with patch("plugin.writer.search.find_occurrence_range", return_value=start_match), \
         patch("plugin.writer.search._iter_lo_matches", side_effect=fake_iter_lo):
        cursor, err = find_anchor_span(doc, "start", "Hello\nWorld")
        assert err is None
        assert cursor is created_cursor


class _SampleFakeCursor:
    def __init__(self, doc, pos=0):
        self.doc = doc
        self.pos = pos
        self.end_pos = pos

    def gotoStart(self, select=False):
        self.pos = 0
        if not select:
            self.end_pos = 0

    def gotoRange(self, r, select=False):
        target = r.pos if hasattr(r, "pos") else r
        if select:
            self.end_pos = target
        else:
            self.pos = target
            self.end_pos = target

    def getString(self):
        return self.doc.text[self.pos:self.end_pos]

    def goLeft(self, count=1, select=False):
        return False

    def goRight(self, count=1, select=False):
        return False

    def getPropertyValue(self, name):
        return None


class _SampleFakeRange:
    def __init__(self, doc, start, end):
        self.doc = doc
        self.start = start
        self.end = end

    @property
    def pos(self):
        return self.start

    def getString(self):
        return self.doc.text[self.start:self.end]

    def getStart(self):
        return _SampleFakeCursor(self.doc, self.start)

    def getEnd(self):
        return _SampleFakeCursor(self.doc, self.end)

    def getText(self):
        return self.doc


class _SampleFakeSearchDescriptor:
    def __init__(self):
        self.SearchString = ""
        self.SearchCaseSensitive = False
        self.SearchRegularExpression = False


class _SampleFakeDoc:
    def __init__(self, text):
        self.text = text
        self.controller = MagicMock()
        self.controller.getViewCursor.return_value = MagicMock()
        um = MagicMock()
        um.isLocked.return_value = False
        self.getUndoManager = MagicMock(return_value=um)

    def getCurrentController(self):
        return self.controller

    def getText(self):
        return self

    def createTextCursor(self):
        return _SampleFakeCursor(self, 0)

    def createTextCursorByRange(self, r):
        pos = r.pos if hasattr(r, "pos") else r
        return _SampleFakeCursor(self, pos)

    def createSearchDescriptor(self):
        return _SampleFakeSearchDescriptor()

    def _find_from(self, sd, start_pos):
        import re
        pat = sd.SearchString
        hay = self.text[start_pos:]
        if getattr(sd, "SearchRegularExpression", False):
            flags = 0 if getattr(sd, "SearchCaseSensitive", False) else re.IGNORECASE
            m = re.search(pat, hay, flags)
            if m:
                return _SampleFakeRange(self, start_pos + m.start(), start_pos + m.end())
            return None
        cs = getattr(sd, "SearchCaseSensitive", False)
        hay_cmp = hay if cs else hay.lower()
        pat_cmp = pat if cs else pat.lower()
        idx = hay_cmp.find(pat_cmp)
        if idx != -1:
            return _SampleFakeRange(self, start_pos + idx, start_pos + idx + len(pat))
        return None

    def findFirst(self, sd):
        return self._find_from(sd, 0)

    def findNext(self, pos_or_range, sd):
        start = pos_or_range.pos if hasattr(pos_or_range, "pos") else (pos_or_range if isinstance(pos_or_range, int) else 0)
        return self._find_from(sd, start)

    def getDrawPage(self):
        dp = MagicMock()
        dp.getCount.return_value = 0
        return dp

    def getTextFields(self):
        tf = MagicMock()
        tf.createEnumeration.return_value.hasMoreElements.return_value = False
        return tf


def test_search_selection_apply_shared_case_default_on_sample_text():
    from plugin.tests.testing_utils import TestingFactory
    from plugin.writer.search import SearchInDocument
    from plugin.writer.selection import SetSelection
    from plugin.writer.apply_document_content import ApplyDocumentContent

    doc = _SampleFakeDoc("Apple pie and apple tart")
    ctx = TestingFactory.create_context(doc=doc, doc_type="writer")

    # 1. search_in_document
    # Default (omitted) is case-insensitive: finds both "Apple" and "apple"
    s_default = SearchInDocument().execute(ctx, pattern="apple")
    assert s_default["status"] == "ok"
    assert [m["text"] for m in s_default["matches"]] == ["Apple", "apple"]
    assert s_default["matched_case_sensitive"] is False

    # Uppercase query with default case-insensitive also hits both
    s_upper = SearchInDocument().execute(ctx, pattern="APPLE")
    assert [m["text"] for m in s_upper["matches"]] == ["Apple", "apple"]
    assert s_upper["matched_case_sensitive"] is False

    # Explicit case_sensitive=True finds only exact casing
    s_exact = SearchInDocument().execute(ctx, pattern="apple", case_sensitive=True)
    assert [m["text"] for m in s_exact["matches"]] == ["apple"]
    assert s_exact["matched_case_sensitive"] is True

    s_exact_miss = SearchInDocument().execute(ctx, pattern="APPLE", case_sensitive=True)
    assert s_exact_miss["matches"] == []
    assert s_exact_miss["matched_case_sensitive"] is True

    # 2. set_selection
    # Default (omitted) is case-insensitive: occurrence 0 hits first match "Apple"
    sel_default = SetSelection().execute(ctx, search_text="apple")
    assert sel_default["status"] == "ok"
    assert sel_default["selected_text"] == "Apple"
    assert sel_default["matched_case_sensitive"] is False

    sel_upper = SetSelection().execute(ctx, search_text="APPLE")
    assert sel_upper["status"] == "ok"
    assert sel_upper["selected_text"] == "Apple"
    assert sel_upper["matched_case_sensitive"] is False

    # Explicit case_sensitive=True selects exact match
    sel_exact = SetSelection().execute(ctx, search_text="apple", case_sensitive=True)
    assert sel_exact["status"] == "ok"
    assert sel_exact["selected_text"] == "apple"
    assert sel_exact["matched_case_sensitive"] is True

    sel_exact_miss = SetSelection().execute(ctx, search_text="APPLE", case_sensitive=True)
    assert sel_exact_miss["status"] == "error"
    assert "search_text not found in document" in sel_exact_miss["message"]
    assert sel_exact_miss["matched_case_sensitive"] is True

    # 3. apply_document_content (dry_run preview)
    # Default (omitted) is case-insensitive: finds both "Apple" and "apple"
    app_default = ApplyDocumentContent().execute(ctx, content="cherry", target="search", old_content="apple", dry_run=True)
    assert app_default["status"] == "ok"
    assert app_default["replaceable"][0]["text"] == "Apple"
    assert [m["text"] for m in app_default["replaceable"]] == ["Apple", "apple"]
    assert app_default["matched_case_sensitive"] is False

    app_upper = ApplyDocumentContent().execute(ctx, content="cherry", target="search", old_content="APPLE", dry_run=True)
    assert [m["text"] for m in app_upper["replaceable"]] == ["Apple", "apple"]
    assert app_upper["matched_case_sensitive"] is False

    # Explicit case_sensitive=True finds only exact casing
    app_exact = ApplyDocumentContent().execute(ctx, content="cherry", target="search", old_content="apple", case_sensitive=True, dry_run=True)
    assert [m["text"] for m in app_exact["replaceable"]] == ["apple"]
    assert app_exact["matched_case_sensitive"] is True

    app_exact_miss = ApplyDocumentContent().execute(ctx, content="cherry", target="search", old_content="APPLE", case_sensitive=True, dry_run=True)
    assert app_exact_miss["replaceable"] == []
    assert app_exact_miss["matched_case_sensitive"] is True

    # 4. Explicit case_sensitive=None (JSON null) falls back to default case-insensitive
    s_none = SearchInDocument().execute(ctx, pattern="apple", case_sensitive=None)
    assert s_none["matched_case_sensitive"] is False
    assert [m["text"] for m in s_none["matches"]] == ["Apple", "apple"]

    sel_none = SetSelection().execute(ctx, search_text="apple", case_sensitive=None)
    assert sel_none["matched_case_sensitive"] is False
    assert sel_none["selected_text"] == "Apple"

    app_none = ApplyDocumentContent().execute(
        ctx, content="cherry", target="search", old_content="apple", case_sensitive=None, dry_run=True,
    )
    assert app_none["matched_case_sensitive"] is False
    assert [m["text"] for m in app_none["replaceable"]] == ["Apple", "apple"]

    # 5. apply_document_content non-dry-run replace with all_matches=True
    doc_replace = _SampleFakeDoc("Apple pie and apple tart")
    ctx_replace = TestingFactory.create_context(doc=doc_replace, doc_type="writer")

    replaced_items: list[str] = []

    def fake_replace_preserving(model, target_range, new_text, *args, **kwargs):
        replaced_items.append(target_range.getString())
        model.text = model.text[:target_range.start] + new_text + model.text[target_range.end:]

    with patch("plugin.writer.apply_document_content.get_agent_edit_review_mode", return_value="off"), \
         patch("plugin.writer.edit_review.get_agent_edit_review_mode", return_value="off"), \
         patch("plugin.writer.apply_document_content.edit_review_wait_seconds", return_value=0), \
         patch("plugin.writer.edit_review.replace_preserving_format", side_effect=fake_replace_preserving), \
         patch("plugin.writer.apply_document_content.collapsed_anchor", return_value=MagicMock()):
        res_replace = ApplyDocumentContent().execute(
            ctx_replace, content=["cherry"], target="search", old_content="apple", all_matches=True,
        )
        assert res_replace["status"] == "ok"
        assert res_replace["replaced_count"] == 2
        assert res_replace["matched_case_sensitive"] is False
        assert set(replaced_items) == {"Apple", "apple"}
        assert doc_replace.text == "cherry pie and cherry tart"


# ---- replace_text_in_shape ---------------------------------------------------

from plugin.framework.errors import DocumentDisposedError
from plugin.writer.search import replace_text_in_shape


class DisposedException(Exception):
    pass


def test_replace_text_in_shape_reraises_disposal():
    shape = MagicMock()
    shape.getText.side_effect = DisposedException("closed")
    with pytest.raises(DocumentDisposedError):
        replace_text_in_shape(shape, "a", "b")


def test_replace_text_in_shape_returns_false_on_ordinary_error():
    shape = MagicMock()
    shape.getText.side_effect = RuntimeError("boom")
    assert replace_text_in_shape(shape, "a", "b") is False


def test_replace_text_in_shape_folds_quotes():
    xtext = MagicMock()
    xtext.getString.return_value = "He said \u201cHi,\u201d"
    cursor = MagicMock()
    xtext.createTextCursorByRange.return_value = cursor
    shape = MagicMock()
    shape.getText.return_value = xtext

    assert replace_text_in_shape(shape, '"Hi,"', "Bye") is True
    cursor.goRight.assert_any_call(8, False)
    cursor.goRight.assert_any_call(5, True)
    cursor.setString.assert_called_once_with("Bye")


def test_replace_text_in_shape_case_sensitivity():
    xtext = MagicMock()
    xtext.getString.return_value = "Hello World"
    cursor = MagicMock()
    xtext.createTextCursorByRange.return_value = cursor
    shape = MagicMock()
    shape.getText.return_value = xtext

    assert replace_text_in_shape(shape, "world", "Universe", case_sensitive=False) is True
    cursor.goRight.assert_any_call(6, False)
    cursor.goRight.assert_any_call(5, True)
    cursor.setString.assert_called_once_with("Universe")

    assert replace_text_in_shape(shape, "world", "Universe", case_sensitive=True) is False


def test_replace_text_in_shape_regex():
    xtext = MagicMock()
    xtext.getString.return_value = "say hello!"
    cursor = MagicMock()
    xtext.createTextCursorByRange.return_value = cursor
    shape = MagicMock()
    shape.getText.return_value = xtext

    assert replace_text_in_shape(shape, "h.+o", "goodbye", use_regex=True) is True
    cursor.goRight.assert_any_call(4, False)
    cursor.goRight.assert_any_call(5, True)
    cursor.setString.assert_called_once_with("goodbye")


def test_replace_text_in_shape_regex_empty_match_returns_false():
    xtext = MagicMock()
    xtext.getString.return_value = "hello"
    cursor = MagicMock()
    xtext.createTextCursorByRange.return_value = cursor
    shape = MagicMock()
    shape.getText.return_value = xtext

    for pat in ("x*", "^"):
        assert replace_text_in_shape(shape, pat, "bar", use_regex=True) is False
    cursor.setString.assert_not_called()
    cursor.goRight.assert_not_called()


def test_replace_text_in_shape_regex_skips_leading_empty():
    xtext = MagicMock()
    xtext.getString.return_value = "ab"
    cursor = MagicMock()
    xtext.createTextCursorByRange.return_value = cursor
    shape = MagicMock()
    shape.getText.return_value = xtext

    assert replace_text_in_shape(shape, "b*", "B", use_regex=True) is True
    cursor.goRight.assert_any_call(1, False)
    cursor.goRight.assert_any_call(1, True)
    cursor.setString.assert_called_once_with("B")


class _Pos:
    """A text range at character positions [start, end] in *text*."""

    def __init__(self, text, start, end=None):
        self.text, self.start, self.end = text, start, start if end is None else end

    def getText(self):
        return self.text


class _Text:
    @staticmethod
    def _cmp(a, b):
        return (a > b) - (a < b)

    def compareRegionStarts(self, a, b):
        return self._cmp(b.start, a.start)

    def compareRegionEnds(self, a, b):
        return self._cmp(b.end, a.end)


def test_inside_tracked_deletion_needs_the_hit_wholly_inside():
    from plugin.writer.search import inside_tracked_deletion

    text, other = _Text(), _Text()
    # RedlineStart can be the later end of the deletion.
    spans = [(_Pos(text, 20), _Pos(text, 10))]
    with patch("plugin.writer.search.uno_same", side_effect=lambda a, b: a is b):
        assert inside_tracked_deletion(_Pos(text, 12, 18), spans)
        assert inside_tracked_deletion(_Pos(text, 10, 20), spans)
        assert not inside_tracked_deletion(_Pos(text, 5, 15), spans)
        assert not inside_tracked_deletion(_Pos(text, 21, 25), spans)
        assert not inside_tracked_deletion(_Pos(other, 12, 18), spans)
        assert not inside_tracked_deletion(_Pos(text, 12, 18), [])


def test_edit_search_skips_hits_in_tracked_deletions():
    """apply_document_content's search must not pick text the user deleted with Track Changes."""
    from plugin.writer import search

    doc = MagicMock()
    deleted_hit, live_hit = MagicMock(), MagicMock()
    deleted_hit.getString.return_value = live_hit.getString.return_value = "Para"
    doc.findFirst.return_value = deleted_hit
    doc.findNext.side_effect = [live_hit, None]
    with patch.object(search, "tracked_deletion_spans", return_value=["span"]), \
            patch.object(search, "inside_tracked_deletion", side_effect=lambda f, s: f is deleted_hit):
        assert search.find_ranges_regex_case(doc, "Para", False, False, all_matches=False) is live_hit
        doc.findNext.side_effect = [live_hit, None]
        assert search.find_ranges_regex_case(doc, "Para", False, False, all_matches=True) == [live_hit]


def test_tracked_deletion_spans_skips_unreadable_redlines():
    """Unreadable redlines must not abort the scan or hide later tracked deletions."""
    from plugin.writer import search

    doc = MagicMock()
    bad_rl = MagicMock()
    bad_rl.getPropertyValue.side_effect = RuntimeError("corrupt redline")

    good_rl = MagicMock()
    start_pos, end_pos = MagicMock(), MagicMock()
    good_rl.getPropertyValue.side_effect = lambda name: {
        "RedlineType": "Delete",
        "RedlineStart": start_pos,
        "RedlineEnd": end_pos,
    }[name]

    def fake_scan(d, on_item):
        on_item(bad_rl)
        on_item(good_rl)

    with patch.object(search, "scan_redlines", side_effect=fake_scan):
        spans = search.tracked_deletion_spans(doc)
    assert spans == [(start_pos, end_pos)]


def test_find_text_ranges_without_deletions_uses_fast_path():
    """When the document has no tracked deletions, find_text_ranges measures natively
    without invoking get_string_without_tracked_deletions portion walks."""
    from plugin.writer import search

    model = MagicMock()
    text = MagicMock()
    model.getText.return_value = text
    cursor = MagicMock()
    text.createTextCursor.return_value = cursor
    cursor.getString.return_value = "hello "

    hit = MagicMock()
    hit.getText.return_value = text
    hit.getString.return_value = "world"
    model.findNext.side_effect = [hit, None]

    with patch.object(search, "tracked_deletion_spans", return_value=[]), \
            patch.object(search, "get_string_without_tracked_deletions") as mock_helper:
        matches = search.find_text_ranges(model, "world")
        assert len(matches) == 1
        assert matches[0] == {"start": 6, "end": 11, "text": "world"}
        mock_helper.assert_not_called()


def test_find_text_ranges_with_deletions_measures_incrementally():
    """When tracked deletions exist, find_text_ranges measures forward incrementally."""
    from plugin.writer import search

    model = MagicMock()
    text = MagicMock()
    model.getText.return_value = text

    hit1 = MagicMock()
    hit1.getText.return_value = text
    hit1.getString.return_value = "one"

    hit2 = MagicMock()
    hit2.getText.return_value = text
    hit2.getString.return_value = "two"

    model.findNext.side_effect = [hit1, hit2, None]

    # Incremental walk measures visible text of gaps and hits in forward order:
    # 1. gap to hit 1 (len 10)
    # 2. visible text of hit 1 ("one", len 3)
    # 3. gap between hit 1 and hit 2 (len 12)
    # 4. visible text of hit 2 ("two", len 3)
    # Result: hit 1 at [10, 13], hit 2 at [10 + 3 + 12 = 25, 28]
    step_results = ["x" * 10, "one", "x" * 12, "two"]
    with patch.object(search, "tracked_deletion_spans", return_value=[(MagicMock(), MagicMock())]), \
            patch.object(search, "inside_tracked_deletion", return_value=False), \
            patch.object(search, "get_string_without_tracked_deletions", side_effect=lambda g, **kw: step_results.pop(0)):
        matches = search.find_text_ranges(model, "pattern")
        assert len(matches) == 2
        assert matches[0] == {"start": 10, "end": 13, "text": "one"}
        assert matches[1] == {"start": 25, "end": 28, "text": "two"}


def test_find_text_ranges_straddling_tracked_deletion_measures_visible_span():
    """Hits straddling a tracked deletion boundary measure end offset from visible characters."""
    from plugin.writer import search

    model = MagicMock()
    text = MagicMock()
    model.getText.return_value = text

    hit = MagicMock()
    hit.getText.return_value = text
    # Raw text returned by LO contains the deleted characters (9 chars)
    hit.getString.return_value = "foobarbaz"
    model.findNext.side_effect = [hit, None]

    # Gap before hit is 5 visible chars; hit visible text has "bar" omitted (6 chars)
    step_results = ["x" * 5, "foobaz"]
    with patch.object(search, "tracked_deletion_spans", return_value=[(MagicMock(), MagicMock())]), \
            patch.object(search, "inside_tracked_deletion", return_value=False), \
            patch.object(search, "get_string_without_tracked_deletions", side_effect=lambda g, **kw: step_results.pop(0)):
        matches = search.find_text_ranges(model, "pattern")
        assert len(matches) == 1
        assert matches[0] == {"start": 5, "end": 11, "text": "foobaz"}
        assert matches[0]["end"] - matches[0]["start"] == 6
