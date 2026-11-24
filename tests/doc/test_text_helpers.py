from unittest.mock import MagicMock, patch

import pytest

from plugin.doc.text_helpers import (
    DeletionState,
    visible_portions,
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


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("hello\r\nworld", "hello\nworld"),
        ("hello\n\rworld", "hello\nworld"),
        ("hello\rworld", "hello\nworld"),
        ("Line 1\nLine 2", "Line 1\nLine 2"),
        ("A\r\nB\rC\n\rD\nE", "A\nB\nC\nD\nE"),
        ("\r\n\r\n", "\n\n"),
        ("\n\r\n\r", "\n\n"),
        ("\r\r", "\n\n"),
        ("", ""),
        (None, ""),
    ],
)
def test_normalize_linebreaks(raw, expected):
    assert normalize_linebreaks(raw) == expected


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
    def __init__(self, text="", portion_type="Text", redline_type=None, is_start=True):
        self._text = text
        self._portion_type = portion_type
        self._redline_type = redline_type
        self._is_start = is_start

    def getPropertyValue(self, name):
        if name == "TextPortionType":
            return self._portion_type
        if name == "RedlineType":
            return self._redline_type
        if name == "IsStart":
            return self._is_start
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
                    _Portion(portion_type="Redline", redline_type="Delete", is_start=True),
                    _Portion("remove me"),
                    _Portion(portion_type="Redline", redline_type="Delete", is_start=False),
                    _Portion("text"),
                ],
                fallback_text="Keep remove metext",
            ),
            _Paragraph([_Portion("Next line")], fallback_text="Next line"),
        ],
        fallback_text="Keep remove metext\nNext line",
    )

    assert get_string_without_tracked_deletions(text_range) == "Keep text\nNext line"


def test_get_string_without_tracked_deletions_leading_end_marker_keeps_text():
    para = _Paragraph(
        [
            _Portion(portion_type="Redline", redline_type="Delete", is_start=False),
            _Portion("Keep after"),
        ],
        fallback_text="Keep after",
    )
    assert get_string_without_tracked_deletions(para) == "Keep after"


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
    assert got == "".join(chunk for _unused, chunk in visible_portions(para))


def test_get_string_without_tracked_deletions_paragraph_service():
    para = _ParagraphService(
        [_Portion("Hello "), _Portion("bold")],
        fallback_text="Hello bold",
    )
    assert get_string_without_tracked_deletions(para) == "Hello bold"


class _Shape:
    def __init__(self, text: str = "alpha\nbeta"):
        self._text = text

    def supportsService(self, name: str) -> bool:
        return name == "com.sun.star.drawing.Shape"

    def getString(self) -> str:
        return self._text

    def createEnumeration(self):
        raise AssertionError("must not enumerate")


class _ShapeIntService(_Shape):
    def supportsService(self, name: str) -> int:
        return 1 if name == "com.sun.star.drawing.Shape" else 0


def test_get_string_without_tracked_deletions_draw_shape_uses_getstring():
    shape = _Shape("alpha\nbeta")
    assert get_string_without_tracked_deletions(shape) == "alpha\nbeta"

    shape_int = _ShapeIntService("gamma")
    assert get_string_without_tracked_deletions(shape_int) == "gamma"


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
    assert list(visible_portions(para, abort_on_portion_error=True)) == []
    assert "".join(chunk for _unused, chunk in visible_portions(para)) == "later"


def test_visible_portions_truncated_out_when_cap_hit():
    para = _Paragraph([_Portion("a"), _Portion("b"), _Portion("c")])
    hit = []
    chunks = [chunk for _unused, chunk in visible_portions(para, limit=2, truncated_out=hit)]
    assert chunks == ["a", "b"]
    assert hit == [2]
    exhausted = []
    assert [c for _u, c in visible_portions(para, limit=3, truncated_out=exhausted)] == ["a", "b", "c"]
    assert exhausted == []


def test_visible_portions_shared_deletion_state_skips_middle_paragraph():
    p1 = _Paragraph([_Portion("keep "), _Portion(portion_type="Redline", redline_type="Delete", is_start=True), _Portion("del1")])
    p2 = _Paragraph([_Portion("middle deleted")])
    p3 = _Paragraph([_Portion("del3"), _Portion(portion_type="Redline", redline_type="Delete", is_start=False), _Portion(" after")])

    state = DeletionState()
    assert [c for _u, c in visible_portions(p1, state=state)] == ["keep "]
    assert [c for _u, c in visible_portions(p2, state=state)] == []
    assert [c for _u, c in visible_portions(p3, state=state)] == [" after"]

    # Without state, p2 has no markers and yields its text unchanged.
    assert [c for _u, c in visible_portions(p2)] == ["middle deleted"]


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


