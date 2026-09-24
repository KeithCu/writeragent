# Unit tests for apply_document_content search helpers (no LibreOffice required).
from plugin.writer.search import (
    all_start_indices,
    drawing_shape_containing,
    escape_for_lo_regex,
    HORIZONTAL_SPACE_CLASS,
    normalize_search_string_for_find,
    SPACE_NORMALIZE_MAP,
)


def test_all_start_indices_non_overlapping():
    assert all_start_indices("abababa", "aba") == [0, 4]
    assert all_start_indices("", "x") == []
    assert all_start_indices("abc", "") == []


def test_normalize_search_string_collapses_nbsp():
    nbsp = "\u00a0"
    em_space = "\u2003"
    cjk_space = "\u3000"
    assert normalize_search_string_for_find("foo" + nbsp + nbsp + "bar") == "foo bar"
    assert normalize_search_string_for_find("foo" + em_space + cjk_space + "bar") == "foo bar"
    assert normalize_search_string_for_find("line1\nline2") == "line1\nline2"


def test_space_normalize_map_is_one_to_one_ascii():
    for cp, replacement in SPACE_NORMALIZE_MAP.items():
        assert len(chr(cp)) == 1
        assert replacement == " "


def test_escape_for_lo_regex_expands_ascii_space_runs():
    assert escape_for_lo_regex("a  b") == "a" + HORIZONTAL_SPACE_CLASS + "+" + "b"
    assert escape_for_lo_regex("hello world") == "hello" + HORIZONTAL_SPACE_CLASS + "+" + "world"


def test_escape_for_lo_regex_normalizes_exotic_spaces_in_needle():
    nbsp = "\u00a0"
    # NBSP in needle is normalized to ASCII space, then expanded to the flex space class.
    assert escape_for_lo_regex("a" + nbsp + "b") == "a" + HORIZONTAL_SPACE_CLASS + "+" + "b"


def test_escape_for_lo_regex_escapes_regex_metacharacters():
    assert escape_for_lo_regex("a.b") == r"a\.b"
    assert escape_for_lo_regex("(test)") == r"\(test\)"


# --- drawing_shape_containing (C7): actionable "text is inside a shape" diagnostic -----------

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
    assert drawing_shape_containing(doc, "of box") == "Caixa de Texto 8"


def test_drawing_shape_containing_unnamed_shape():
    doc = _FakeDocWithShapes([_FakeShape("<O QUE ORIGINOU A DEMANDA?>", name="")])
    assert drawing_shape_containing(doc, "ORIGINOU") == "(unnamed shape)"


def test_drawing_shape_containing_not_found_returns_none():
    doc = _FakeDocWithShapes([_FakeShape("something else")])
    assert drawing_shape_containing(doc, "missing") is None


def test_drawing_shape_containing_empty_needle_returns_none():
    doc = _FakeDocWithShapes([_FakeShape("anything")])
    assert drawing_shape_containing(doc, "   ") is None


def test_drawing_shape_containing_no_draw_page_returns_none():
    assert drawing_shape_containing(object(), "x") is None


def test_drawing_shape_containing_skips_failing_shape():
    # A shape whose getString raises must be skipped, not abort the scan -- the match is in the next.
    doc = _FakeDocWithShapes([_FakeShape("", raise_on_get=True), _FakeShape("here is the marker")])
    assert drawing_shape_containing(doc, "marker") == "(unnamed shape)"


def test_drawing_shape_containing_fails_safe_on_count_error():
    doc = _FakeDocWithShapes([_FakeShape("marker")], raise_count=True)
    assert drawing_shape_containing(doc, "marker") is None



# --- deleting a table's text is not deleting the table ------------------------
# Regression: asked to delete a table, agents emptied its text with
# apply_document_content, got status ok, and reported success -- the table stayed.

def _range_in_table(table_name):
    from unittest.mock import MagicMock

    table = MagicMock()
    table.getName.return_value = table_name
    cursor = MagicMock()
    cursor.getPropertyValue.side_effect = lambda k: table if k == "TextTable" else None
    found = MagicMock()
    found.getText.return_value.createTextCursorByRange.return_value = cursor
    return found


def _range_in_body():
    from unittest.mock import MagicMock

    cursor = MagicMock()
    cursor.getPropertyValue.return_value = None
    found = MagicMock()
    found.getText.return_value.createTextCursorByRange.return_value = cursor
    return found


def test_emptying_a_table_cell_names_the_table_and_table_delete():
    from plugin.writer.content import _emptied_table_cell_hint

    hint = _emptied_table_cell_hint(_range_in_table("Table1"), "")
    assert hint["table_name"] == "Table1"
    assert "table_delete(name='Table1')" in hint["message"]
    assert "still in the document" in hint["message"]


def test_no_table_hint_for_real_edits_or_body_text():
    from plugin.writer.content import _emptied_table_cell_hint

    assert _emptied_table_cell_hint(_range_in_table("Table1"), "novo valor") is None
    assert _emptied_table_cell_hint(_range_in_body(), "") is None


def test_table_hint_never_raises_on_a_hostile_range():
    from unittest.mock import MagicMock

    from plugin.writer.content import _emptied_table_cell_hint

    found = MagicMock()
    found.getText.side_effect = RuntimeError("disposed")
    assert _emptied_table_cell_hint(found, "") is None
