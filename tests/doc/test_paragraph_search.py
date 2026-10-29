"""Unit tests for paragraph range helpers moved out of document_helpers."""

import logging
from unittest.mock import MagicMock

import pytest

from plugin.doc.paragraph_search import (
    confirm_paragraph_index,
    find_paragraph_for_range,
    get_paragraph_ranges,
    search_paragraph_texts,
)
from plugin.framework.errors import DocumentDisposedError


class _Enum:
    def __init__(self, items):
        self._items = list(items)
        self._idx = 0

    def hasMoreElements(self):
        return self._idx < len(self._items)

    def nextElement(self):
        item = self._items[self._idx]
        self._idx += 1
        return item


class _Text:
    def __init__(self, paras):
        self._paras = paras

    def createEnumeration(self):
        return _Enum(self._paras)


class _Doc:
    def __init__(self, paras):
        self._text = _Text(paras)

    def getText(self):
        return self._text


def test_get_paragraph_ranges_returns_enumerated_elements():
    paras = ["p0", "p1", "p2"]
    assert get_paragraph_ranges(_Doc(paras)) == paras


def test_find_paragraph_for_range_binary_search_hit():
    class _Pos:
        def __init__(self, n):
            self.n = n

        def getStart(self):
            return self.n

        def getEnd(self):
            return self.n + 10

    class _TextObj:
        def compareRegionStarts(self, a, b):
            if a < b:
                return 1
            if a > b:
                return -1
            return 0

    paras = [_Pos(0), _Pos(10), _Pos(20)]
    match = _Pos(12)
    assert find_paragraph_for_range(match, paras, _TextObj()) == 1


def test_search_paragraph_texts_still_exported():
    matches, truncated = search_paragraph_texts("foo", ["foo bar", "baz"])
    assert truncated is False
    assert matches[0]["paragraph_index"] == 0


class _TextObj:
    def compareRegionStarts(self, a, b):
        if a < b:
            return 1
        if a > b:
            return -1
        return 0


class _Para:
    def __init__(self, start, end):
        self.start = start
        self.end = end

    def supportsService(self, name):
        return name == "com.sun.star.text.Paragraph"

    def getStart(self):
        return self.start

    def getEnd(self):
        return self.end


class _Table:
    """SwXTextTable stand-in: enumeration element, not an XTextRange."""

    def __init__(self, anchor_at):
        self.anchor_at = anchor_at
        self.start_calls = 0

    def supportsService(self, name):
        return name == "com.sun.star.text.TextTable"

    def getStart(self):
        self.start_calls += 1
        raise AttributeError("getStart")

    def getEnd(self):
        raise AttributeError("getEnd")

    def getAnchor(self):
        return _Para(self.anchor_at, self.anchor_at)


def test_get_paragraph_ranges_keeps_tables_for_enumeration_index():
    table = _Table(15)
    elements = [_Para(0, 10), table, _Para(20, 30)]
    assert get_paragraph_ranges(_Doc(elements)) == elements


@pytest.mark.parametrize(
    "value, value_2, expected",
    [
        pytest.param(24, 24, 2, id="test_find_paragraph_after_table_keeps_enumeration_index"),
        pytest.param(15, 15, 1, id="test_find_table_anchor_maps_to_table_slot"),
    ],
)
def test_find_paragraph_after_table_keeps_enumeration_index(value, value_2, expected):
    table = _Table(15)
    elements = [_Para(0, 10), table, _Para(20, 30)]
    match = _Para(value, value_2)
    assert find_paragraph_for_range(match, elements, _TextObj()) == expected
    assert table.start_calls == 0

def test_confirm_paragraph_index_keeps_real_zero_and_rejects_unplaced():
    """Index 0 is a real hit only when the anchor start is inside that paragraph."""
    elements = [_Para(0, 10), _Para(20, 30)]
    text = _TextObj()
    at_start = _Para(0, 0)
    past_end = _Para(100, 100)
    assert find_paragraph_for_range(at_start, elements, text) == 0
    assert confirm_paragraph_index(text, at_start, elements, 0) == 0
    # The finder returns None when the point is past the last paragraph.
    assert find_paragraph_for_range(past_end, elements, text) is None
    assert confirm_paragraph_index(text, past_end, elements, 0) is None
    assert confirm_paragraph_index(text, past_end, elements, None) is None
    assert confirm_paragraph_index(text, _Para(24, 24), elements, 1) == 1

    class _Broken:
        def getStart(self):
            raise RuntimeError("stale anchor")

    assert confirm_paragraph_index(text, _Broken(), elements, 0) is None


def test_find_paragraph_for_range_unplaced_returns_none():
    elements = [_Para(5, 10)]
    text = _TextObj()
    assert find_paragraph_for_range(_Para(0, 0), elements, text) is None
    assert find_paragraph_for_range(_Para(50, 50), elements, text) is None
    assert find_paragraph_for_range(_Para(0, 0), [], text) is None
    assert find_paragraph_for_range(_Para(10, 10), elements, text) == 0