def test_get_document_length_is_the_visible_writer_text():
    """document_length and scope='range' share one space: the visible text with LF-normalised
    breaks and tracked deletions hidden."""
    from plugin.doc import text_helpers

    body = MagicMock()
    body.createEnumeration.return_value = _TextRange([
        _Paragraph([_Portion("ab\r\ncd")]),
        _Paragraph([_Portion("ef")]),
    ]).createEnumeration()
    model = MagicMock()
    model.getText.return_value = body
    with patch.object(text_helpers._doc_type, "get_document_type", return_value=text_helpers._doc_type.DocumentType.WRITER):
        # "ab\ncd" (5) + "\n" (1) + "ef" (2) = 8
        assert text_helpers.get_document_length(model) == 8


def test_visible_body_range_rejects_table_offset_and_clamps_end():
    from plugin.doc.text_helpers import visible_body_range
    from plugin.framework.errors import ToolExecutionError

    table = _FakeTable({"A1": _TextRange([_Paragraph([_Portion("Cell")])])})
    portion = _Portion("After")
    portion.getStart = lambda: "port_start"  # type: ignore[attr-defined]
    para = _Paragraph([portion])
    para.getStart = lambda: "p_start"  # type: ignore[attr-defined]
    para.getEnd = lambda: "p_end"  # type: ignore[attr-defined]
    body = MagicMock()
    body.createEnumeration.side_effect = lambda: _TextRange([table, para]).createEnumeration()
    body.getEnd.return_value = "doc_end"
    model = MagicMock()
    model.getText.return_value = body

    with pytest.raises(ToolExecutionError, match="offsets 0 to 4") as exc_info:
        visible_body_range(model, 2, 7)
    assert exc_info.value.code == "INVALID_PARAM"

    assert visible_body_range(model, 10, 20) is None

    got = visible_body_range(model, 5, 10**6)
    assert got is not None
    assert got.start == 5
    assert got.end == 10
    assert got.text == "After"


def test_visible_body_range_early_walk_stop_ends_at_last_para():
    from plugin.doc.text_helpers import visible_body_range

    class _BoomEnum:
        def __init__(self, first):
            self._first = first
            self._n = 0

        def hasMoreElements(self):
            return self._n < 2

        def nextElement(self):
            self._n += 1
            if self._n == 1:
                return self._first
            raise RuntimeError("enum broke")

    portion = _Portion("Keep")
    portion.getStart = lambda: "port_start"  # type: ignore[attr-defined]
    para = _Paragraph([portion])
    para.getStart = lambda: "p1_start"  # type: ignore[attr-defined]
    para.getEnd = lambda: "p1_end"  # type: ignore[attr-defined]
    cursor = MagicMock()
    body = MagicMock()
    body.createEnumeration.return_value = _BoomEnum(para)
    body.createTextCursorByRange.return_value = cursor
    model = MagicMock()
    model.getText.return_value = body

    got = visible_body_range(model, 1, 100)
    assert got is not None
    assert got.text == "eep"
    cursor.gotoRange.assert_called_once_with("p1_end", True)
    body.getEnd.assert_not_called()


def test_visible_body_range_short_go_right_raises():
    from plugin.doc.text_helpers import visible_body_range
    from plugin.framework.errors import ToolExecutionError

    portion = _Portion("Hello")
    portion.getStart = lambda: "port_start"  # type: ignore[attr-defined]
    para = _Paragraph([portion])
    cursor = MagicMock()
    cursor.goRight.return_value = False
    body = MagicMock()
    body.createEnumeration.return_value = _TextRange([para]).createEnumeration()
    body.createTextCursorByRange.return_value = cursor
    model = MagicMock()
    model.getText.return_value = body

    with pytest.raises(ToolExecutionError, match="offset 2"):
        visible_body_range(model, 2, 4)


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

def _make_selection_range(*, compare: int = 0, is_shape: bool = False) -> MagicMock:
    rng = MagicMock()
    rng.getStart.return_value = MagicMock()
    rng.getEnd.return_value = MagicMock()
    rng.getText.return_value.compareRegionStarts.return_value = compare
    if is_shape:
        rng.supportsService.side_effect = lambda s: 1 if s == "com.sun.star.drawing.Shape" else 0
    return rng


