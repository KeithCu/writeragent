# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Search descriptor helpers. No LibreOffice."""
from unittest.mock import MagicMock

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
    from plugin.writer.search import find_ranges_regex_case, _MAX_SEARCH_REPLACEMENTS

    doc = MagicMock()
    doc.createSearchDescriptor.return_value = MagicMock()
    doc.findFirst.return_value = MagicMock()
    doc.findNext.side_effect = lambda found, sd: MagicMock()
    out = find_ranges_regex_case(doc, "x", True, True, all_matches=True)
    assert len(out) == _MAX_SEARCH_REPLACEMENTS


def test_find_lo_regex_ranges_caps_at_max_search_replacements():
    from plugin.writer.search import find_lo_regex_ranges, _MAX_SEARCH_REPLACEMENTS

    doc = MagicMock()
    doc.createSearchDescriptor.return_value = MagicMock()
    doc.findFirst.return_value = MagicMock()
    doc.findNext.side_effect = lambda found, sd: MagicMock()
    out = find_lo_regex_ranges(doc, "x", all_matches=True)
    assert len(out) == _MAX_SEARCH_REPLACEMENTS


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
