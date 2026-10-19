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

def _search_zero_hits(pattern, use_regex):
    """Run SearchInDocument against a doc that finds nothing."""
    from plugin.writer.search import SearchInDocument

    doc = MagicMock()
    doc.createSearchDescriptor.return_value = MagicMock()
    doc.findFirst.return_value = None
    doc.getDrawPage.return_value = []
    doc.getTextFields.return_value.createEnumeration.return_value.hasMoreElements.return_value = False
    ctx = MagicMock()
    ctx.doc = doc
    return SearchInDocument().execute(ctx, pattern=pattern, regex=use_regex)


def test_invalid_regex_zero_hits_is_error():
    res = _search_zero_hits("([a-", True)
    assert res["status"] == "error" and res["code"] == "INVALID_REGEX"
    assert "regex=false" in res["message"]


def test_valid_regex_zero_hits_stays_ok():
    res = _search_zero_hits("nunca_existe_\\d+", True)
    assert res["status"] == "ok" and res["count"] == 0


def test_literal_zero_hits_stays_ok():
    res = _search_zero_hits("([a-", False)  # literal search for weird chars is legitimate
    assert res["status"] == "ok" and res["count"] == 0


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


def test_find_lo_regex_ranges_caps_at_max_search_replacements():
    from plugin.writer.search import find_lo_regex_ranges, MAX_SEARCH_REPLACEMENTS

    doc = MagicMock()
    doc.createSearchDescriptor.return_value = MagicMock()
    doc.findFirst.return_value = MagicMock()
    doc.findNext.side_effect = lambda found, sd: MagicMock()
    out = find_lo_regex_ranges(doc, "x", all_matches=True)
    assert len(out) == MAX_SEARCH_REPLACEMENTS


def test_not_found_hints_at_pending_tracked_deletions():
    """#43: visible text across a struck word is never found; say why instead of just 'shorter'."""
    from unittest.mock import MagicMock

    from plugin.writer.search import build_search_not_found_response, document_has_tracked_deletions

    def doc_with(*types):
        doc = MagicMock()
        reds = doc.getRedlines.return_value
        reds.getCount.return_value = len(types)
        items = [MagicMock(**{"getPropertyValue.return_value": t}) for t in types]
        enum = reds.createEnumeration.return_value
        enum.hasMoreElements.side_effect = [True] * len(items) + [False]
        enum.nextElement.side_effect = items
        return doc

    assert document_has_tracked_deletions(doc_with("Insert", "Delete")) is True
    assert document_has_tracked_deletions(doc_with("Insert")) is False
    msg = build_search_not_found_response(tracked_deletions=True)["message"]
    assert "pending tracked deletions" in msg
    assert "pending tracked" not in build_search_not_found_response()["message"]


def test_tracked_deletion_scan_skips_an_unreadable_redline():
    from unittest.mock import MagicMock

    from plugin.writer.search import document_has_tracked_deletions

    bad = MagicMock()
    bad.getPropertyValue.side_effect = RuntimeError("gone")
    good = MagicMock(**{"getPropertyValue.return_value": "Delete"})
    doc = MagicMock()
    doc.getRedlines.return_value.getCount.return_value = 2
    enum = doc.getRedlines.return_value.createEnumeration.return_value
    enum.hasMoreElements.side_effect = [True, True, False]
    enum.nextElement.side_effect = [bad, good]
    assert document_has_tracked_deletions(doc) is True


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
    assert "search_in_document: getString failed" in caplog.text


@pytest.mark.parametrize("bad_val", [0, -1, True, False, None, "20", 1.5])
def test_search_in_document_max_results_rejects_non_positive(bad_val):
    from plugin.writer.search import SearchInDocument

    doc = MagicMock()
    ctx = MagicMock(doc=doc)
    res = SearchInDocument().execute(ctx, pattern="test", max_results=bad_val)
    assert res["status"] == "error"
    assert res["code"] == "INVALID_PARAM"
    doc.createSearchDescriptor.assert_not_called()


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


