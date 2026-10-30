from unittest.mock import MagicMock, patch

import pytest

from plugin.doc.text_helpers import (
    _visible_portions,
    _writer_char_count,
    apply_chapter_number,
    chapter_number_from_para,
    clone_text_range,
    find_heading_by_chapter_number,
    get_document_path,
    get_string_without_tracked_deletions,
    normalize_linebreaks,
)
from plugin.framework.errors import DocumentDisposedError


class _LabelPara:
    def __init__(self, label=None, explode=False):
        self._label = label
        self._explode = explode

    def getPropertyValue(self, name):
        if self._explode:
            raise RuntimeError("property gone")
        if name == "ListLabelString":
            return self._label
        raise KeyError(name)


def test_chapter_number_from_para_normalizes_suffix_and_omits_empty():
    """Locked emit: rstrip('.') so Suffix='' and Suffix='.' share chapter_number:3.1."""
    assert chapter_number_from_para(_LabelPara("3.1")) == "3.1"
    assert chapter_number_from_para(_LabelPara("3.1.")) == "3.1"
    assert chapter_number_from_para(_LabelPara("1.")) == "1"
    assert chapter_number_from_para(_LabelPara("")) is None
    assert chapter_number_from_para(_LabelPara(None)) is None
    assert chapter_number_from_para(_LabelPara(".")) is None
    assert chapter_number_from_para(_LabelPara(explode=True)) is None


def test_apply_chapter_number_omits_key_when_off():
    on = {}
    apply_chapter_number(on, _LabelPara("1.2."))
    assert on == {"chapter_number": "1.2"}
    off = {}
    apply_chapter_number(off, _LabelPara(""))
    assert "chapter_number" not in off


def test_find_heading_by_chapter_number_exact_field_not_ordinal():
    tree = {
        "level": 0,
        "text": "root",
        "para_index": -1,
        "children": [
            {
                "level": 1,
                "text": "DOCUMENT 7",
                "para_index": 0,
                "children": [
                    {
                        "level": 2,
                        "text": "Geographical situation",
                        "para_index": 1,
                        "children": [],
                        "body_paragraphs": 0,
                        "chapter_number": "3.1",
                    },
                ],
                "body_paragraphs": 0,
                "chapter_number": "3",
            },
        ],
        "body_paragraphs": 0,
    }
    found = find_heading_by_chapter_number(tree, "3.1.")
    assert found is not None and found["text"] == "Geographical situation"
    assert find_heading_by_chapter_number(tree, "1.1") is None
    assert find_heading_by_chapter_number(tree, "7") is None
    assert find_heading_by_chapter_number(tree, "") is None


def test_clone_text_range_uses_range_own_xtext():
    """Must not clone through the body XText — that fails inside table cells."""
    own = MagicMock()
    cloned = MagicMock(name="cloned")
    own.createTextCursorByRange.return_value = cloned
    rng = MagicMock()
    rng.getText.return_value = own
    body = MagicMock()
    body.createTextCursorByRange.side_effect = RuntimeError(
        "End of content node doesn't have the proper start node"
    )
    assert clone_text_range(rng) is cloned
    own.createTextCursorByRange.assert_called_once_with(rng)
    body.createTextCursorByRange.assert_not_called()


def test_normalize_linebreaks():
    assert normalize_linebreaks("hello\r\nworld") == "hello\nworld"
    assert normalize_linebreaks("hello\n\rworld") == "hello\nworld"
    assert normalize_linebreaks("hello\rworld") == "hello\nworld"
    assert normalize_linebreaks("Line 1\nLine 2") == "Line 1\nLine 2"
    assert normalize_linebreaks("Line 1\r\nLine 2") == "Line 1\nLine 2"
    assert normalize_linebreaks("Line 1\rLine 2") == "Line 1\nLine 2"
    assert normalize_linebreaks("Line 1\n\rLine 2") == "Line 1\nLine 2"
    assert normalize_linebreaks("A\r\nB\rC\n\rD\nE") == "A\nB\nC\nD\nE"
    assert normalize_linebreaks("\r\n\r\n") == "\n\n"
    assert normalize_linebreaks("\n\r\n\r") == "\n\n"
    assert normalize_linebreaks("\r\r") == "\n\n"
    assert normalize_linebreaks("") == ""
    assert normalize_linebreaks(None) == ""


