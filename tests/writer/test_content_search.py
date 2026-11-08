# Unit tests for apply_document_content search helpers (no LibreOffice required).
from plugin.writer.search import (
    _compare_normalize,
    _paragraph_matches_part,
    build_search_not_found_response,
    closest_mismatch,
    DASH_CLASS,
    drawing_shape_containing,
    escape_for_lo_regex,
    HORIZONTAL_SPACE_CLASS,
    normalize_search_string_for_find,
    old_content_covers_most,
    prepare_old_content,
    SINGLE_QUOTE_CLASS,
    SPACE_NORMALIZE_MAP,
)
import pytest


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


def test_prepare_old_content_strips_tags_and_preserves_newlines():
    assert prepare_old_content("<p>Hello <b>World</b></p>") == "Hello World"
    assert prepare_old_content("<p>Hello</p><p>World</p>") == "Hello\nWorld"
    assert prepare_old_content(None) == ""
    assert prepare_old_content("   ") == ""


def test_compare_normalize_quotes_and_dashes():
    assert _compare_normalize("it’s") == _compare_normalize("it's")
    assert _compare_normalize("a–b") == _compare_normalize("a-b")


def test_escape_for_lo_regex_quotes_and_dashes():
    assert escape_for_lo_regex("it's") == escape_for_lo_regex("it’s")
    assert SINGLE_QUOTE_CLASS in escape_for_lo_regex("it's")
    assert DASH_CLASS in escape_for_lo_regex("a-b")


def test_paragraph_matches_part_tail_and_middle():
    ok, off = _paragraph_matches_part("prefix and ‘tail’", "'tail'", tail=True)
    assert ok is True
    ok, off = _paragraph_matches_part("prefix and tail", "prefix", tail=True)
    assert ok is False
    ok, off = _paragraph_matches_part("middle–line", "middle-line")
    assert ok is True
    ok, off = _paragraph_matches_part("middle line", "muddle line")
    assert ok is False


def test_closest_mismatch_near_miss():
    hay = "Preface. The revenue grew last year."
    needle = "The revenue grew fast"
    res = closest_mismatch(needle, hay, lenient=True)
    assert res is not None
    assert res["message"] == "closest match starts 'The revenue grew…', differs at character 17: f vs l"
    assert res["closest_match"] == "The revenue grew…"
    assert res["diff_at"] == 17


def test_closest_mismatch_no_useful_match_returns_none():
    hay = "The revenue grew last year."
    needle = "Completely unrelated search text"
    assert closest_mismatch(needle, hay, lenient=True) is None
    # Leading character mismatch should not trigger a near-miss report for position 0
    assert closest_mismatch("Revenue grew last year", hay, lenient=False) is None


def test_closest_mismatch_collapses_space_runs():
    hay = "Preface. The revenue  grew last year."
    needle = "The revenue grew fast"
    res = closest_mismatch(needle, hay, lenient=True)
    assert res is not None
    assert res["diff_at"] == 17
    assert res["closest_match"] == "The revenue  gre…"
    assert "differs at character 17: f vs l" in res["message"]


def test_closest_mismatch_lenient_quote_pair_returns_no_mismatch():
    needle = "it is fine with 'quotes'"
    hay = "it is fine with ’quotes’"
    assert closest_mismatch(needle, hay, lenient=True) is None
    res = closest_mismatch(needle, hay, lenient=False)
    assert res is not None
    assert res["diff_at"] == 16


class _FakeCursorForSteps:
    def __init__(self, length):
        self.length = length
        self.pos = 0

    def gotoStart(self, expand=False):
        self.pos = 0

    def goRight(self, n, expand=False):
        if self.pos + n <= self.length:
            self.pos += n
            return True
        self.pos = self.length
        return False


class _FakeTextForSteps:
    def __init__(self, length):
        self.length = length

    def createTextCursor(self):
        return _FakeCursorForSteps(self.length)


class _FakeDocForSteps:
    def __init__(self, length):
        self.text = _FakeTextForSteps(length)

    def getText(self):
        return self.text


def test_old_content_covers_most_proved_table():
    assert old_content_covers_most(_FakeDocForSteps(200), 120) is True
    assert old_content_covers_most(_FakeDocForSteps(200), 100) is False
    assert old_content_covers_most(_FakeDocForSteps(0), 120) is False
    assert old_content_covers_most(_FakeDocForSteps(150), 160) is True
    assert old_content_covers_most(_FakeDocForSteps(200), 0) is False
    assert old_content_covers_most(_FakeDocForSteps(50), 40) is False


def test_old_content_covers_most_boundaries():
    doc = _FakeDocForSteps(200)
    # Needle exactly half the body is not oversized
    assert old_content_covers_most(doc, 100) is False
    # Needle strictly more than half the body is oversized
    assert old_content_covers_most(doc, 101) is True
    # Needle under 100 chars is never oversized, even if it exceeds half the body
    small_doc = _FakeDocForSteps(120)
    assert old_content_covers_most(small_doc, 99) is False
    assert old_content_covers_most(small_doc, 100) is True


def test_build_search_not_found_response_oversized_with_near_miss():
    doc = _FakeDocForSteps(200)
    # 120-char needle is > half of 200-char doc, but has a closest mismatch
    needle = "A" * 120
    closest = {
        "closest_match": "A" * 16 + "…",
        "diff_at": 119,
        "message": "closest match starts '...', differs at character 119: A vs B",
    }
    resp = build_search_not_found_response(doc, needle, closest=closest)
    assert resp["status"] == "error"
    assert "target='full_document'" not in resp["message"]
    assert "closest match starts" in resp["message"]
    assert resp.get("oversized") is not True

    # When no closest mismatch is found, oversized refusal triggers
    resp_oversized = build_search_not_found_response(doc, needle, closest=None)
    assert resp_oversized["status"] == "error"
    assert "target='full_document'" in resp_oversized["message"]
    assert resp_oversized.get("oversized") is True


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