def _make_selection_ctrl(*ranges: MagicMock) -> MagicMock:
    sel = MagicMock()
    sel.getCount.return_value = len(ranges)
    sel.getByIndex.side_effect = lambda i: ranges[i]
    ctrl = MagicMock()
    ctrl.getSelection.return_value = sel
    return ctrl


def test_selected_text_range_none_controller():
    from plugin.doc.text_helpers import selected_text_range

    assert selected_text_range(None) is None


def test_selected_text_range_skips_collapsed_index0():
    from plugin.doc.text_helpers import selected_text_range

    rng0 = _make_selection_range(compare=0)
    rng1 = _make_selection_range(compare=-1)
    assert selected_text_range(_make_selection_ctrl(rng0, rng1)) is rng1


def test_selected_text_range_all_collapsed_returns_first():
    from plugin.doc.text_helpers import selected_text_range

    rng0 = _make_selection_range(compare=0)
    rng1 = _make_selection_range(compare=0)
    assert selected_text_range(_make_selection_ctrl(rng0, rng1)) is rng0


def test_selected_text_range_shape_service_returns_none():
    from plugin.doc.text_helpers import selected_text_range

    shape_rng = _make_selection_range(is_shape=True)
    assert selected_text_range(_make_selection_ctrl(shape_rng)) is None


def test_selected_text_range_disposed_raises():
    from plugin.doc.text_helpers import selected_text_range

    class DisposedException(Exception):
        pass

    ctrl = MagicMock()
    ctrl.getSelection.side_effect = DisposedException("closed")

    with pytest.raises(DocumentDisposedError):
        selected_text_range(ctrl)


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
                _Portion(portion_type="Redline", redline_type="Delete", is_start=True),
                _Portion("hidden"),
                _Portion(portion_type="Redline", redline_type="Delete", is_start=False),
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


def test_read_visible_body_hides_deletion_across_paragraphs_and_table():
    from plugin.doc.text_helpers import read_visible_body

    table = _FakeTable({
        "A1": _TextRange([_Paragraph([_Portion("CellGone")])]),
    })
    body = MagicMock()
    body.createEnumeration.return_value = _TextRange([
        _Paragraph([
            _Portion("Keep "),
            _Portion(portion_type="Redline", redline_type="Delete", is_start=True),
            _Portion("gone"),
        ]),
        table,
        _Paragraph([
            _Portion("also gone"),
            _Portion(portion_type="Redline", redline_type="Delete", is_start=False),
            _Portion("After"),
        ]),
    ]).createEnumeration()
    model = MagicMock()
    model.getText.return_value = body
    text, offsets = read_visible_body(model)
    assert text == "Keep \n\nAfter"
    assert offsets == []


def test_read_visible_body_position_in_other_text_is_none():
    from plugin.doc.text_helpers import read_visible_body

    body = MagicMock()
    body.createEnumeration.return_value = _TextRange([_Paragraph([_Portion("Hello")])]).createEnumeration()
    other = MagicMock()
    pos = MagicMock()
    pos.getText.return_value = other
    model = MagicMock()
    model.getText.return_value = body
    text, offsets = read_visible_body(model, [pos])
    assert text == "Hello"
    assert offsets == [None]


def test_read_visible_body_position_not_reached_is_none():
    from plugin.doc.text_helpers import read_visible_body

    para = _Paragraph([_Portion("Hello")])
    para.getEnd = lambda: "para_end"  # type: ignore[attr-defined]
    body = MagicMock()
    body.createEnumeration.return_value = _TextRange([para]).createEnumeration()
    body.compareRegionStarts.return_value = 1
    pos = MagicMock()
    pos.getText.return_value = body
    model = MagicMock()
    model.getText.return_value = body
    text, offsets = read_visible_body(model, [pos])
    assert text == "Hello"
    assert offsets == [None]


def test_read_visible_body_disposed():
    from plugin.doc.text_helpers import read_visible_body

    model = MagicMock()
    model.getText.side_effect = DocumentDisposedError("stale")
    with pytest.raises(DocumentDisposedError):
        read_visible_body(model)


