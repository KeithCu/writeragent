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
