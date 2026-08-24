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