def test_read_visible_body_measure_failure_returns_none_and_full_text():
    from plugin.doc.text_helpers import read_visible_body

    para = _Paragraph([_Portion("Hello world")])
    para.getStart = lambda: "para_start"  # type: ignore[attr-defined]
    para.getEnd = lambda: "para_end"  # type: ignore[attr-defined]
    body = MagicMock()
    body.createEnumeration.return_value = _TextRange([para]).createEnumeration()
    body.compareRegionStarts.return_value = 0
    body.createTextCursorByRange.side_effect = RuntimeError("cursor failed")
    pos = MagicMock()
    pos.getText.return_value = body
    model = MagicMock()
    model.getText.return_value = body

    text, offsets = read_visible_body(model, [pos])
    assert text == "Hello world"
    assert offsets == [None]


def test_get_document_length_non_writer_is_zero():
    from plugin.doc import text_helpers

    model = MagicMock()
    with patch.object(text_helpers._doc_type, "get_document_type", return_value=text_helpers._doc_type.DocumentType.CALC):
        assert text_helpers.get_document_length(model) == 0
    model.getText.assert_not_called()


def test_iter_elements_limited_leaves_enum_when_cap_hit():
    """Capped walks must not consume past the limit before record_walk_cap checks."""
    from plugin.doc.text_helpers import iter_elements_limited

    enum = MagicMock()
    rest = ["a", "b", "c"]
    enum.hasMoreElements.side_effect = lambda: bool(rest)
    enum.nextElement.side_effect = lambda: rest.pop(0)
    seen = 0
    for _item in iter_elements_limited(enum, 1):
        seen += 1
    assert seen == 1
    assert enum.hasMoreElements() is True


def test_starts_at_or_after():
    from plugin.doc.text_helpers import starts_at_or_after

    text = MagicMock()
    left = MagicMock()
    right = MagicMock()

    # None text -> False
    assert starts_at_or_after(None, left, right) is False

    # 1: left starts before right -> False
    text.compareRegionStarts.return_value = 1
    assert starts_at_or_after(text, left, right) is False

    # 0: left starts at right -> True
    text.compareRegionStarts.return_value = 0
    assert starts_at_or_after(text, left, right) is True

    # -1: left starts after right -> True
    text.compareRegionStarts.return_value = -1
    assert starts_at_or_after(text, left, right) is True

    # Exception during compare -> False
    text.compareRegionStarts.side_effect = RuntimeError("UNO error")
    assert starts_at_or_after(text, left, right) is False


def test_iter_elements_stops_for_magicmock():
    """`if not enum.hasMoreElements()` never stops on MagicMock (truthy mock)."""
    from plugin.doc.text_helpers import iter_elements

    assert list(iter_elements(MagicMock())) == []

    class _UnoTrue:
        def __init__(self):
            self.calls = 0

        def hasMoreElements(self):
            return self.calls == 0

        def nextElement(self):
            self.calls += 1
            return "elem"

    class _UnoFalse:
        def hasMoreElements(self):
            return False

        def nextElement(self):
            return "elem"

    assert list(iter_elements(_UnoTrue())) == ["elem"]
    assert list(iter_elements(_UnoFalse())) == []


def test_enum_hit_walk_cap_and_warning_are_honest():
    from plugin.doc import text_helpers as fmt

    more = MagicMock()
    more.hasMoreElements.return_value = True
    done = MagicMock()
    done.hasMoreElements.return_value = False

    assert fmt.record_walk_cap(more, 50, 50, "text portions") is True
    assert fmt.record_walk_cap(more, 49, 50, "text portions") is False
    assert fmt.record_walk_cap(done, 50, 50, "text portions") is False
    dest = []
    assert fmt.record_walk_cap(more, 50, 50, "text portions", dest) is True
    assert len(dest) == 1
    assert fmt.record_walk_cap(done, 50, 50, "text portions", dest) is False
    assert len(dest) == 1


def test_go_right_chunks_large_moves():
    from plugin.doc.text_helpers import go_right

    class _Cursor:
        def __init__(self):
            self.go_right_calls = []

        def goRight(self, count, expand):
            self.go_right_calls.append((count, expand))
            return True

    cursor = _Cursor()
    assert go_right(cursor, 40000, False) is True
    pre_len = sum(count for count, expand in cursor.go_right_calls)
    assert pre_len > 32767, f"pre_len {pre_len} was not large enough"
    assert all(count <= 8192 for count, expand in cursor.go_right_calls)