def test_find_text_ranges_limit_zero_and_bad_types_return_empty():
    from plugin.writer.search import find_text_ranges

    for bad_limit in (0, -3, "1", True):
        model = MagicMock()
        res = find_text_ranges(model, MagicMock(), "foo", limit=bad_limit)
        assert res == []
        model.createSearchDescriptor.assert_not_called()

    model = MagicMock()
    model.findNext.return_value = None
    res = find_text_ranges(model, MagicMock(), "foo", limit=None)
    model.createSearchDescriptor.assert_called_once()


def test_find_text_ranges_body_only_skips_other_xtext():
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
    res_body_only = find_text_ranges(model, MagicMock(), "hit", body_only=True)
    assert len(res_body_only) == 1
    assert res_body_only[0]["text"] == "hit1"

    # body_only=False -> both
    model.findNext.side_effect = [hit_body, hit_nested, None]
    res_all = find_text_ranges(model, MagicMock(), "hit", body_only=False)
    assert len(res_all) == 2
    assert res_all[0]["text"] == "hit1"
    assert res_all[1]["text"] == "hit2"
    assert res_all[1]["start"] == len("nested_pre")


def test_find_text_ranges_start_stops_when_go_right_is_false():
    from plugin.writer.search import find_text_ranges

    model = MagicMock()
    cursor = model.getText.return_value.createTextCursor.return_value
    cursor.goRight.return_value = False
    model.findNext.return_value = None

    find_text_ranges(model, MagicMock(), "foo", start=100000)
    cursor.goRight.assert_called_once_with(8192, False)
    model.findNext.assert_called_once()


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
    assert "advanced_search: could not read the view cursor" in caplog.text


def test_advanced_search_missing_query():
    from plugin.writer.search import AdvancedSearch

    idx_svc = MagicMock()
    ctx = MagicMock()
    ctx.services.writer_index = idx_svc

    tool = AdvancedSearch()
    for bad_q in ({}, {"query": None}, {"query": "  "}, {"query": 1}):
        res = tool.execute(ctx, **bad_q)
        assert res["status"] == "error"
        assert "query is required" in res["message"]
        idx_svc.search_boolean.assert_not_called()

    idx_svc.search_boolean.return_value = {"matches": []}
    res_ok = tool.execute(ctx, query="climate")
    assert res_ok["status"] == "ok"
    idx_svc.search_boolean.assert_called_once()


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
        find_text_ranges(model, MagicMock(), "foo")


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


def test_find_chained_range_same_text_combine_failure_propagates():
    from plugin.writer.search import find_chained_range

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
    with patch("plugin.writer.search.find_lo_regex_ranges", return_value=None), \
         patch("plugin.writer.search.get_string_without_tracked_deletions", side_effect=["Hello", "World"]):
        with pytest.raises(RuntimeError, match="combine failed"):
            find_chained_range(doc, "Hello\nWorld", all_matches=False)


def test_find_chained_range_cross_text_is_skipped():
    from plugin.writer.search import find_chained_range

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
    with patch("plugin.writer.search.find_lo_regex_ranges", return_value=None), \
         patch("plugin.writer.search.get_string_without_tracked_deletions", side_effect=["Hello", "World"]):
        assert find_chained_range(doc, "Hello\nWorld", all_matches=False) is None
    assert n["i"] == 7


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
        describe_match_location(closed)

    odd = MagicMock()
    odd.getText.side_effect = RuntimeException("uno")
    odd.getString.return_value = "raw"
    assert describe_match_location(odd) == "body"
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


def test_find_first_range_sensitive_then_insensitive_fallback():
    from plugin.writer.search import find_first_range

    doc = MagicMock()
    sd = MagicMock()
    doc.createSearchDescriptor.return_value = sd

    hit = MagicMock()
    hit.getString.return_value = "lower"

    doc.findFirst.side_effect = [None, hit]

    res = find_first_range(doc, "LOWER")
    assert res is hit

