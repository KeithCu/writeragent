# Unit tests for apply_document_content search helpers (no LibreOffice required).
from plugin.writer.content import (
    _all_start_indices,
    _drawing_shape_containing,
    _escape_for_lo_regex,
    _HORIZONTAL_SPACE_CLASS,
    _normalize_search_string_for_find,
    _SPACE_NORMALIZE_MAP,
)


def test_all_start_indices_non_overlapping():
    assert _all_start_indices("abababa", "aba") == [0, 4]
    assert _all_start_indices("", "x") == []
    assert _all_start_indices("abc", "") == []


def test_normalize_search_string_collapses_nbsp():
    nbsp = "\u00a0"
    em_space = "\u2003"
    cjk_space = "\u3000"
    assert _normalize_search_string_for_find("foo" + nbsp + nbsp + "bar") == "foo bar"
    assert _normalize_search_string_for_find("foo" + em_space + cjk_space + "bar") == "foo bar"
    assert _normalize_search_string_for_find("line1\nline2") == "line1\nline2"


def test_space_normalize_map_is_one_to_one_ascii():
    for cp, replacement in _SPACE_NORMALIZE_MAP.items():
        assert len(chr(cp)) == 1
        assert replacement == " "


def test_escape_for_lo_regex_expands_ascii_space_runs():
    assert _escape_for_lo_regex("a  b") == "a" + _HORIZONTAL_SPACE_CLASS + "+" + "b"
    assert _escape_for_lo_regex("hello world") == "hello" + _HORIZONTAL_SPACE_CLASS + "+" + "world"


def test_escape_for_lo_regex_normalizes_exotic_spaces_in_needle():
    nbsp = "\u00a0"
    # NBSP in needle is normalized to ASCII space, then expanded to the flex space class.
    assert _escape_for_lo_regex("a" + nbsp + "b") == "a" + _HORIZONTAL_SPACE_CLASS + "+" + "b"


def test_escape_for_lo_regex_escapes_regex_metacharacters():
    assert _escape_for_lo_regex("a.b") == r"a\.b"
    assert _escape_for_lo_regex("(test)") == r"\(test\)"


# --- _drawing_shape_containing (C7): actionable "text is inside a shape" diagnostic -----------

class _FakeShape:
    def __init__(self, text, name="", raise_on_get=False):
        self._text = text
        self.Name = name
        self._raise = raise_on_get

    def getString(self):
        if self._raise:
            raise RuntimeError("shape getString boom")
        return self._text


class _FakeDrawPage:
    def __init__(self, shapes, raise_count=False):
        self._shapes = shapes
        self._raise_count = raise_count

    def getCount(self):
        if self._raise_count:
            raise RuntimeError("count boom")
        return len(self._shapes)

    def getByIndex(self, i):
        return self._shapes[i]


class _FakeDocWithShapes:
    def __init__(self, shapes, raise_count=False):
        self._dp = _FakeDrawPage(shapes, raise_count=raise_count)

    def getDrawPage(self):
        return self._dp


def test_drawing_shape_containing_returns_name():
    doc = _FakeDocWithShapes([_FakeShape("body of box", name="Caixa de Texto 8")])
    assert _drawing_shape_containing(doc, "of box") == "Caixa de Texto 8"


def test_drawing_shape_containing_unnamed_shape():
    doc = _FakeDocWithShapes([_FakeShape("<O QUE ORIGINOU A DEMANDA?>", name="")])
    assert _drawing_shape_containing(doc, "ORIGINOU") == "(unnamed shape)"


def test_drawing_shape_containing_not_found_returns_none():
    doc = _FakeDocWithShapes([_FakeShape("something else")])
    assert _drawing_shape_containing(doc, "missing") is None


def test_drawing_shape_containing_empty_needle_returns_none():
    doc = _FakeDocWithShapes([_FakeShape("anything")])
    assert _drawing_shape_containing(doc, "   ") is None


def test_drawing_shape_containing_no_draw_page_returns_none():
    assert _drawing_shape_containing(object(), "x") is None


def test_drawing_shape_containing_skips_failing_shape():
    # A shape whose getString raises must be skipped, not abort the scan -- the match is in the next.
    doc = _FakeDocWithShapes([_FakeShape("", raise_on_get=True), _FakeShape("here is the marker")])
    assert _drawing_shape_containing(doc, "marker") == "(unnamed shape)"


def test_drawing_shape_containing_fails_safe_on_count_error():
    doc = _FakeDocWithShapes([_FakeShape("marker")], raise_count=True)
    assert _drawing_shape_containing(doc, "marker") is None