def test_keep_view_selection_restores_single_range_not_container():
    from plugin.doc.text_helpers import KeepViewSelection

    class _FakeText:
        def __init__(self, cmp_result: int) -> None:
            self._cmp = cmp_result

        def compareRegionStarts(self, start, end) -> int:
            return self._cmp

    class _FakeRange:
        def __init__(self, cmp_result: int) -> None:
            self._text = _FakeText(cmp_result)

        def getText(self) -> _FakeText:
            return self._text

        def getStart(self) -> str:
            return "start"

        def getEnd(self) -> str:
            return "end"

    class _FakeRanges:
        def __init__(self, items, is_text_ranges: bool = True) -> None:
            self._items = items
            self._is_text_ranges = is_text_ranges

        def supportsService(self, name: str) -> bool:
            return self._is_text_ranges and name == "com.sun.star.text.TextRanges"

        def getCount(self) -> int:
            return len(self._items)

        def getByIndex(self, idx: int):
            return self._items[idx]

    class _FakeController:
        def __init__(self, selection) -> None:
            self._selection = selection
            self.selected = []

        def getSelection(self):
            return self._selection

        def select(self, target) -> bool:
            self.selected.append(target)
            return True

    # Single non-collapsed range -> restored via the range itself, not the container.
    non_collapsed = _FakeRange(1)
    single_container = _FakeRanges([non_collapsed])
    ctrl = _FakeController(single_container)
    with KeepViewSelection(ctrl):
        pass
    assert ctrl.selected == [non_collapsed]

    # Collapsed range -> restored via the container.
    collapsed_container = _FakeRanges([_FakeRange(0)])
    ctrl_collapsed = _FakeController(collapsed_container)
    with KeepViewSelection(ctrl_collapsed):
        pass
    assert ctrl_collapsed.selected == [collapsed_container]

    # Multi-range selection -> restored via the container.
    multi_container = _FakeRanges([_FakeRange(1), _FakeRange(1)])
    ctrl_multi = _FakeController(multi_container)
    with KeepViewSelection(ctrl_multi):
        pass
    assert ctrl_multi.selected == [multi_container]

    # Non-TextRanges selection (e.g., frame or shape) -> restored via the object itself.
    frame_obj = _FakeRanges([], is_text_ranges=False)
    ctrl_frame = _FakeController(frame_obj)
    with KeepViewSelection(ctrl_frame):
        pass
    assert ctrl_frame.selected == [frame_obj]

    # None controller is a no-op.
    with KeepViewSelection(None):
        pass


def test_keep_view_selection_restore_errors():
    from plugin.doc.text_helpers import KeepViewSelection

    class DisposedException(Exception):
        pass

    class _Controller:
        def __init__(self, select_exc: Exception) -> None:
            self._select_exc = select_exc
            self._sel = object()

        def getSelection(self):
            return self._sel

        def select(self, target) -> None:
            raise self._select_exc

    # Disposal during restore propagates.
    with pytest.raises(DisposedException):
        with KeepViewSelection(_Controller(DisposedException("gone"))):
            pass

    # Non-disposal restore failure is swallowed and does not mask a body exception.
    class _BodyError(RuntimeError):
        pass

    with pytest.raises(_BodyError):
        with KeepViewSelection(_Controller(RuntimeError("select failed"))):
            raise _BodyError("body failed")

    # Non-disposal restore failure is swallowed on a normal exit.
    with KeepViewSelection(_Controller(RuntimeError("select failed"))):
        pass


