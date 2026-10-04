"""Unit tests for paragraph range helpers moved out of document_helpers."""

from plugin.doc.paragraph_search import find_paragraph_for_range, get_paragraph_ranges, search_paragraph_texts


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
    matches, total = search_paragraph_texts("foo", ["foo bar", "baz"])
    assert total == 1
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


def test_find_paragraph_after_table_keeps_enumeration_index():
    table = _Table(15)
    elements = [_Para(0, 10), table, _Para(20, 30)]
    match = _Para(24, 24)
    assert find_paragraph_for_range(match, elements, _TextObj()) == 2
    assert table.start_calls == 0


def test_find_table_anchor_maps_to_table_slot():
    table = _Table(15)
    elements = [_Para(0, 10), table, _Para(20, 30)]
    match = _Para(15, 15)
    assert find_paragraph_for_range(match, elements, _TextObj()) == 1
    assert table.start_calls == 0


def test_confirm_paragraph_index_keeps_real_zero_and_rejects_unplaced():
    """Index 0 is a real hit only when the anchor start is inside that paragraph."""
    from plugin.doc.paragraph_search import confirm_paragraph_index

    elements = [_Para(0, 10), _Para(20, 30)]
    text = _TextObj()
    at_start = _Para(0, 0)
    past_end = _Para(100, 100)
    assert find_paragraph_for_range(at_start, elements, text) == 0
    assert confirm_paragraph_index(text, at_start, elements, 0) == 0
    # The finder still reports 0 when the point is past the last paragraph.
    assert find_paragraph_for_range(past_end, elements, text) == 0
    assert confirm_paragraph_index(text, past_end, elements, 0) is None
    assert confirm_paragraph_index(text, _Para(24, 24), elements, 1) == 1

    class _Broken:
        def getStart(self):
            raise RuntimeError("stale anchor")

    assert confirm_paragraph_index(text, _Broken(), elements, 0) is None


def test_find_picks_matching_table_when_two_share_a_gap():
    first = _Table(12)
    second = _Table(18)
    elements = [_Para(0, 10), first, second, _Para(30, 40)]
    match = _Para(18, 18)
    assert find_paragraph_for_range(match, elements, _TextObj()) == 2
    assert first.start_calls == 0
    assert second.start_calls == 0