def test_get_document_path_repairs_legacy_file_url_and_rejects_non_file():
    def to_system_path(url):
        assert url.startswith("file://")
        return url[len("file://") :]

    with patch("plugin.doc.text_helpers.uno.fileUrlToSystemPath", side_effect=to_system_path):
        model = MagicMock()
        model.getURL.return_value = "file:/tmp/legacy.odt"
        assert get_document_path(model) == "/tmp/legacy.odt"

        model.getURL.return_value = "file:///tmp/ok.odt"
        assert get_document_path(model) == "/tmp/ok.odt"

        model.getURL.return_value = ""
        assert get_document_path(model) is None

        model.getURL.return_value = "private:factory/swriter"
        assert get_document_path(model) is None


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


class _Portion:
    def __init__(self, text="", portion_type="Text", redline_type=None):
        self._text = text
        self._portion_type = portion_type
        self._redline_type = redline_type

    def getPropertyValue(self, name):
        if name == "TextPortionType":
            return self._portion_type
        if name == "RedlineType":
            return self._redline_type
        raise Exception(name)

    def getString(self):
        return self._text


class _Paragraph:
    def __init__(self, portions, fallback_text=""):
        self._portions = portions
        self._fallback_text = fallback_text

    def createEnumeration(self):
        return _Enum(self._portions)

    def getString(self):
        return self._fallback_text


class _TextRange:
    def __init__(self, paragraphs, fallback_text=""):
        self._paragraphs = paragraphs
        self._fallback_text = fallback_text

    def createEnumeration(self):
        return _Enum(self._paragraphs)

    def getString(self):
        return self._fallback_text


def test_get_string_without_tracked_deletions_skips_deleted_portions():
    text_range = _TextRange(
        [
            _Paragraph(
                [
                    _Portion("Keep "),
                    _Portion(portion_type="Redline", redline_type="Delete"),
                    _Portion("remove me"),
                    _Portion(portion_type="Redline", redline_type="Delete"),
                    _Portion("text"),
                ],
                fallback_text="Keep remove metext",
            ),
            _Paragraph([_Portion("Next line")], fallback_text="Next line"),
        ],
        fallback_text="Keep remove metext\nNext line",
    )

    assert get_string_without_tracked_deletions(text_range) == "Keep text\nNext line"


class _ParagraphService(_Paragraph):
    def supportsService(self, name):
        return name == "com.sun.star.text.Paragraph"


def test_get_string_without_tracked_deletions_paragraph_no_mid_newline():
    """A paragraph's children are portions (e.g. a bold run), not paragraphs."""
    para = _Paragraph(
        [
            _Portion("Paragraph "),
            _Portion("with n"),
            _Portion("ormal and bold text"),
        ],
        fallback_text="Paragraph with normal and bold text",
    )

    got = get_string_without_tracked_deletions(para)
    assert got == "Paragraph with normal and bold text"
    assert "\n" not in got
    assert got == "".join(chunk for _unused, chunk in _visible_portions(para))


def test_get_string_without_tracked_deletions_paragraph_service():
    para = _ParagraphService(
        [_Portion("Hello "), _Portion("bold")],
        fallback_text="Hello bold",
    )
    assert get_string_without_tracked_deletions(para) == "Hello bold"


def test_visible_portions_helper_continues_paint_aborts():
    """Paint stops on a bad portion (offset drift); the helper continues."""

    class _BoomEnum:
        def __init__(self, items):
            self._items = list(items)
            self._idx = 0

        def hasMoreElements(self):
            return self._idx < len(self._items)

        def nextElement(self):
            item = self._items[self._idx]
            self._idx += 1
            if item == "boom":
                raise RuntimeError("portion gone")
            return item

    class _BoomPara:
        def createEnumeration(self):
            return _BoomEnum(["boom", _Portion("later")])

    para = _BoomPara()
    assert list(_visible_portions(para, abort_on_portion_error=True)) == []
    assert "".join(chunk for _unused, chunk in _visible_portions(para)) == "later"