def test_live_selection_range_and_snapshot():
    from plugin.doc.text_helpers import (
        get_writer_selection_positions,
        live_selection_range,
        selection_cursor_snapshot,
    )
    from plugin.framework.errors import DocumentDisposedError

    class DisposedException(Exception):
        pass

    # Selection range wins over view cursor.
    sel_rng = MagicMock()
    controller = MagicMock()
    with patch("plugin.doc.text_helpers.selected_text_range", return_value=sel_rng):
        assert live_selection_range(controller) is sel_rng
    controller.getViewCursor.assert_not_called()

    # Falls back to view cursor when selected_text_range returns None.
    vc = MagicMock()
    controller.getViewCursor.return_value = vc
    with patch("plugin.doc.text_helpers.selected_text_range", return_value=None):
        assert live_selection_range(controller) is vc

    # Both raising non-disposal errors yields None; disposal re-raises.
    bad_ctrl = MagicMock()
    bad_ctrl.getViewCursor.side_effect = RuntimeError("no view")
    with patch("plugin.doc.text_helpers.selected_text_range", side_effect=RuntimeError("no sel")):
        assert live_selection_range(bad_ctrl) is None
    with patch("plugin.doc.text_helpers.selected_text_range", side_effect=DisposedException("gone")):
        with pytest.raises(DocumentDisposedError):
            live_selection_range(bad_ctrl)

    # selection_cursor_snapshot clones the range, and returns None when getText() raises (frame case).
    doc = MagicMock()
    doc.getCurrentController.return_value = controller
    cloned = MagicMock()
    sel_rng.getText.return_value.createTextCursorByRange.return_value = cloned
    with patch("plugin.doc.text_helpers.selected_text_range", return_value=sel_rng):
        assert selection_cursor_snapshot(doc) is cloned

    frame_rng = MagicMock()
    frame_rng.getText.side_effect = RuntimeError("no text selection")
    with patch("plugin.doc.text_helpers.selected_text_range", return_value=frame_rng):
        assert selection_cursor_snapshot(doc) is None

    # Explicit selection= skips model.getCurrentController().
    model = MagicMock()
    explicit_rng = MagicMock()
    pos = get_writer_selection_positions(model, selection=explicit_rng)
    assert pos == (explicit_rng.getText.return_value, explicit_rng.getStart.return_value, explicit_rng.getEnd.return_value)
    model.getCurrentController.assert_not_called()


class _FakePageViewCursor:
    def __init__(self, page_count: int) -> None:
        self.page_count = page_count
        self.page = 1
        self.jump_calls: list[int] = []

    def jumpToPage(self, target: int) -> bool:
        if target > 0x7FFF or target < -0x8000:
            raise ValueError("VALUE is out of range!")
        self.jump_calls.append(target)
        # Matches SwRootFrame::SetCurrPage: valid 1..count lands there; anything else lands on last page.
        self.page = target if 1 <= target <= self.page_count else self.page_count
        return True

    def jumpToLastPage(self) -> bool:
        self.page = self.page_count
        return True

    def getPage(self) -> int:
        return self.page


def test_jump_view_cursor_to_page_lands_on_lo_page():
    from plugin.doc.text_helpers import jump_view_cursor_to_page

    vc = _FakePageViewCursor(3)
    assert jump_view_cursor_to_page(vc, 0) is None
    assert vc.getPage() == 1
    assert jump_view_cursor_to_page(vc, 2) is None
    assert vc.getPage() == 3


def test_jump_view_cursor_to_page_missing_page_returns_count():
    from plugin.doc.text_helpers import jump_view_cursor_to_page

    vc = _FakePageViewCursor(3)
    assert jump_view_cursor_to_page(vc, 3) == 3
    assert vc.getPage() == 3
    assert jump_view_cursor_to_page(vc, -1) == 3
    assert vc.getPage() == 3
    vc.jump_calls.clear()
    assert jump_view_cursor_to_page(vc, 40000) == 3
    assert vc.jump_calls == []


class _DeletionFakeTable:
    def __init__(self, cells):
        self._cells = cells

    def supportsService(self, name):
        return name == "com.sun.star.text.TextTable"

    def getCellNames(self):
        return tuple(self._cells.keys())

    def getCellByName(self, name):
        return self._cells[name]


class _DeletionFakeRange:
    def __init__(self, elements):
        self._elements = elements

    def supportsService(self, name):
        return False

    def createEnumeration(self):
        from tests.testing_utils import FakeUnoEnum

        return FakeUnoEnum(self._elements)


def test_deletion_state_across_three_paragraphs():
    from plugin.doc.text_helpers import DeletionState
    from tests.testing_utils import FakeUnoPara, FakeUnoPortion

    p1 = FakeUnoPara([
        FakeUnoPortion("Text", "Start "),
        FakeUnoPortion("Redline", redline_type="Delete", is_start=True),
        FakeUnoPortion("Text", "gone1"),
    ])
    p2 = FakeUnoPara([FakeUnoPortion("Text", "gone2")])
    p3 = FakeUnoPara([
        FakeUnoPortion("Text", "gone3 "),
        FakeUnoPortion("Redline", redline_type="Delete", is_start=False),
        FakeUnoPortion("Text", "Tail"),
    ])
    state = DeletionState()
    assert get_string_without_tracked_deletions(p1, state) == "Start "
    assert state.in_delete is True
    assert get_string_without_tracked_deletions(p2, state) == ""
    assert state.in_delete is True
    assert get_string_without_tracked_deletions(p3, state) == "Tail"
    assert state.in_delete is False