def test_find_paragraph_for_range_unplaced_logs_debug(caplog):
    class _BrokenAnchor:
        def getText(self):
            return _TextObj()

        def getStart(self):
            raise RuntimeError("stale anchor")

    elements = [_Para(0, 10)]
    with caplog.at_level(logging.DEBUG):
        result = find_paragraph_for_range(_BrokenAnchor(), elements)

    assert result is None
    assert not any(record.levelno >= logging.ERROR for record in caplog.records)


def test_find_picks_matching_table_when_two_share_a_gap():
    first = _Table(12)
    second = _Table(18)
    elements = [_Para(0, 10), first, second, _Para(30, 40)]
    match = _Para(18, 18)
    assert find_paragraph_for_range(match, elements, _TextObj()) == 2
    assert first.start_calls == 0
    assert second.start_calls == 0

def test_find_paragraph_for_range_disposed():
    match_range = MagicMock()
    match_range.getText.side_effect = DocumentDisposedError("stale")

    with pytest.raises(DocumentDisposedError):
        find_paragraph_for_range(match_range, [])


def test_two_finds_with_shared_entries_calls_supports_service_once():
    from plugin.doc.paragraph_search import paragraph_entries

    p0 = MagicMock()
    p0.supportsService.return_value = True
    p0.getStart.return_value = 0
    p0.getEnd.return_value = 10

    p1 = MagicMock()
    p1.supportsService.return_value = True
    p1.getStart.return_value = 20
    p1.getEnd.return_value = 30

    elements = [p0, p1]
    entries = paragraph_entries(elements)

    assert p0.supportsService.call_count == 1
    assert p1.supportsService.call_count == 1

    text = _TextObj()
    m1 = _Para(5, 5)
    m2 = _Para(25, 25)

    idx1 = find_paragraph_for_range(m1, elements, text, entries=entries)
    idx2 = find_paragraph_for_range(m2, elements, text, entries=entries)

    assert idx1 == 0
    assert idx2 == 1
    assert p0.supportsService.call_count == 1
    assert p1.supportsService.call_count == 1


def test_search_paragraph_texts_literal_case_insensitive():
    matches, truncated = search_paragraph_texts("FOO", ["Foo bar", "nope"])
    assert truncated is False
    assert matches[0]["text"] == "Foo"


def test_search_paragraph_texts_case_sensitive():
    matches, truncated = search_paragraph_texts("foo", ["Foo foo"], case_sensitive=True)
    assert truncated is False
    assert len(matches) == 1
    assert matches[0]["text"] == "foo"


def test_search_paragraph_texts_regex():
    matches, truncated = search_paragraph_texts(r"f.o", ["foo bar"], regex=True)
    assert truncated is False
    assert matches[0]["text"] == "foo"


def test_search_paragraph_texts_invalid_regex():
    with pytest.raises(ValueError, match="Invalid regex"):
        search_paragraph_texts("(", ["foo"], regex=True)


def test_search_paragraph_texts_literal_unicode_offsets():
    matches, truncated = search_paragraph_texts("foo", ["İfoo"])
    assert truncated is False
    assert matches[0]["text"] == "foo"


def test_search_paragraph_texts_multiple_matches_per_paragraph():
    matches, truncated = search_paragraph_texts("foo", ["foo foo"])
    assert truncated is False
    assert len(matches) == 2


def test_search_paragraph_texts_stops_at_max_results_plus_one():
    matches, truncated = search_paragraph_texts("foo", ["foo foo foo", "foo"], max_results=2)
    assert len(matches) == 2
    assert truncated is True


def test_search_paragraph_texts_exact_fit_is_not_truncated():
    matches, truncated = search_paragraph_texts("foo", ["foo", "foo"], max_results=2)
    assert truncated is False
    assert len(matches) == 2


def test_search_paragraph_texts_does_not_scan_past_cap():
    scans = []

    def check_stop():
        scans.append(1)
        return False

    matches, truncated = search_paragraph_texts(
        "foo",
        ["foo foo foo", "foo"],
        max_results=2,
        stop_checker=check_stop,
    )
    assert len(matches) == 2
    assert truncated is True
    assert len(scans) == 1


def test_search_paragraph_texts_stop_checker_returns_nothing():
    matches, truncated = search_paragraph_texts("foo", ["foo"], stop_checker=lambda: True)
    assert matches == []
    assert truncated is False


def test_search_paragraph_texts_context_at_edges():
    matches, _ = search_paragraph_texts("hit", ["hit", "a", "b", "hit"], max_results=5, context_paragraphs=1)
    assert len(matches) == 2
    assert [c["index"] for c in matches[0]["context"]] == [0, 1]
    assert [c["index"] for c in matches[1]["context"]] == [2, 3]