def test_visible_portions_truncated_out_when_cap_hit():
    para = _Paragraph([_Portion("a"), _Portion("b"), _Portion("c")])
    hit = []
    chunks = [chunk for _unused, chunk in _visible_portions(para, limit=2, truncated_out=hit)]
    assert chunks == ["a", "b"]
    assert hit == [2]
    exhausted = []
    assert [c for _u, c in _visible_portions(para, limit=3, truncated_out=exhausted)] == ["a", "b", "c"]
    assert exhausted == []


def test_text_helpers_import_does_not_load_calc_analyzer():
    """LibrePy-style import: text_helpers must not pull SheetAnalyzer / CalcBridge."""
    import subprocess
    import sys
    from pathlib import Path

    repo_root = str(Path(__file__).resolve().parents[2])
    code = (
        "import sys, types\n"
        "if 'pyuno' not in sys.modules:\n"
        "    _mod = types.ModuleType('pyuno')\n"
        "    _mod.getComponentContext = lambda: None\n"
        "    sys.modules['pyuno'] = _mod\n"
        "import plugin.doc.text_helpers\n"
        "assert 'plugin.calc.analyzer' not in sys.modules\n"
        "assert 'plugin.calc.bridge' not in sys.modules\n"
        "assert 'plugin.doc.document_helpers' not in sys.modules\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=repo_root,
        capture_output=True,
        text=True,
        env={**__import__("os").environ, "PYTHONPATH": repo_root},
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_get_text_cursor_at_range_ignores_the_short_character_count():
    """#37: CharacterCount leaves out paragraph breaks and tracked deletions, so clamping to it
    cut a real offset from search_in_document(return_offsets) down to nothing."""
    from plugin.doc.text_helpers import get_text_cursor_at_range

    model = MagicMock()
    model.CharacterCount = 5  # far shorter than the offsets asked for
    cursor = model.getText.return_value.createTextCursor.return_value
    got = get_text_cursor_at_range(model, 3338, 3358)
    assert got is cursor
    assert cursor.goRight.call_args_list[0].args == (3338, False)
    assert cursor.goRight.call_args_list[-1].args == (20, True)


def test_get_text_cursor_at_range_stops_at_the_end_of_the_text():
    """Without the clamp, char_end=10**12 would run ~10**8 goRight calls; stop once one runs
    out of text (it has already moved to the end)."""
    from plugin.doc.text_helpers import get_text_cursor_at_range

    model = MagicMock()
    cursor = model.getText.return_value.createTextCursor.return_value
    cursor.goRight.return_value = False
    assert get_text_cursor_at_range(model, 10**12, 10**12 + 5) is cursor
    assert cursor.goRight.call_count == 1


class _WalkCursor:
    """Model cursor over a body of *n* cursor steps: goRight stops at the end and says False."""

    def __init__(self, text, pos):
        self.text, self.pos = text, pos

    def gotoStart(self, expand):
        self.pos = 0

    def getStart(self):
        return self

    def goRight(self, n, expand):
        if self.pos + n <= self.text.n:
            self.pos += n
            return True
        self.pos = self.text.n
        return False


class _WalkText:
    def __init__(self, n):
        self.n = n

    def createTextCursor(self):
        return _WalkCursor(self, 0)

    def createTextCursorByRange(self, rng):
        return _WalkCursor(self, rng.pos)


@pytest.mark.parametrize("n", [0, 5, 8192, 20001])
def test_writer_char_count_walks_the_real_length_not_character_count(n):
    """CharacterCount leaves out paragraph breaks and tracked deletions (54 read as 53), so the
    chat excerpt lost the end of the document."""
    from types import SimpleNamespace

    from plugin.doc.text_helpers import _writer_char_count

    text = _WalkText(n)
    assert _writer_char_count(SimpleNamespace(getText=lambda: text, CharacterCount=1)) == n


def test_get_document_length_is_the_visible_writer_text():
    """document_length and scope='range' share one space: the visible text as
    get_string_without_tracked_deletions reads it (a field or footnote number is several
    characters there but one cursor step; CharacterCount cut a range read at the end: "o dan")."""
    from plugin.doc import text_helpers

    model = MagicMock()
    with (
        patch.object(text_helpers._doc_type, "get_document_type", return_value=text_helpers._doc_type.DocumentType.WRITER),
        patch.object(text_helpers, "check_disposed"),
        patch.object(text_helpers, "get_string_without_tracked_deletions", return_value="ab\r\ncd") as visible,
    ):
        assert text_helpers.get_document_length(model) == 5
    whole = model.getText.return_value.createTextCursor.return_value
    visible.assert_called_once_with(whole)
    whole.gotoEnd.assert_called_once_with(True)


def test_get_document_length_propagates_disposal():
    from plugin.framework.errors import DocumentDisposedError
    from plugin.doc import text_helpers

    class DisposedException(Exception):
        pass

    model = MagicMock()
    model.getText.side_effect = DisposedException("closed")
    with pytest.raises(DocumentDisposedError):
        text_helpers.get_document_length(model)


def test_build_heading_tree_propagates_disposal_and_uno_error():
    from plugin.framework.errors import DocumentDisposedError, UnoObjectError
    from plugin.doc.text_helpers import build_heading_tree

    closed = MagicMock()
    closed.getText.side_effect = DocumentDisposedError("closed", object_type="Document")
    with pytest.raises(DocumentDisposedError):
        build_heading_tree(closed)

    broken = MagicMock()
    broken.getText.side_effect = RuntimeError("boom")
    with pytest.raises(UnoObjectError):
        build_heading_tree(broken)


def test_char_offset_of_other_xtext_is_not_zero():
    from plugin.doc.text_helpers import _char_offset_of_position

    class _Text(MagicMock):
        pass

    class _Pos:
        def __init__(self, text):
            self._text = text

        def getText(self):
            return self._text

    body = _Text()
    other = _Text()
    model = MagicMock()
    model.getText.return_value = body
    assert _char_offset_of_position(model, _Pos(other), 10) == -1
    model.getText.return_value.createTextCursor.assert_not_called()


def test_with_view_cursor_does_not_jump_when_clone_fails():
    from plugin.doc.text_helpers import with_view_cursor_left_body_locked

    doc = MagicMock()
    vc = MagicMock()
    vc.getText.side_effect = RuntimeError("cannot clone")
    seen = []
    result = with_view_cursor_left_body_locked(doc, vc, lambda: seen.append("ran") or "ok")
    assert result == "ok"
    assert seen == ["ran"]
    vc.gotoRange.assert_not_called()
    doc.lockControllers.assert_not_called()
    doc.getText.assert_not_called()


def test_visible_text_midwalk_failure_keeps_deletion_free_prefix():
    from plugin.doc.text_helpers import get_string_without_tracked_deletions

    class _BoomEnum:
        def __init__(self):
            self._n = 0

        def hasMoreElements(self):
            return self._n < 2

        def nextElement(self):
            self._n += 1
            if self._n == 1:
                return _Paragraph([_Portion("Keep ")], fallback_text="Keep DELETED")
            raise RuntimeError("enum broke")

    class _BoomRange:
        def createEnumeration(self):
            return _BoomEnum()

        def getString(self):
            return "Keep DELETED"

    got = get_string_without_tracked_deletions(_BoomRange())
    assert got == "Keep "
    assert "DELETED" not in got

def test_writer_char_count_disposed():
    model = MagicMock()
    model.getText.side_effect = DocumentDisposedError("stale")

    with pytest.raises(DocumentDisposedError):
        _writer_char_count(model)


def test_selected_text_range_none_controller():
    from plugin.doc.text_helpers import selected_text_range

    assert selected_text_range(None) is None


def test_selected_text_range_skips_collapsed_index0():
    from plugin.doc.text_helpers import selected_text_range

    rng0 = MagicMock()
    rng0.getStart.return_value = MagicMock()
    rng0.getEnd.return_value = MagicMock()
    rng0.getText.return_value.compareRegionStarts.return_value = 0

    rng1 = MagicMock()
    rng1.getStart.return_value = MagicMock()
    rng1.getEnd.return_value = MagicMock()
    rng1.getText.return_value.compareRegionStarts.return_value = -1

    sel = MagicMock()
    sel.getCount.return_value = 2
    sel.getByIndex.side_effect = lambda i: rng0 if i == 0 else rng1

    ctrl = MagicMock()
    ctrl.getSelection.return_value = sel

    assert selected_text_range(ctrl) is rng1


def test_selected_text_range_all_collapsed_returns_first():
    from plugin.doc.text_helpers import selected_text_range

    rng0 = MagicMock()
    rng0.getStart.return_value = MagicMock()
    rng0.getEnd.return_value = MagicMock()
    rng0.getText.return_value.compareRegionStarts.return_value = 0

    rng1 = MagicMock()
    rng1.getStart.return_value = MagicMock()
    rng1.getEnd.return_value = MagicMock()
    rng1.getText.return_value.compareRegionStarts.return_value = 0

    sel = MagicMock()
    sel.getCount.return_value = 2
    sel.getByIndex.side_effect = lambda i: rng0 if i == 0 else rng1

    ctrl = MagicMock()
    ctrl.getSelection.return_value = sel

    assert selected_text_range(ctrl) is rng0


def test_selected_text_range_shape_service_returns_none():
    from plugin.doc.text_helpers import selected_text_range

    shape_rng = MagicMock()
    shape_rng.getStart.return_value = MagicMock()
    shape_rng.getEnd.return_value = MagicMock()
    shape_rng.supportsService.side_effect = lambda s: 1 if s == "com.sun.star.drawing.Shape" else 0

    sel = MagicMock()
    sel.getCount.return_value = 1
    sel.getByIndex.return_value = shape_rng

    ctrl = MagicMock()
    ctrl.getSelection.return_value = sel

    assert selected_text_range(ctrl) is None


def test_selected_text_range_disposed_raises():
    from plugin.doc.text_helpers import selected_text_range

    class DisposedException(Exception):
        pass

    ctrl = MagicMock()
    ctrl.getSelection.side_effect = DisposedException("closed")

    with pytest.raises(DocumentDisposedError):
        selected_text_range(ctrl)


class _BsearchCursor:
    def __init__(self, text, offset=0):
        self.text = text
        self.offset = offset

    def gotoStart(self, expand=False):
        self.offset = 0

    def getStart(self):
        return self

    def getText(self):
        return self.text

    def goRight(self, n, expand=False):
        self.text.go_right_calls += 1
        self.offset = min(self.offset + n, self.text.doc_len)
        return True

    def goLeft(self, n, expand=False):
        self.text.go_left_calls += 1
        self.offset = max(self.offset - n, 0)
        return True


class _BsearchText:
    def __init__(self, doc_len):
        self.doc_len = doc_len
        self.go_right_calls = 0
        self.go_left_calls = 0

    def createTextCursor(self):
        return _BsearchCursor(self, 0)

    def createTextCursorByRange(self, start_pos):
        return _BsearchCursor(self, getattr(start_pos, "offset", 0))

    def compareRegionStarts(self, a, b):
        a_off = getattr(a, "offset", 0)
        b_off = getattr(b, "offset", 0)
        if a_off < b_off:
            return 1
        if a_off > b_off:
            return -1
        return 0


def test_char_offset_of_position_binary_search():
    from plugin.doc.text_helpers import _char_offset_of_position

    doc_len = 20000
    text = _BsearchText(doc_len)
    model = MagicMock()
    model.getText.return_value = text

    target = _BsearchCursor(text, 12345)
    offset = _char_offset_of_position(model, target, doc_len)
    assert offset == 12345
    assert text.go_left_calls == 0
    assert text.go_right_calls < 40

    # Target at 0
    text.go_right_calls = 0
    text.go_left_calls = 0
    target_0 = _BsearchCursor(text, 0)
    assert _char_offset_of_position(model, target_0, doc_len) == 0
    assert text.go_left_calls == 0
    assert text.go_right_calls < 40

    # Target at doc_len (end)
    text.go_right_calls = 0
    text.go_left_calls = 0
    target_end = _BsearchCursor(text, doc_len)
    assert _char_offset_of_position(model, target_end, doc_len) == doc_len
    assert text.go_left_calls == 0
    assert text.go_right_calls < 40


def test_char_offset_of_position_error_returns_negative_one():
    from plugin.doc.text_helpers import _char_offset_of_position

    text = MagicMock()
    text.createTextCursor.side_effect = RuntimeError("cursor creation failed")
    model = MagicMock()
    model.getText.return_value = text

    target = MagicMock()
    target.getText.return_value = text
    assert _char_offset_of_position(model, target, 1000) == -1


class _FakeTable:
    def __init__(self, cells=None, raise_names=False):
        self._cells = cells or {}
        self._raise_names = raise_names

    def supportsService(self, name):
        return name == "com.sun.star.text.TextTable"

    def getCellNames(self):
        if self._raise_names:
            raise RuntimeError("table destroyed")
        return list(self._cells.keys())

    def getCellByName(self, name):
        return self._cells.get(name)


def test_get_string_without_tracked_deletions_table_cells():
    table = _FakeTable({
        "A1": _TextRange([_Paragraph([_Portion("A1")], fallback_text="A1")]),
        "B1": _TextRange([_Paragraph([_Portion("B1")], fallback_text="B1")]),
    })
    rng = _TextRange([
        _Paragraph([_Portion("Before")], fallback_text="Before"),
        table,
        _Paragraph([_Portion("After")], fallback_text="After"),
    ])
    assert get_string_without_tracked_deletions(rng) == "Before\nA1\nB1\nAfter"


def test_get_string_without_tracked_deletions_table_cell_redline():
    table = _FakeTable({
        "A1": _TextRange([
            _Paragraph([
                _Portion("A1 "),
                _Portion(portion_type="Redline", redline_type="Delete"),
                _Portion("hidden"),
                _Portion(portion_type="Redline", redline_type="Delete"),
                _Portion("cell"),
            ], fallback_text="A1 cell"),
        ]),
        "B1": _TextRange([_Paragraph([_Portion("B1")], fallback_text="B1")]),
    })
    rng = _TextRange([
        _Paragraph([_Portion("Before")], fallback_text="Before"),
        table,
        _Paragraph([_Portion("After")], fallback_text="After"),
    ])
    assert get_string_without_tracked_deletions(rng) == "Before\nA1 cell\nB1\nAfter"


def test_get_string_without_tracked_deletions_nested_table():
    subtable = _FakeTable({
        "SubA": _TextRange([_Paragraph([_Portion("SubA")], fallback_text="SubA")]),
        "SubB": _TextRange([_Paragraph([_Portion("SubB")], fallback_text="SubB")]),
    })
    table = _FakeTable({
        "A1": _TextRange([subtable]),
        "B1": _TextRange([_Paragraph([_Portion("B1")], fallback_text="B1")]),
    })
    rng = _TextRange([
        _Paragraph([_Portion("Before")], fallback_text="Before"),
        table,
        _Paragraph([_Portion("After")], fallback_text="After"),
    ])
    assert get_string_without_tracked_deletions(rng) == "Before\nSubA\nSubB\nB1\nAfter"


def test_get_string_without_tracked_deletions_table_error_returns_empty():
    broken_table = _FakeTable(raise_names=True)
    rng = _TextRange([
        _Paragraph([_Portion("Before")], fallback_text="Before"),
        broken_table,
        _Paragraph([_Portion("After")], fallback_text="After"),
    ])
    assert get_string_without_tracked_deletions(rng) == "Before\n\nAfter"


def test_get_document_length_non_writer_is_zero():
    from plugin.doc import text_helpers

    model = MagicMock()
    with (
        patch.object(text_helpers._doc_type, "get_document_type", return_value=text_helpers._doc_type.DocumentType.CALC),
        patch.object(text_helpers, "check_disposed"),
    ):
        assert text_helpers.get_document_length(model) == 0
    model.getText.assert_not_called()