def test_struck_table_in_multi_paragraph_range_reads_empty():
    from tests.testing_utils import FakeUnoPara, FakeUnoPortion

    p1 = FakeUnoPara([
        FakeUnoPortion("Text", "Keep "),
        FakeUnoPortion("Redline", redline_type="Delete", is_start=True),
    ])
    table = _DeletionFakeTable({"A1": _DeletionFakeRange([FakeUnoPara([FakeUnoPortion("Text", "cellA")])])})
    p2 = FakeUnoPara([
        FakeUnoPortion("Redline", redline_type="Delete", is_start=False),
        FakeUnoPortion("Text", "End"),
    ])
    rng = _DeletionFakeRange([p1, table, p2])
    assert get_string_without_tracked_deletions(rng) == "Keep \n\nEnd"


def test_visible_portions_cap_hit_resets_open_deletion_state():
    from plugin.doc.text_helpers import DeletionState
    from tests.testing_utils import FakeUnoPara, FakeUnoPortion

    p1 = FakeUnoPara([
        FakeUnoPortion("Redline", redline_type="Delete", is_start=True),
        FakeUnoPortion("Text", "gone"),
    ])
    p2 = FakeUnoPara([FakeUnoPortion("Text", "Next")])
    state = DeletionState()
    truncated: list[int] = []
    assert list(visible_portions(p1, limit=1, truncated_out=truncated, state=state)) == []
    assert truncated == [1]
    assert state.in_delete is False
    assert [c for _p, c in visible_portions(p2, state=state)] == ["Next"]


def test_paragraph_deletion_flags_no_redlines_and_with_redlines():
    from plugin.doc.text_helpers import paragraph_deletion_flags
    from tests.testing_utils import FakeUnoPara, FakeUnoPortion

    p1 = FakeUnoPara([
        FakeUnoPortion("Text", "A"),
        FakeUnoPortion("Redline", redline_type="Delete", is_start=True),
    ])
    p2 = FakeUnoPara([FakeUnoPortion("Text", "B")])
    p3 = FakeUnoPara([
        FakeUnoPortion("Redline", redline_type="Delete", is_start=False),
        FakeUnoPortion("Text", "C"),
    ])

    no_rl_model = MagicMock()
    no_rl_model.getRedlines.return_value.hasElements.return_value = False
    assert paragraph_deletion_flags(no_rl_model, [p1, p2, p3]) == [False, False, False]

    rl_model = MagicMock()
    rl_model.getRedlines.return_value.hasElements.return_value = True
    assert paragraph_deletion_flags(rl_model, [p1, p2, p3]) == [False, True, True]


def test_tracked_deletions_state_at_and_covers():
    from plugin.doc.text_helpers import TrackedDeletions
    from tests.testing_utils import FakeUnoEnum

    class _Pos:
        def __init__(self, idx):
            self.idx = idx

        def getStart(self):
            return self

        def getEnd(self):
            return self

        def createEnumeration(self):
            return FakeUnoEnum([])

        def getText(self):
            return text

    class _Text:
        def compareRegionStarts(self, a, b):
            if not isinstance(a, _Pos) or not isinstance(b, _Pos):
                raise RuntimeError("not comparable")
            return 1 if a.idx < b.idx else (0 if a.idx == b.idx else -1)

        def compareRegionEnds(self, a, b):
            if not isinstance(a, _Pos) or not isinstance(b, _Pos):
                raise RuntimeError("not comparable")
            return 1 if a.idx < b.idx else (0 if a.idx == b.idx else -1)

    text = _Text()

    class _Range:
        def __init__(self, start, end):
            self._start = _Pos(start)
            self._end = _Pos(end)

        def getStart(self):
            return self._start

        def getEnd(self):
            return self._end

        def getText(self):
            return text

        def createEnumeration(self):
            return FakeUnoEnum([])

    class _Table:
        def __init__(self, anchor_idx):
            self._anchor = _Pos(anchor_idx)

        def supportsService(self, svc):
            return svc == "com.sun.star.text.TextTable"

        def getAnchor(self):
            return self._anchor

    class _RL:
        def __init__(self, kind, start, end):
            self._kind = kind
            self._start = start
            self._end = end

        def getPropertyValue(self, name):
            if name == "RedlineType":
                return self._kind
            if name == "RedlineStart":
                return self._start
            if name == "RedlineEnd":
                return self._end
            raise KeyError(name)

    rl_doc = MagicMock()
    rl_doc.getRedlines.return_value.hasElements.return_value = True
    rl_doc.getRedlines.return_value.createEnumeration.return_value = FakeUnoEnum([
        _RL("Delete", _Table(2), _Pos(5)),
    ])
    td = TrackedDeletions(rl_doc)
    assert bool(td) is True

    # Half-open [2, 5): at start (2) and inside (3) are in_delete; at end (5) and after (6) are not.
    assert td.state_at(_Pos(2)).in_delete is True
    assert td.state_at(_Pos(3)).in_delete is True
    assert td.state_at(_Pos(5)).in_delete is False
    assert td.state_at(_Pos(6)).in_delete is False
    assert td.state_at(_Table(2)).in_delete is True
    assert td.state_at(_Table(5)).in_delete is False

    # Not comparable -> not inside.
    assert td.state_at(MagicMock(getStart=lambda: "bad", createEnumeration=lambda: FakeUnoEnum([]))).in_delete is False

    # covers: [2, 5] inclusive.
    assert td.covers(_Range(2, 5)) is True
    assert td.covers(_Range(3, 4)) is True
    assert td.covers(_Range(1, 3)) is False
    assert td.covers(_Range(4, 6)) is False