@pytest.mark.parametrize(
    "value, name, value_2, expected",
    [
        pytest.param("body of box", "Caixa de Texto 8", "of box", "Caixa de Texto 8", id="test_drawing_shape_containing_returns_name"),
        pytest.param("<O QUE ORIGINOU A DEMANDA?>", "", "ORIGINOU", "(unnamed shape)", id="test_drawing_shape_containing_unnamed_shape"),
    ],
)
def test_drawing_shape_containing_returns_name(value, name, value_2, expected):
    doc = _FakeDocWithShapes([_FakeShape(value, name=name)])
    assert drawing_shape_containing(doc, value_2) == expected

@pytest.mark.parametrize(
    "value, value_2",
    [
        pytest.param("something else", "missing", id="test_drawing_shape_containing_not_found_returns_none"),
        pytest.param("anything", "   ", id="test_drawing_shape_containing_empty_needle_returns_none"),
    ],
)
def test_drawing_shape_containing_not_found_returns_none(value, value_2):
    doc = _FakeDocWithShapes([_FakeShape(value)])
    assert drawing_shape_containing(doc, value_2) is None

def test_drawing_shape_containing_no_draw_page_returns_none():
    assert drawing_shape_containing(object(), "x") is None


def test_drawing_shape_containing_skips_failing_shape():
    # A shape whose getString raises must be skipped, not abort the scan -- the match is in the next.
    doc = _FakeDocWithShapes([_FakeShape("", raise_on_get=True), _FakeShape("here is the marker")])
    assert drawing_shape_containing(doc, "marker") == "(unnamed shape)"


def test_drawing_shape_containing_fails_safe_on_count_error():
    doc = _FakeDocWithShapes([_FakeShape("marker")], raise_count=True)
    assert drawing_shape_containing(doc, "marker") is None

# --- an empty replacement deletes a table only when it removes the last text --
# Regression: asked to delete a table, agents emptied its text with
# apply_document_content, got status ok, and reported success -- the shell stayed.
# Clearing one cell of a table that still has other text is a normal edit.

class _Cell:
    def __init__(self, text, table=None):
        self.text = text
        self.table = table

    def getString(self):
        return self.text

    def getText(self):
        return self

    def getStart(self):
        return None

    def createTextCursorByRange(self, start):
        return self

    def getPropertyValue(self, name):
        if name == "TextTable":
            return self.table
        return None


class _Match:
    def __init__(self, cell, text=None):
        self.cell = cell
        self._text = cell.getString() if text is None else text

    def getText(self):
        return self.cell

    def getString(self):
        return self._text

    def getStart(self):
        return None


class _Table:
    def __init__(self, name, cells):
        self.name = name
        self.cells = cells
        for cell in cells.values():
            cell.table = self

    def getName(self):
        return self.name

    def getCellNames(self):
        return list(self.cells)

    def getCellByName(self, name):
        return self.cells[name]


class _Nested:
    def getName(self):
        return "Inner"

    def supportsService(self, name):
        return name == "com.sun.star.text.TextTable"


class _Enum:
    def __init__(self, items):
        self._items = list(items)

    def hasMoreElements(self):
        return bool(self._items)

    def nextElement(self):
        return self._items.pop(0)


class _HostCell(_Cell):
    """A cell whose enumeration contains a nested table."""

    def createEnumeration(self):
        return _Enum([_Nested(), _Cell(self.text)])


def _names(ranges, content):
    from plugin.writer.apply_document_content import writer_tables_emptied_by_matches

    return [name for _table, name in writer_tables_emptied_by_matches(ranges, content)]


def test_empty_replacement_of_the_only_text_marks_the_table():
    table = _Table("Fee", {"A1": _Cell("Custas"), "B1": _Cell("")})
    assert _names([_Match(table.cells["A1"])], "") == ["Fee"]


@pytest.mark.parametrize(
    "value, value_2",
    [
        pytest.param("Honorarios", "", id="test_clearing_one_cell_while_another_has_text_keeps_the_table"),
        pytest.param("", "novo valor", id="test_non_empty_replacement_does_not_delete"),
    ],
)
def test_clearing_one_cell_while_another_has_text_keeps_the_table(value, value_2):
    table = _Table("Fee", {"A1": _Cell("Custas"), "B1": _Cell(value)})
    assert _names([_Match(table.cells["A1"])], value_2) == []


def test_substring_clear_keeps_the_table():
    table = _Table("Fee", {"A1": _Cell("Custas processuais"), "B1": _Cell("")})
    assert _names([_Match(table.cells["A1"], text="Custas")], "") == []

def test_matches_that_together_cover_every_cell_mark_the_table():
    table = _Table("Fee", {"A1": _Cell("Custas"), "B1": _Cell("Honorarios")})
    ranges = [_Match(table.cells["A1"]), _Match(table.cells["B1"])]
    assert _names(ranges, "") == ["Fee"]


def test_a_table_that_hosts_a_nested_table_is_not_auto_deleted():
    host = _HostCell("Caption")
    _Table("Outer", {"A1": host, "B1": _Cell("")})
    assert _names([_Match(host)], "") == []


def test_a_body_match_and_a_hostile_range_delete_nothing():
    class _Body:
        def getText(self):
            raise RuntimeError("disposed")

    assert _names([_Body()], "") == []
    body = _Cell("paragraph")
    body.table = None
    assert _names([_Match(body)], "") == []