def test_visible_portions_resets_state_when_enumeration_fails():
    from plugin.doc.text_helpers import DeletionState

    para = MagicMock()
    para.createEnumeration.side_effect = RuntimeError("no enum")
    state = DeletionState(in_delete=True)
    assert list(visible_portions(para, state=state)) == []
    assert state.in_delete is False


def test_iter_xtext_paragraphs_cell_states_and_struck_table():
    from plugin.doc.text_helpers import DeletionState, iter_xtext_paragraphs
    from tests.testing_utils import FakeUnoPara, FakeUnoPortion

    p1 = FakeUnoPara([
        FakeUnoPortion("Text", "Keep "),
        FakeUnoPortion("Redline", redline_type="Delete", is_start=True),
    ])
    struck_table = _DeletionFakeTable({"A1": _DeletionFakeRange([FakeUnoPara([FakeUnoPortion("Text", "hidden")])])})
    p2 = FakeUnoPara([
        FakeUnoPortion("Redline", redline_type="Delete", is_start=False),
        FakeUnoPortion("Text", "Mid"),
    ])
    live_table = _DeletionFakeTable({"A1": _DeletionFakeRange([FakeUnoPara([FakeUnoPortion("Text", "cell")])])})
    root = _DeletionFakeRange([p1, struck_table, p2, live_table])

    collected = []
    for para, st in iter_xtext_paragraphs(root, DeletionState()):
        collected.append(("".join(c for _p, c in visible_portions(para, state=st)), st.in_delete))
    assert collected == [("Keep ", True), ("Mid", False), ("cell", False)]


def test_build_heading_tree_drops_struck_heading_and_keeps_empty_live_heading():
    from plugin.doc.text_helpers import build_heading_tree
    from tests.testing_utils import FakeUnoEnum, FakeUnoPara, FakeUnoPortion

    h1 = FakeUnoPara([FakeUnoPortion("Text", "Live 1")], OutlineLevel=1)
    h_struck = FakeUnoPara([
        FakeUnoPortion("Redline", redline_type="Delete", is_start=True),
        FakeUnoPortion("Text", "Gone Heading"),
        FakeUnoPortion("Redline", redline_type="Delete", is_start=False),
    ], OutlineLevel=2)
    h_empty = FakeUnoPara([FakeUnoPortion("Text", "")], OutlineLevel=2)
    h2 = FakeUnoPara([FakeUnoPortion("Text", "Live 2")], OutlineLevel=1)

    body = MagicMock()
    body.createEnumeration.return_value = FakeUnoEnum([h1, h_struck, h_empty, h2])
    model = MagicMock()
    model.getText.return_value = body
    model.getRedlines.return_value.hasElements.return_value = False

    tree = build_heading_tree(model)
    assert [c["text"] for c in tree["children"]] == ["Live 1", "Live 2"]
    assert tree["children"][0]["body_paragraphs"] == 1  # h_struck counted as body
    assert [c["text"] for c in tree["children"][0]["children"]] == [""]
    assert tree["children"][1]["para_index"] == 3
