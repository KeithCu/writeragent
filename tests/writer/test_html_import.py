
from contextlib import contextmanager
import html as html_mod
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from plugin.writer.html_export import HTML_FILTER
from plugin.writer.html_import import (
    _wrap_html_fragment,
    content_has_markup,
    ensure_html_linebreaks,
    insert_html_fragment_at_cursor,
    replace_xtext_with_html,
)




def test_mixed_math_segments_produce_separate_ruby_spans_without_tex_source():
    from plugin.writer.html_import import segment_html_with_mixed_math
    from plugin.writer.html_ruby import extract_and_strip_ruby

    content = "<p><ruby>東<rt>ひがし</rt></ruby> \\(x^2 + y^2\\) <ruby>西<rt>にし</rt></ruby></p>"
    segs = segment_html_with_mixed_math(content)
    assert len(segs) == 3
    assert segs[0].kind == "html"
    assert segs[1].kind == "tex"
    assert segs[2].kind == "html"

    _clean0, spans0 = extract_and_strip_ruby(segs[0].text)
    _clean2, spans2 = extract_and_strip_ruby(segs[2].text)

    assert spans0 == [("東", "ひがし", True, 0)]
    assert spans2 == [("西", "にし", True, 1)]



def test_ensure_html_linebreaks_does_not_unescape_again():
    """A second unescape used to turn entity-escaped markup into live tags.

    Insert/replace already unescape once. ``&amp;lt;p&amp;gt;`` arrives here as
    ``&lt;p&gt;`` and must stay an entity, not a ``<p>`` element.
    """
    once = html_mod.unescape("&amp;lt;p&amp;gt;hello&amp;lt;/p&amp;gt;")
    assert once == "&lt;p&gt;hello&lt;/p&gt;"
    out = ensure_html_linebreaks(once)
    assert "<p>hello</p>" not in out
    assert "&lt;p&gt;hello&lt;/p&gt;" in out

    inside = html_mod.unescape("<p>see &amp;lt;b&amp;gt;x&amp;lt;/b&amp;gt;</p>")
    wrapped = ensure_html_linebreaks(inside)
    assert "<b>" not in wrapped
    assert "&lt;b&gt;x&lt;/b&gt;" in wrapped
    assert "<p>see" in wrapped


def test_ensure_html_linebreaks_escapes_prose_angle_brackets():
    out = ensure_html_linebreaks("<p>List<String> and 2 < 3</p>")
    assert "List&lt;String&gt;" in out
    assert "2 &lt; 3" in out
    assert "<p>" in out and "</p>" in out


def test_insert_content_entry_points_decode_complete_entities():
    from plugin.writer import html_import

    raw = "<p>&sect 2o &amp;lt;b&amp;gt;x&amp;lt;/b&amp;gt;</p>"
    expected = "<p>&sect 2o &lt;b&gt;x&lt;/b&gt;</p>"

    model = MagicMock()
    model.getPropertyValue.return_value = False

    with patch.object(html_import, "_insert_mixed_or_plain_html") as mock_mixed:
        html_import.replace_full_document(model, MagicMock(), raw)
        assert mock_mixed.call_args.args[3] == expected

    cursor = MagicMock()
    model.getText.return_value.createTextCursor.return_value = cursor
    with patch.object(html_import, "_insert_mixed_or_plain_html") as mock_mixed:
        html_import.insert_content_at_position(model, MagicMock(), raw, "beginning")
        assert mock_mixed.call_args.args[3] == expected

    text_range = MagicMock()
    text_range.getText.return_value = MagicMock()
    with patch.object(html_import, "_insert_mixed_or_plain_html") as mock_mixed, \
         patch("plugin.writer.html_import.raise_if_range_hosts_nested_table"):
        html_import.replace_single_range_with_content(model, text_range, raw, MagicMock())
        assert mock_mixed.call_args.args[3] == expected


def test_replace_single_range_block_html_in_cell_raises_coded_error():
    from plugin.framework.errors import ToolExecutionError
    from plugin.writer import html_import

    model = MagicMock()
    text_range = MagicMock()
    cell_cursor = MagicMock()
    cell_cursor.getPropertyValue.return_value = MagicMock()  # TextTable is not None
    text_obj = MagicMock()
    text_obj.createTextCursorByRange.return_value = cell_cursor
    text_range.getText.return_value = text_obj

    with patch.object(html_import, "_insert_mixed_or_plain_html", side_effect=RuntimeError("nested xtext failure")), \
         patch("plugin.writer.html_import.raise_if_range_hosts_nested_table"):
        with pytest.raises(ToolExecutionError) as exc_info:
            html_import.replace_single_range_with_content(model, text_range, "<p>block</p>", MagicMock())
        assert exc_info.value.code == "TABLE_BLOCK_IN_CELL"
        assert "Rich/block HTML can't be inserted inside a table cell" in str(exc_info.value)


def test_replace_xtext_with_html_undo_rollback_on_import_failure():
    text = MagicMock()
    cursor = MagicMock()
    text.createTextCursor.return_value = cursor

    model = MagicMock()
    undo_mgr = MagicMock()
    undo_mgr.isLocked.return_value = False
    undo_title = None

    def enter_ctx(title):
        nonlocal undo_title
        undo_title = title

    undo_mgr.enterUndoContext.side_effect = enter_ctx
    undo_mgr.getAllUndoActionTitles.side_effect = lambda: [undo_title]
    model.getUndoManager.return_value = undo_mgr

    with patch(
        "plugin.writer.html_import.insert_html_fragment_at_cursor",
        side_effect=RuntimeError("import failed"),
    ):
        with pytest.raises(RuntimeError, match="import failed"):
            replace_xtext_with_html(text, "<p>New</p>", model=model)

    undo_mgr.enterUndoContext.assert_called_once()
    undo_mgr.leaveUndoContext.assert_called_once()
    undo_mgr.undo.assert_called_once()
    assert len(cursor.setString.call_args_list) == 1
    assert cursor.setString.call_args_list[0].args == ("",)


def _undo_model():
    model = MagicMock()
    undo_mgr = MagicMock()
    undo_mgr.isLocked.return_value = False
    model.getUndoManager.return_value = undo_mgr
    return model


def test_replace_xtext_with_html_applies_and_reports_data_lo_attrs():
    """Header/footer writes strip data-lo-* before the import and apply them after it."""
    text = MagicMock()
    imported: list[str] = []
    html = (
        '<p data-lo-style="Header" data-lo-para="margin-left:1cm; font-weight:bold">H</p>'
        '<table><tr><td><p data-lo-para="margin-top:1cm">c</p></td></tr></table>'
    )
    with patch(
        "plugin.writer.html_import.insert_html_fragment_at_cursor",
        side_effect=lambda cursor, prepared, **kw: imported.append(prepared),
    ), patch(
        "plugin.writer.html_import_styles.apply_block_lo_styles", return_value=["font-weight"],
    ) as apply_styles:
        failed = replace_xtext_with_html(text, html, model=_undo_model())

    assert "data-lo-" not in imported[0]
    args, kwargs = apply_styles.call_args
    assert args[1] is text and args[3] == ["Header"]
    assert kwargs["paras"] == ["margin-left:1cm; font-weight:bold"]
    # The table-cell declaration is never applied, so it is reported.
    assert failed.unapplied_para == ["margin-top", "font-weight"]


def test_replace_xtext_with_html_plain_html_skips_style_walk():
    text = MagicMock()
    with patch("plugin.writer.html_import.insert_html_fragment_at_cursor"), patch(
        "plugin.writer.html_import_styles.apply_block_lo_styles",
    ) as apply_styles:
        assert replace_xtext_with_html(text, "<p>New</p>", model=_undo_model()).unapplied_para == []
    apply_styles.assert_not_called()


def test_replace_xtext_with_html_undo_rollback_on_field_restore_failure():
    text = MagicMock()
    cursor = MagicMock()
    text.createTextCursor.return_value = cursor

    model = MagicMock()
    undo_mgr = MagicMock()
    undo_mgr.isLocked.return_value = False
    undo_title = None

    def enter_ctx(title):
        nonlocal undo_title
        undo_title = title

    undo_mgr.enterUndoContext.side_effect = enter_ctx
    undo_mgr.getAllUndoActionTitles.side_effect = lambda: [undo_title]
    model.getUndoManager.return_value = undo_mgr

    with patch("plugin.writer.html_import.insert_html_fragment_at_cursor"), \
         patch("plugin.writer.html_ruby.apply_ruby_spans"), \
         patch("plugin.writer.field_restore.restore_field_placeholders", side_effect=RuntimeError("field failed")):
        with pytest.raises(RuntimeError, match="field failed"):
            replace_xtext_with_html(text, "<p>New</p>", model=model)

    undo_mgr.enterUndoContext.assert_called_once()
    undo_mgr.leaveUndoContext.assert_called_once()
    undo_mgr.undo.assert_called_once()


def test_replace_xtext_with_html_undo_manager_locked_refuses_clear():
    from plugin.framework.errors import ToolExecutionError

    text = MagicMock()
    cursor = MagicMock()
    text.createTextCursor.return_value = cursor

    model = MagicMock()
    undo_mgr = MagicMock()
    undo_mgr.isLocked.return_value = True
    model.getUndoManager.return_value = undo_mgr

    with pytest.raises(ToolExecutionError, match="Could not enter undo context"):
        replace_xtext_with_html(text, "<p>New</p>", model=model)

    cursor.setString.assert_not_called()



def test_insert_inline_at_cursor_plain_text_does_not_wrap_in_p():
    """Plain text wrapped in <p> split the host paragraph when inserted mid-paragraph."""

    from plugin.writer import html_import

    cursor = MagicMock()
    with patch.object(html_import, "insert_html_fragment_at_cursor") as frag:
        html_import.insert_inline_at_cursor(MagicMock(), MagicMock(), cursor, "bem ")
    cursor.getText.return_value.insertString.assert_called_once_with(cursor, "bem ", False)
    frag.assert_not_called()


def test_insert_inline_at_cursor_keeps_edge_whitespace_of_markup():
    """The HTML import drops a fragment's edge whitespace: "<b>muito</b> " fused with the next word."""

    from plugin.writer import html_import

    cursor = MagicMock()
    cursor.getText.return_value.createTextCursor.return_value.getString.return_value = ""
    text = cursor.getText.return_value
    with patch.object(html_import, "insert_html_fragment_at_cursor") as frag:
        html_import.insert_inline_at_cursor(MagicMock(), MagicMock(), cursor, "<b>muito</b> ")
    text.insertString.assert_called_once_with(cursor, " ", False)
    cursor.goLeft.assert_called_once_with(1, False)
    frag.assert_called_once()
    assert frag.call_args.args[1] == "<b>muito</b>"
    assert frag.call_args.kwargs["wrap"] is False


def test_mixed_math_segments_stay_in_place_not_at_body_end():
    """#46/#47: after a math segment the rest of a mid-document replace went to the END of the
    body ("[Math import failed] ..." orphaned after the last paragraph)."""
    from types import SimpleNamespace

    from plugin.writer import html_import

    model, cursor = MagicMock(), MagicMock()
    parked = cursor.getText.return_value.createTextCursorByRange.return_value
    failed = SimpleNamespace(ok=False, starmath=None, error_message="no converter")
    with patch.object(html_import, "insert_html_fragment_at_cursor") as frag, \
         patch.object(html_import, "convert_latex_to_starmath", return_value=failed):
        html_import._insert_mixed_html_and_math_at_cursor(model, MagicMock(), cursor, "<p>antes \\(x\\) depois</p>")
    assert frag.call_count == 2
    cursor.getText.return_value.insertString.assert_called_once_with(cursor, "[Math import failed] x", False)
    assert cursor.gotoRange.call_args.args == (parked.getStart.return_value, False)
    model.getText.assert_not_called()


def test_insert_inline_at_cursor_does_not_unescape_plain_text_again():
    """apply_document_content.py decodes only complete references; a second html.unescape turned "&sect 2o"
    into "§ 2o"."""

    from plugin.writer import html_import

    cursor = MagicMock()
    with patch.object(html_import, "insert_html_fragment_at_cursor"):
        html_import.insert_inline_at_cursor(MagicMock(), MagicMock(), cursor, "&sect 2o ")
    cursor.getText.return_value.insertString.assert_called_once_with(cursor, "&sect 2o ", False)


def test_insert_inline_at_cursor_paints_ruby_instead_of_gluing_the_reading():
    from plugin.writer import html_import, html_ruby, range_copy

    cursor = MagicMock()
    with patch.object(html_import, "insert_html_fragment_at_cursor") as frag, \
         patch.object(range_copy, "prefix_char_count", return_value=7), \
         patch.object(html_ruby, "apply_ruby_spans") as paint:
        html_import.insert_inline_at_cursor(MagicMock(), MagicMock(), cursor, "<ruby>漢字<rt>かんじ</rt></ruby>")
    assert frag.call_args.args[1] == "漢字"
    assert paint.call_args.args[1] == [("漢字", "かんじ", True, 0)]
    assert paint.call_args.kwargs["skip_chars"] == 7


@pytest.mark.parametrize(
    "fn_name, fragment, extra_patches",
    [
        pytest.param("insert_inline_at_cursor", "<b>x</b>", {}, id="inline"),
        pytest.param("_insert_mixed_or_plain_html", "<p>hi</p>", {}, id="mixed_or_plain"),
        pytest.param(
            "_insert_mixed_html_and_math_at_cursor",
            "<p>text before</p> \\(x^2\\) <p>text after</p>",
            {"html_fragment_contains_mixed_math": True},
            id="mixed_html_and_math",
        ),
    ],
)
def test_insert_skips_prefix_count_without_ruby(fn_name, fragment, extra_patches):
    from contextlib import ExitStack
    from plugin.writer import html_import, range_copy

    cursor = MagicMock()
    with ExitStack() as stack:
        count = stack.enter_context(patch.object(range_copy, "prefix_char_count"))
        stack.enter_context(patch.object(html_import, "insert_html_fragment_at_cursor"))
        for attr, val in extra_patches.items():
            stack.enter_context(patch.object(html_import, attr, return_value=val))
        getattr(html_import, fn_name)(MagicMock(), MagicMock(), cursor, fragment)
    count.assert_not_called()


def test_insert_mixed_html_and_math_counts_prefix_when_segment_contains_ruby():
    from plugin.writer import html_import, html_ruby, range_copy

    cursor = MagicMock()
    calls = []
    with patch.object(range_copy, "prefix_char_count", side_effect=lambda t, c: calls.append("count") or 5) as count, \
         patch.object(html_import, "insert_html_fragment_at_cursor", side_effect=lambda *a, **k: calls.append("insert")), \
         patch.object(html_ruby, "apply_ruby_spans") as paint, \
         patch.object(html_import, "html_fragment_contains_mixed_math", return_value=True):
        html_import._insert_mixed_html_and_math_at_cursor(
            MagicMock(), MagicMock(), cursor, "<p><ruby>漢字<rt>かんじ</rt></ruby></p> \\(x^2\\)"
        )
    count.assert_called_once()
    assert calls == ["count", "insert"]
    assert paint.call_args.kwargs["skip_chars"] == 5




def test_full_document_in_review_mode_imports_into_a_fresh_paragraph():
    """Imported at the start of the deleted text, the new paragraphs took its character formatting
    (an 18pt bold opening heading spread over the whole document)."""

    from plugin.writer import html_import

    model = MagicMock()
    model.getPropertyValue.return_value = True  # RecordChanges
    text = model.getText.return_value
    leftover = text.createTextCursorByRange.return_value
    leftover.getString.return_value = ""
    with patch.object(html_import, "_insert_mixed_or_plain_html") as imp, \
         patch.object(html_import, "_deletion_author"):
        imp.return_value = ["margin-left"]
        dropped = html_import.replace_full_document(model, MagicMock(), "<p>x</p>")
    assert dropped == ["margin-left"]
    cursor = text.createTextCursor.return_value
    text.insertControlCharacter.assert_called_once_with(cursor, 0, False)
    cursor.goLeft.assert_called_once_with(1, False)
    leftover.goLeft.assert_called_once_with(1, True)
    leftover.setString.assert_called_once_with("")


def test_full_document_without_review_imports_in_place():
    from plugin.writer import html_import

    model = MagicMock()
    model.getPropertyValue.return_value = False
    with patch.object(html_import, "_insert_mixed_or_plain_html") as imp, \
         patch.object(html_import, "_deletion_author"):
        imp.return_value = ["margin-left"]
        dropped = html_import.replace_full_document(model, MagicMock(), "<p>x</p>")
    assert dropped == ["margin-left"]
    model.getText.return_value.insertControlCharacter.assert_not_called()
    imp.assert_called_once()


def test_deletion_author_noop_when_review_authors_missing(monkeypatch):
    """LibrePy omits review_authors; tracked-delete helper must not ImportError."""
    import contextlib
    import sys

    import plugin.writer.html_import as html_import

    monkeypatch.setitem(sys.modules, "plugin.writer.review_authors", None)
    cm = html_import._deletion_author()
    with cm:
        pass
    assert isinstance(cm, contextlib.nullcontext)


def test_html_import_avoids_document_helpers_ops_review_authors():
    """LibrePy RPS insert loads html_import.py; chat export/replace stay local imports."""
    import plugin.writer.html_import as fmt
    from tests.harness.import_guards import assert_no_toplevel_import

    assert_no_toplevel_import(fmt, {"plugin.doc.document_helpers", "ops", "review_authors"})


_FRAME = ('<div data-lo-style="Standard"><div style="float:left" class="graphic-fr1" id="Assinatura">'
          '<img style="height:1cm" alt="" src=""/></div><div style="clear:both; line-height:0;">&nbsp;</div>'
          'Pelotas.</div>')


def test_full_document_refuses_before_deleting_anything():
    from plugin.framework.errors import ToolExecutionError
    from plugin.writer import html_import
    from tests.writer.test_image_placeholders import _graphics_doc

    model = _graphics_doc()
    with pytest.raises(ToolExecutionError):
        html_import.replace_full_document(model, None, _FRAME)
    model.getText.assert_not_called()


@pytest.mark.parametrize(
    "html_input,expected",
    [
        ("<b>foo</b>", "foo"),
        ("<p>Hello</p><p>World</p>", "Hello\nWorld"),
        ("<ruby>漢字<rt>かんじ</rt></ruby>", "漢字"),
        ("  plain text search  ", "plain text search"),
    ],
)
def test_html_to_plain_text_values(html_input: str, expected: str):
    from plugin.writer.html_import import html_to_plain_text

    assert html_to_plain_text(html_input) == expected


def test_content_has_markup_cases():
    # Unclosed elements and attribute variants are markup
    assert content_has_markup("<h4>Title</h4>") is True
    assert content_has_markup("<h4>Title") is True
    assert content_has_markup('<a href="x">t</a>') is True
    assert content_has_markup('<a href="x">t') is True
    assert content_has_markup("<b class=x>t</b>") is True
    assert content_has_markup("<b class=x>t") is True

    # Unknown angle-bracket prose is not markup
    assert content_has_markup("<spanish>hi</spanish>") is False
    assert content_has_markup("a < b") is False
    assert content_has_markup("List<String>") is False

    # TeX, MathML, HTML, and ruby are markup
    assert content_has_markup(r"formula \(x\)") is True
    assert content_has_markup(r"formula \[x\]") is True
    assert content_has_markup("cost $$10") is True
    assert content_has_markup("<math>...</math>") is True
    assert content_has_markup("<b>bold</b>") is True
    assert content_has_markup("<ruby>漢字<rt>かんじ</rt></ruby>") is True
    assert content_has_markup("<p>前<ruby>漢字<rt>かんじ</rt></ruby>後</p>") is True
    assert content_has_markup("The identity $x^2$ holds.") is False

    # Markdown and plain prose are not markup
    assert content_has_markup("**bold**") is False
    assert content_has_markup("# Heading") is False
    assert content_has_markup("| col1 | col2 |") is False
    assert content_has_markup("Issue # 5") is False
    assert content_has_markup("a | b") is False
    assert content_has_markup("2**3") is False
    assert content_has_markup("__init__") is False
    assert content_has_markup("```code```") is False
    assert content_has_markup("- [ ] todo") is False
    assert content_has_markup("Jane Doe") is False
    assert content_has_markup("Hello world, this is plain text.") is False
    assert content_has_markup("") is False
    assert content_has_markup(None) is False

    from plugin.writer.html_import import content_needs_html_import
    assert content_needs_html_import("The identity $x^2$ holds.") is True
    assert content_needs_html_import("costs $5 and $7") is False
    assert content_needs_html_import("R$ 5,00 e R$ 9,00") is False




def test_styled_insert_propagates_style_pass_failure():
    from plugin.writer import html_import, html_import_styles

    cursor = MagicMock()
    cursor.getText.return_value.createTextCursor.return_value.getString.return_value = ""
    with patch.object(html_import_styles, "apply_block_lo_styles", side_effect=RuntimeError("style")), \
         patch.object(html_import, "insert_html_fragment_at_cursor"):
        with pytest.raises(RuntimeError, match="style"):
            html_import._insert_mixed_or_plain_html(
                MagicMock(), MagicMock(), cursor,
                '<p data-lo-style="Heading1">x</p>',
                apply_styles=True,
            )


def test_trailing_paragraph_not_deleted_when_portion_walk_fails():
    """A failed portion inspection is not 'empty'. Deleting can drop a frame."""

    class _Enum:
        def __init__(self, items):
            self._items = list(items)

        def hasMoreElements(self):
            return bool(self._items)

        def nextElement(self):
            return self._items.pop(0)

    class _Para:
        def __init__(self, text, boom=None):
            self._text = text
            self._boom = boom

        def getString(self):
            return self._text

        def createEnumeration(self):
            if self._boom is not None:
                raise self._boom
            raise AssertionError("only the empty paragraph is walked")

    deleted = []

    class _Text:
        def getString(self):
            return ""

        def createTextCursor(self):
            cursor = MagicMock()
            cursor.getString.return_value = "Old"
            return cursor

        def createEnumeration(self):
            return _Enum([_Para("kept"), _Para("", boom=RuntimeError("portion"))])

        def createTextCursorByRange(self, _para):
            deleted.append("delete")
            return MagicMock()

    model = MagicMock()
    model.findFirst.return_value = None
    undo_mgr = MagicMock()
    undo_mgr.isLocked.return_value = False
    model.getUndoManager.return_value = undo_mgr

    with patch("plugin.writer.html_import.insert_html_fragment_at_cursor"):
        replace_xtext_with_html(_Text(), "<p>New</p>", model=model)
    assert deleted == []


def test_trailing_paragraph_still_deleted_when_walk_is_empty_text():
    class _Enum:
        def __init__(self, items):
            self._items = list(items)

        def hasMoreElements(self):
            return bool(self._items)

        def nextElement(self):
            return self._items.pop(0)

    class _Portion:
        def getString(self):
            return ""

        def getPropertyValue(self, name):
            assert name == "TextPortionType"
            return "Text"

    class _Para:
        def __init__(self, text, walk):
            self._text = text
            self._walk = walk

        def getString(self):
            return self._text

        def createEnumeration(self):
            return _Enum(self._walk)

        def getEnd(self):
            return object()

    deleted = []

    class _Text:
        def getString(self):
            return ""

        def createTextCursor(self):
            cursor = MagicMock()
            cursor.getString.return_value = "Old"
            return cursor

        def createEnumeration(self):
            return _Enum([_Para("kept", []), _Para("", [_Portion()])])

        def createTextCursorByRange(self, _para):
            cursor = MagicMock()
            deleted.append("delete")
            return cursor

    model = MagicMock()
    model.findFirst.return_value = None
    undo_mgr = MagicMock()
    undo_mgr.isLocked.return_value = False
    model.getUndoManager.return_value = undo_mgr

    with patch("plugin.writer.html_import.insert_html_fragment_at_cursor"):
        replace_xtext_with_html(_Text(), "<p>New</p>", model=model)
    assert deleted == ["delete"]


def test_trailing_paragraph_disposal_is_not_success():
    from plugin.framework.errors import DocumentDisposedError

    class _Enum:
        def __init__(self, items):
            self._items = list(items)

        def hasMoreElements(self):
            return bool(self._items)

        def nextElement(self):
            return self._items.pop(0)

    class _Para:
        def getString(self):
            return ""

        def createEnumeration(self):
            raise type("DisposedException", (Exception,), {})("gone")

    class _Text:
        def getString(self):
            return ""

        def createTextCursor(self):
            cursor = MagicMock()
            cursor.getString.return_value = "Old"
            return cursor

        def createEnumeration(self):
            return _Enum([_Para(), _Para()])

    model = MagicMock()
    model.findFirst.return_value = None
    undo_mgr = MagicMock()
    undo_mgr.isLocked.return_value = False
    undo_title = None

    def enter_ctx(title):
        nonlocal undo_title
        undo_title = title

    undo_mgr.enterUndoContext.side_effect = enter_ctx
    undo_mgr.getAllUndoActionTitles.side_effect = lambda: [undo_title]
    model.getUndoManager.return_value = undo_mgr

    with patch("plugin.writer.html_import.insert_html_fragment_at_cursor"):
        with pytest.raises(DocumentDisposedError):
            replace_xtext_with_html(_Text(), "<p>New</p>", model=model)


def test_uno_read_probes_raise_on_disposal_and_default_otherwise():
    from plugin.framework.errors import DocumentDisposedError
    from plugin.writer.html_import import _cursor_paragraph_has_visible_text, _uno_service_true
    from plugin.writer.html_import_styles import resolve_paragraph_style_token

    class DisposedException(Exception):
        pass

    class RuntimeException(Exception):
        pass

    closed = MagicMock()
    closed.supportsService.side_effect = DisposedException("closed")
    with pytest.raises(DocumentDisposedError):
        _uno_service_true(closed, "com.sun.star.drawing.Shape")

    odd = MagicMock()
    odd.supportsService.side_effect = RuntimeException("uno")
    assert _uno_service_true(odd, "com.sun.star.drawing.Shape") is False

    # Fail safe: an unreadable paragraph still counts as non-empty.
    cursor = MagicMock()
    cursor.getText.side_effect = RuntimeException("uno")
    assert _cursor_paragraph_has_visible_text(cursor) is True

    fam = MagicMock()
    names = ("Heading 1", "Heading 1", "Text Body")
    fam.getElementNames.return_value = names
    fam.hasByName.return_value = True
    assert resolve_paragraph_style_token(fam, "Heading1", element_names=names) == "Heading 1"




def test_styled_math_insert_styles_from_paragraph_before_insert():
    """The math path moves the cursor past the content, so styling starts after the paragraph before it."""
    from plugin.writer import html_import, html_import_styles

    cursor = MagicMock()
    before = MagicMock()
    start = MagicMock()
    with patch.object(html_import, "_insert_mixed_html_and_math_at_cursor"), \
         patch.object(html_import, "_paragraph_before", return_value=before), \
         patch.object(html_import, "_paragraph_after", return_value=start) as pa, \
         patch.object(html_import_styles, "apply_block_lo_styles") as apply:
        html_import._insert_mixed_or_plain_html(
            MagicMock(), MagicMock(), cursor,
            '<p data-lo-style="Heading1">Equation \\(x^2\\) here</p>',
            apply_styles=True,
        )
    assert pa.call_args.args[1] is before
    assert apply.call_args.args[2] is start


def test_paragraph_after_none_is_text_start():
    from plugin.writer import html_import

    text = MagicMock()
    html_import._paragraph_after(text, None)
    text.createTextCursorByRange.assert_called_once_with(text.getStart.return_value)


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        ("", False),
        (None, False),
        ("hello world", False),
        ("<b>bold</b> text", False),
        ('<span style="background: transparent">Title</span>', False),
        ("<span>", False),
        ("<param>", False),
        ("List<String>", False),
        ("<span>a<br>b</span>", False),
        ("<p>a paragraph</p>", True),
        ("<h1>title</h1>", True),
        ("<h3>x</h3>", True),
        ("<ul><li>x</li></ul>", True),
        ("<P>X</P>", True),
        ("<p\n style=a>", True),
        ("<p\tx>", True),
        ("<dl><dt>x</dt></dl>", True),
        ("<figure></figure>", True),
        ("<header>h</header>", True),
    ],
)
def test_content_has_block_markup(html, expected):
    """R3: pins the block-vs-inline classification that gates the clear 'rich HTML in a table cell' error in replace_single_range_with_content; inline tags go through the in-cell path."""
    from plugin.writer.html_import import content_has_block_markup

    assert content_has_block_markup(html) is expected


def test_collect_range_portions_magic_mock_returns_immediately():
    from plugin.writer.html_import import _collect_range_portions

    mock_range = MagicMock()
    has_special, portions = _collect_range_portions(mock_range)
    assert has_special is False
    assert portions == []


def make_paragraph_edge_range(is_start: bool, is_end: bool) -> MagicMock:
    """Mock an XTextRange whose start and end cursors report paragraph boundary status."""
    start_cur = MagicMock()
    start_cur.isStartOfParagraph.return_value = is_start
    end_cur = MagicMock()
    end_cur.isEndOfParagraph.return_value = is_end
    text = MagicMock()
    text.createTextCursorByRange.side_effect = lambda pos: start_cur if pos == "start" else end_cur
    rng = MagicMock()
    rng.getText.return_value = text
    rng.getStart.return_value = "start"
    rng.getEnd.return_value = "end"
    rng.insertDocumentFromURL = MagicMock()
    return rng


@pytest.mark.parametrize(
    ("content", "recording", "start_edge", "end_edge", "expected_apply", "expected_note"),
    [
        ('<p data-lo-style="Heading2">content</p>', False, True, True, True, None),
        ('<p data-lo-style="Heading2">content</p>', False, True, False, False, "STYLE_IGNORED_PARTIAL_MATCH"),
        ('<p data-lo-style="Heading2">content</p>', False, False, True, False, "STYLE_IGNORED_PARTIAL_MATCH"),
        ('<p data-lo-style="Heading2">content</p>', False, False, False, False, "STYLE_IGNORED_PARTIAL_MATCH"),
        ('<p data-lo-para="text-align:center">content</p>', False, True, True, True, None),
        ('<p data-lo-para="text-align:center">content</p>', False, True, False, False, "STYLE_IGNORED_PARTIAL_MATCH"),
        ('<p data-lo-para="text-align:center">content</p>', False, False, True, False, "STYLE_IGNORED_PARTIAL_MATCH"),
        ('<p data-lo-para="text-align:center">content</p>', False, False, False, False, "STYLE_IGNORED_PARTIAL_MATCH"),
        ('<p data-lo-style="Heading2">content</p>', True, True, True, False, "STYLE_IGNORED_RECORDING"),
        # Geometry checked first: partial match while recording returns partial note.
        pytest.param('<p data-lo-style="Heading2">content</p>', True, False, True, False, "STYLE_IGNORED_PARTIAL_MATCH", id="style_partial_while_recording"),
        ('<p data-lo-para="text-align:center">content</p>', True, True, True, False, "STYLE_IGNORED_RECORDING"),
        pytest.param('<p data-lo-para="text-align:center">content</p>', True, False, True, False, "STYLE_IGNORED_PARTIAL_MATCH", id="para_partial_while_recording"),
    ],
)
def test_lo_style_for_replaced_range(content, recording, start_edge, end_edge, expected_apply, expected_note):
    from plugin.writer.html_import import _lo_style_for_replaced_range

    with patch("plugin.writer.record_changes.is_recording_changes", return_value=recording):
        apply, note = _lo_style_for_replaced_range(
            MagicMock(), content, make_paragraph_edge_range(start_edge, end_edge)
        )
    assert apply is expected_apply
    assert note == expected_note


def test_lo_style_for_replaced_range_non_block_tokens():
    from plugin.writer.html_import import _lo_style_for_replaced_range

    model = MagicMock()
    rng = MagicMock()

    # Span token -> (False, None), range cursor methods not probed
    apply, note = _lo_style_for_replaced_range(model, '<span data-lo-style="Heading2">x</span>', rng)
    assert apply is False
    assert note is None
    rng.getText.assert_not_called()

    # Body-text mention -> (False, None)
    apply, note = _lo_style_for_replaced_range(model, '<p>The attribute data-lo-style is cool</p>', rng)
    assert apply is False
    assert note is None
    rng.getText.assert_not_called()

    # Table-only token -> (False, None)
    apply, note = _lo_style_for_replaced_range(
        model, '<table><tr><td data-lo-style="Heading2">cell</td></tr></table>', rng
    )
    assert apply is False
    assert note is None
    rng.getText.assert_not_called()

    # Span para -> (False, None), range cursor methods not probed
    rng.reset_mock()
    apply, note = _lo_style_for_replaced_range(model, '<span data-lo-para="text-align:center">x</span>', rng)
    assert apply is False
    assert note is None
    rng.getText.assert_not_called()

    # Table-only para -> (False, None)
    rng.reset_mock()
    apply, note = _lo_style_for_replaced_range(
        model, '<table><tr><td data-lo-para="text-align:center">cell</td></tr></table>', rng
    )
    assert apply is False
    assert note is None
    rng.getText.assert_not_called()


def test_styled_insert_epilogue_cursor_move():
    from plugin.writer import html_import

    model = MagicMock()
    model_text = MagicMock()
    model.getText.return_value = model_text

    cursor = MagicMock()
    cursor_text = MagicMock()
    cursor.getText.return_value = cursor_text

    parked = MagicMock()
    cursor_text.createTextCursorByRange.return_value = parked

    with patch.object(html_import, "insert_html_fragment_at_cursor"), \
         patch("plugin.writer.html_import_styles.apply_block_lo_styles"), \
         patch.object(html_import, "_apply_ruby_and_restore_fields"):
        html_import._insert_mixed_or_plain_html(
            model, MagicMock(), cursor,
            '<p data-lo-style="Heading1">Heading</p>',
            apply_styles=True,
        )

    # cursor.gotoRange received the parked cursor's start
    cursor.gotoRange.assert_called_with(parked.getStart.return_value, False)
    # model.getText().createTextCursor is not called
    model_text.createTextCursor.assert_not_called()

@pytest.mark.parametrize(
    ("html", "expected_count", "expected_headings"),
    [
        # Unclosed tag closes when the next paragraph begins.
        ("<h1>Title<p>Next", 2, [{"level": 1, "text": "Title"}]),
        # data-lo-style to non-heading style overrides the heading tag.
        ('<h1 data-lo-style="Quotations">Title</h1>', 1, []),
        # Heading11 is beyond LibreOffice heading level 1-10 range.
        ('<p data-lo-style="Heading11">Title</p>', 1, []),
        # Shared first child slot: outer style token overrides inner tag level.
        ('<blockquote data-lo-style="Heading2"><h1>Title</h1></blockquote>', 1, [{"level": 2, "text": "Title"}]),
        ('<li data-lo-style="Heading3"><h1>Title</h1></li>', 1, [{"level": 3, "text": "Title"}]),
        ('<blockquote data-lo-style="Quotations"><h1>Title</h1></blockquote>', 1, []),
        # Entity references decoded and nested markup text combined.
        ("<h1>Tom &amp; <b>Jerry</b></h1>", 1, [{"level": 1, "text": "Tom & Jerry"}]),
        # Heading text longer than 120 chars is truncated with an ellipsis.
        ("<h1>" + "A" * 130 + "</h1>", 1, [{"level": 1, "text": "A" * 120 + "…"}]),
        # Styled div counts as a paragraph slot.
        ('<div data-lo-style="Heading1">Hello</div>', 1, [{"level": 1, "text": "Hello"}]),
        # <br> inside paragraph separates text with whitespace.
        ('<p data-lo-style="Heading1">Hello<br>World</p>', 1, [{"level": 1, "text": "Hello World"}]),
        # Plain text without HTML tags: blank lines delimit paragraphs.
        ("Hello\n\nWorld", 2, []),
        ("Hello\nWorld", 1, []),
        # Ruby markup without math is stripped to base text.
        ("<ruby>漢字<rt>かんじ</rt></ruby>", 1, []),
        # Encoded HTML tags in prose are decoded before summarization.
        ("<p>Intro</p>&lt;h2&gt;Sub&lt;/h2&gt;", 2, [{"level": 2, "text": "Sub"}]),
    ],
)
def test_summarize_written_blocks_fixtures(html: str, expected_count: int, expected_headings: list[dict[str, Any]]) -> None:
    from plugin.writer.html_import_styles import summarize_written_blocks

    summary = summarize_written_blocks(html)
    assert summary["paragraph_count"] == expected_count
    assert summary["headings"] == expected_headings


def test_summarize_written_blocks_drops_specified_heading_styles():
    from plugin.writer.html_import_styles import summarize_written_blocks

    html = '<p data-lo-style="Heading1">One</p><p data-lo-style="Heading2">Two</p>'
    summary = summarize_written_blocks(html, drop_heading_styles={"Heading1"})
    assert summary["paragraph_count"] == 2
    assert summary["headings"] == [{"level": 2, "text": "Two"}]


def test_summarize_written_blocks_matches_extractor_slot_counts():
    """Verify summarize_written_blocks paragraph counts match _BlockLoStyleExtractor.styles."""
    from plugin.writer.html_import_styles import _BlockLoStyleExtractor, summarize_written_blocks

    fixtures = [
        "<blockquote><p>Quote</p></blockquote>",
        "<ul><li>Item 1</li><li>Item 2</li></ul>",
        "<table><tr><td>Cell</td></tr></table><p>After</p>",
        '<p data-lo-style="Heading 10">Ten</p>',
    ]
    for html in fixtures:
        ex = _BlockLoStyleExtractor()
        ex.feed(html)
        ex.close()
        summary = summarize_written_blocks(html)
        assert summary["paragraph_count"] == len(ex.styles), f"Mismatch for {html}"


def test_insert_mixed_html_and_math_reports_every_attempt():
    from plugin.writer import html_import

    model = MagicMock()
    ctx = MagicMock()
    cursor = MagicMock()

    content = r"<p>Start</p> \(x+1\) between $$y=2$$ and <math display='block'><mi>z</mi></math> end"

    def fake_tex(ctx, text, *, display_block=False):
        if text == "x+1":
            return MagicMock(ok=True, starmath="x + 1", error_message=None)
        return MagicMock(ok=False, starmath=None, error_message="tex syntax error")

    def fake_mathml(ctx, text):
        return MagicMock(ok=True, starmath="z", error_message=None)

    with patch.object(html_import, "convert_latex_to_starmath", side_effect=fake_tex), \
         patch.object(html_import, "convert_mathml_to_starmath", side_effect=fake_mathml), \
         patch.object(html_import, "insert_writer_math_formula") as mock_formula, \
         patch.object(html_import, "insert_html_fragment_at_cursor"):
        attempts = html_import._insert_mixed_html_and_math_at_cursor(model, ctx, cursor, content)

    assert attempts == [
        {"kind": "tex", "display_block": False, "snippet": "x+1", "ok": True},
        {"kind": "tex", "display_block": True, "snippet": "y=2", "ok": False, "error": "tex syntax error"},
        {"kind": "mathml", "display_block": True, "snippet": "<math display='block'><mi>z</mi></math>", "ok": True},
    ]
    assert mock_formula.call_count == 2
    cursor.getText.return_value.insertString.assert_called_once_with(
        cursor, "[Math import failed] y=2", False
    )


def test_mixed_inserters_report_math_and_preserve_returns():
    from plugin.writer import html_import

    model = MagicMock()
    model.getPropertyValue.return_value = False
    cursor = MagicMock()
    model.getText.return_value.createTextCursor.return_value = cursor
    text_range = MagicMock()
    text_range.getText.return_value = MagicMock()
    ctx = MagicMock()

    fake_attempt = [{"kind": "tex", "display_block": False, "snippet": "a", "ok": True}]

    def fake_mixed_math(mod, c, cur, unescaped):
        return list(fake_attempt)

    # 1. replace_full_document returns WriteReport with math
    with patch.object(html_import, "_insert_mixed_html_and_math_at_cursor", side_effect=fake_mixed_math), \
         patch.object(html_import, "html_fragment_contains_mixed_math", return_value=True):
        ret_full = html_import.replace_full_document(model, ctx, r"<p>\(a\)</p>")
    assert ret_full.unapplied_para == []
    assert ret_full.math == fake_attempt

    # 2. insert_content_at_position(..., "end") returns WriteReport with math
    with patch.object(html_import, "_insert_mixed_html_and_math_at_cursor", side_effect=fake_mixed_math), \
         patch.object(html_import, "html_fragment_contains_mixed_math", return_value=True):
        ret_end = html_import.insert_content_at_position(model, ctx, r"<p>\(a\)</p>", "end")
    assert ret_end.unapplied_para == []
    assert ret_end.math == fake_attempt

    # 3. replace_single_range_with_content returns WriteReport with math
    with patch.object(html_import, "_insert_mixed_html_and_math_at_cursor", side_effect=fake_mixed_math), \
         patch("plugin.writer.html_import.raise_if_range_hosts_nested_table"), \
         patch.object(html_import, "html_fragment_contains_mixed_math", return_value=True):
        ret_range = html_import.replace_single_range_with_content(
            model, text_range, r"<p>\(a\)</p>", ctx
        )
    assert ret_range.unapplied_para == []
    assert ret_range.math == fake_attempt


def test_replace_single_range_returns_failures_and_inline_skipped():
    from plugin.writer import html_import, html_import_styles

    model = MagicMock()
    ctx = MagicMock()
    text_range = MagicMock()
    text_obj = MagicMock()
    text_range.getText.return_value = text_obj
    cursor = MagicMock()
    text_obj.createTextCursorByRange.return_value = cursor

    # Block replacement returns failures from _insert_mixed_or_plain_html
    fake_report = html_import_styles.WriteReport(unapplied_para=["font-size"])
    with patch("plugin.writer.html_import.raise_if_range_hosts_nested_table"), \
         patch.object(html_import, "_insert_mixed_or_plain_html", return_value=fake_report) as mock_ins, \
         patch.object(html_import, "_lo_style_for_replaced_range", return_value=(True, None)):
        rep = html_import.replace_single_range_with_content(
            model, text_range, '<p data-lo-para="font-size:12pt">text</p>', ctx
        )
    assert rep.unapplied_para == ["font-size"]
    assert rep.style_skips == []
    mock_ins.assert_called_once()

    # Inline replacement returns property names from skipped inline tags
    cursor.getPropertyValue.return_value = "Standard"
    with patch("plugin.writer.html_import.raise_if_range_hosts_nested_table"), \
         patch.object(html_import, "insert_html_fragment_at_cursor"), \
         patch("plugin.writer.record_changes.is_recording_changes", return_value=False):
        rep_inline = html_import.replace_single_range_with_content(
            model, text_range, '<span data-lo-para="text-align:center; font-size:10pt">inline</span>', ctx
        )
    assert rep_inline.unapplied_para == ["text-align", "font-size"]
    assert rep_inline.style_skips == []


def test_plain_html_leaves_math_empty():
    from plugin.writer import html_import

    model = MagicMock()
    cursor = MagicMock()
    ctx = MagicMock()

    with patch.object(html_import, "insert_html_fragment_at_cursor"):
        report = html_import._insert_mixed_or_plain_html(
            model, ctx, cursor, "<p>hi</p>"
        )
    assert report.unapplied_para == []
    assert report.math == []


class TestWriterMathPreservation:
    def test_mixed_math_preservation(self):
        """Verify that \\nabla in TeX is preserved while \\n in HTML is expanded."""
        from plugin.writer.html_import import _insert_mixed_html_and_math_at_cursor

        ctx = MagicMock()
        model = MagicMock()
        cursor = MagicMock()

        # r"\nabla" has 1 backslash, r"\n" has 1 backslash.
        test_content = r"<p>Equation: \[\nabla \cdot \mathbf{E}\]</p> and a newline: \nHello."

        with patch("plugin.writer.html_import.insert_html_fragment_at_cursor") as mock_insert_html:
            with patch("plugin.writer.html_import.convert_latex_to_starmath") as mock_convert_tex:
                mock_convert_tex.return_value = MagicMock(ok=True, starmath="mocked")

                _insert_mixed_html_and_math_at_cursor(model, ctx, cursor, test_content)

                # Check that HTML segment 2 (with the newline) was expanded
                calls = mock_insert_html.call_args_list
                last_html_chunk = calls[-1][0][1]
                assert "\nHello." in last_html_chunk
                assert "\\nHello." not in last_html_chunk

                # Verify TeX segment 1: \nabla was NOT expanded
                tex_chunk = mock_convert_tex.call_args[0][1]
                assert tex_chunk == r"\nabla \cdot \mathbf{E}"

    def test_brl_currency_never_becomes_a_formula(self):
        """End-to-end insert path: ``R$ 1.234,56`` pairs must not build a Math object.

        Regression for the Writer corruption where the text between two ``R$``
        amounts was swallowed into a formula (italic serif, spaces eaten).
        """
        from plugin.writer.html_import import _insert_mixed_or_plain_html

        ctx = MagicMock()
        model = MagicMock()
        cursor = MagicMock()
        cursor.getText.return_value.createTextCursor.return_value.getString.return_value = ""

        content = (
            "<p>Requer a condena\u00e7\u00e3o em R$ 12.798,82, como montante "
            "indenizat\u00f3rio por ventura arbitrado, evitando-se, assim, o "
            "enriquecimento sem causa, al\u00e9m de R$ 500,00 de custas.</p>"
        )

        with patch("plugin.writer.html_import.insert_html_fragment_at_cursor") as mock_insert_html:
            with patch("plugin.writer.html_import.convert_latex_to_starmath") as mock_convert_tex:
                with patch("plugin.writer.html_import.insert_writer_math_formula") as mock_formula:
                    _insert_mixed_or_plain_html(model, ctx, cursor, content)

        mock_convert_tex.assert_not_called()
        mock_formula.assert_not_called()
        inserted = "".join(c[0][1] for c in mock_insert_html.call_args_list)
        assert "R$ 12.798,82" in inserted
        assert "R$ 500,00" in inserted
        assert "evitando-se, assim" in inserted

    def test_plain_html_expansion(self):
        """Verify that expansion still happens when no math is present."""
        from plugin.writer.html_import import _insert_mixed_or_plain_html

        ctx = MagicMock()
        model = MagicMock()
        cursor = MagicMock()
        cursor.getText.return_value.createTextCursor.return_value.getString.return_value = ""

        test_content = "Line 1\\nLine 2"

        with patch("plugin.writer.html_import.insert_html_fragment_at_cursor") as mock_insert_html:
            _insert_mixed_or_plain_html(model, ctx, cursor, test_content)

            inserted_html = mock_insert_html.call_args[0][1]
            assert "Line 1" in inserted_html
            assert "Line 2" in inserted_html
            assert "Line 1\\n" not in inserted_html

    def test_apply_document_content_plain_text_expansion(self):
        """Verify that ApplyDocumentContent expands \\n for plain text."""
        from plugin.writer.apply_document_content import ApplyDocumentContent
        from plugin.writer.html_import_styles import WriteReport

        tool = ApplyDocumentContent()
        ctx = MagicMock()
        ctx.doc = MagicMock()
        # The lock rule is True. Keep isLocked.return_value = False.
        ctx.doc.getUndoManager.return_value.isLocked.return_value = False
        ctx.ctx = MagicMock()
        ctx.services = MagicMock()

        # Plain text content (no markup)
        content = "Line 1\\nLine 2"

        with (
            patch("plugin.writer.html_import.replace_full_document") as mock_replace,
            # doc.agent_edit_review_mode is read from live config by both modules; pin off for isolated runs without init_config.
            patch("plugin.writer.apply_document_content.get_agent_edit_review_mode", return_value="off"),
            patch("plugin.writer.edit_review.get_agent_edit_review_mode", return_value="off"),
        ):
            mock_replace.return_value = WriteReport()
            tool.execute(ctx, content=content, target="full_document")

            passed_content = mock_replace.call_args[0][2]
            assert passed_content == "Line 1\nLine 2"


def test_conversion():
    # 1. Plain text with single newlines
    inp = "Line 1\nLine 2"
    expected_body = "<p>Line 1<br>\nLine 2</p>"
    result = ensure_html_linebreaks(inp)
    assert expected_body in result
    assert "<!DOCTYPE html>" in result

    # 2. Plain text with double newlines
    inp = "Para 1\n\nPara 2"
    expected_body = "<p>Para 1</p>\n<p>Para 2</p>"
    result = ensure_html_linebreaks(inp)
    assert expected_body in result

    # 3. Text with basic HTML tags (should be untouched, but wrapped)
    inp = "<h1>Title</h1>\n<p>Body</p>"
    result = ensure_html_linebreaks(inp)
    assert inp in result

    # 4. Mix of single and double newlines
    inp = "Title\nSubtitle\n\nMain content\nMore content"
    expected_body = "<p>Title<br>\nSubtitle</p>\n<p>Main content<br>\nMore content</p>"
    result = ensure_html_linebreaks(inp)
    assert expected_body in result


def test_full_document_not_double_wrapped():
    # Docling + css-inline: single wrapper, body markup preserved (not nested <!DOCTYPE>).
    inp = (
        '<!DOCTYPE html><html style="font-family: Arial"><head></head>'
        '<body><h2 style="font-weight: bold">Title</h2><p>Body</p></body></html>'
    )
    result = ensure_html_linebreaks(inp)
    assert result.lower().count("<html>") == 1, result
    assert result.lower().count("<body>") == 1, result
    assert '<h2 style="font-weight: bold">Title</h2>' in result, result
    assert "<p>Body</p>" in result, result


def test_block_only_html_not_wrapped_in_p_or_br():
    for tag_sample in (
        "<table><tr><td>A</td></tr></table>",
        "<h4>T</h4>",
        "<dl><dt>A</dt><dd>B</dd></dl>",
        "<figure><figcaption>C</figcaption></figure>",
        "<address>A</address>",
        "<details><summary>S</summary>D</details>",
    ):
        res = ensure_html_linebreaks(tag_sample)
        assert "<!DOCTYPE html>" in res
        assert "<p>" not in res
        assert "<br>" not in res
        assert tag_sample in res


# ---------------------------------- recording-mode replace atomicity (fallback)

class _FakeCursor:
    def __init__(self):
        self.value = "OLD TEXT"

    def getString(self):
        return self.value

    def setString(self, v):  # the tracked delete empties the range
        self.value = v


class _FakeText:
    """Records insertString calls; the first (the new text) FAILS, so a restore must follow."""

    def __init__(self):
        self.cursor = _FakeCursor()
        self.inserts = []
        self._failed_once = False

    def createTextCursorByRange(self, _r):
        return self.cursor

    def insertString(self, _cursor, s, _sel):
        self.inserts.append(s)
        if not self._failed_once:
            self._failed_once = True
            raise RuntimeError("insert boom")  # the new-text insert fails after the delete


class _FakeRange:
    def __init__(self, text):
        self._text = text

    def getText(self):
        return self._text

    def getString(self):
        return self._text.cursor.getString()


def test_replace_preserving_format_restores_original_on_insert_failure():
    # inside an undo context (in_undo_context=True) the replace deletes then inserts. If the
    # insert fails after the delete, the ORIGINAL is restored (best-effort net; the caller's context
    # rollback also cleans up). The failure still propagates.
    import contextlib
    from unittest.mock import patch

    import plugin.writer.html_import as fmt

    text = _FakeText()
    target = _FakeRange(text)
    with patch("plugin.writer.record_changes.is_recording_changes", return_value=True), \
         patch("plugin.writer.review_authors.deletion_author", lambda: contextlib.nullcontext()), \
         pytest.raises(RuntimeError, match="insert boom"):
        fmt.replace_preserving_format(object(), target, "NEW TEXT", in_undo_context=True)

    # The new-text insert was attempted and failed; then the ORIGINAL was re-inserted (restore) so
    # the range is never left a bare partial deletion.
    assert text.inserts == ["NEW TEXT", "OLD TEXT"]


def test_replace_preserving_format_atomic_setstring_when_not_in_undo_context():
    # when the caller has NOT opened an undo context (in_undo_context=False --
    # the default, used by whole-block / streamed / direct callers), there is nothing to roll back a
    # delete-then-insert, so the recording replace uses a SINGLE atomic setString (one UNO action) --
    # never a separate delete + insert that could fail mid-way into a partial deletion. Whether an undo
    # manager merely EXISTS is irrelevant; only an actually-open context makes the two-step safe.
    from unittest.mock import patch

    import plugin.writer.html_import as fmt

    text = _FakeText()
    target = _FakeRange(text)
    with patch("plugin.writer.record_changes.is_recording_changes", return_value=True):
        fmt.replace_preserving_format(object(), target, "NEW TEXT")  # in_undo_context defaults to False

    assert text.cursor.getString() == "NEW TEXT"  # single atomic replace
    assert text.inserts == []                     # no delete-then-insert two-step at all


def test_insert_content_at_position_text_selection_clears_range():
    from unittest.mock import MagicMock, patch

    from plugin.writer.html_import import insert_content_at_position

    text_rng = MagicMock()
    text_rng.getText.return_value.createTextCursorByRange.return_value = MagicMock()
    text_rng.supportsService.return_value = False

    sel = MagicMock()
    sel.getCount.return_value = 1
    sel.getByIndex.return_value = text_rng

    controller = MagicMock()
    controller.getSelection.return_value = sel

    model = MagicMock()
    model.getCurrentController.return_value = controller
    model.getText.return_value.createTextCursor.return_value = MagicMock()

    with patch("plugin.writer.html_import.is_graphic_object", return_value=False), patch(
        "plugin.writer.html_import._insert_mixed_or_plain_html"
    ), patch(
        # Master nested-table check walks MagicMock enumerations forever.
        "plugin.writer.html_import.raise_if_range_hosts_nested_table",
    ):
        insert_content_at_position(model, MagicMock(), "<p>hi</p>", "selection")

    text_rng.setString.assert_called_once_with("")


def test_insert_content_at_position_two_ranges_clears_non_collapsed_range():
    from unittest.mock import MagicMock, patch

    from plugin.writer.html_import import insert_content_at_position

    rng0 = MagicMock()
    rng0.getStart.return_value = MagicMock()
    rng0.getEnd.return_value = MagicMock()
    rng0.getText.return_value.compareRegionStarts.return_value = 0
    rng0.supportsService.return_value = False

    rng1 = MagicMock()
    rng1.getStart.return_value = MagicMock()
    rng1.getEnd.return_value = MagicMock()
    rng1.getText.return_value.compareRegionStarts.return_value = -1
    rng1.getText.return_value.createTextCursorByRange.return_value = MagicMock()
    rng1.supportsService.return_value = False

    sel = MagicMock()
    sel.getCount.return_value = 2
    sel.getByIndex.side_effect = lambda i: rng0 if i == 0 else rng1

    controller = MagicMock()
    controller.getSelection.return_value = sel

    model = MagicMock()
    model.getCurrentController.return_value = controller
    model.getText.return_value.createTextCursor.return_value = MagicMock()

    with patch("plugin.writer.html_import.is_graphic_object", return_value=False), patch(
        "plugin.writer.html_import._insert_mixed_or_plain_html"
    ), patch(
        "plugin.writer.html_import.raise_if_range_hosts_nested_table",
    ):
        insert_content_at_position(model, MagicMock(), "<p>hi</p>", "selection")

    rng0.setString.assert_not_called()
    rng1.setString.assert_called_once_with("")


def test_insert_content_at_position_shape_selection_falls_back_to_doc_end():
    """After shape.upsert, controller selects the Draw shape — HTML insert must not use it."""
    from unittest.mock import MagicMock, patch

    from plugin.writer.html_import import insert_content_at_position

    shape = MagicMock()
    # LibreOffice often returns 1 rather than True
    shape.supportsService.side_effect = lambda s: 1 if s == "com.sun.star.drawing.Shape" else 0

    sel = MagicMock()
    sel.getCount.return_value = 1
    sel.getByIndex.return_value = shape

    controller = MagicMock()
    controller.getSelection.return_value = sel
    # View cursor also unusable / shape-like → force document end
    controller.getViewCursor.side_effect = Exception("no view cursor")

    body_cursor = MagicMock()
    body_text = MagicMock()
    body_text.createTextCursor.return_value = body_cursor

    model = MagicMock()
    model.getCurrentController.return_value = controller
    model.getText.return_value = body_text

    with patch("plugin.writer.html_import.is_graphic_object", return_value=False), patch(
        "plugin.writer.html_import._insert_mixed_or_plain_html"
    ) as mock_insert:
        insert_content_at_position(model, MagicMock(), "<p>hi</p>", "selection")

    body_cursor.gotoEnd.assert_called_once_with(False)
    mock_insert.assert_called_once()
    assert mock_insert.call_args.args[2] is body_cursor
    shape.setString.assert_not_called()


def test_insert_content_at_position_empty_selection_uses_doc_end():
    from unittest.mock import MagicMock, patch

    from plugin.writer.html_import import insert_content_at_position

    sel = MagicMock()
    sel.getCount.return_value = 0

    controller = MagicMock()
    controller.getSelection.return_value = sel
    controller.getViewCursor.side_effect = Exception("no view")

    body_cursor = MagicMock()
    body_text = MagicMock()
    body_text.createTextCursor.return_value = body_cursor
    model = MagicMock()
    model.getCurrentController.return_value = controller
    model.getText.return_value = body_text

    with patch("plugin.writer.html_import.is_graphic_object", return_value=False), patch(
        "plugin.writer.html_import._insert_mixed_or_plain_html"
    ) as mock_insert:
        insert_content_at_position(model, MagicMock(), "<p>hi</p>", "selection")

    body_cursor.gotoEnd.assert_called_once_with(False)
    mock_insert.assert_called_once()






@pytest.mark.parametrize(("position", "goto"), [("beginning", "gotoStart"), ("end", "gotoEnd")])
def test_insert_content_at_position_extremes_apply_styles(position: str, goto: str):
    """Beginning needs no PARAGRAPH_BREAK prep and end runs absorb prep; both pass apply_styles=True."""
    from unittest.mock import MagicMock, patch

    from plugin.writer.html_import import insert_content_at_position

    body_cursor = MagicMock()
    body_text = MagicMock()
    body_text.createTextCursor.return_value = body_cursor
    model = MagicMock()
    model.getText.return_value = body_text

    with patch("plugin.writer.html_import._insert_mixed_or_plain_html") as mock_insert, patch(
        "plugin.writer.html_import._ensure_empty_absorb_for_styled_insert",
        side_effect=lambda text, cursor, pos: cursor,
    ):
        insert_content_at_position(model, MagicMock(), '<p data-lo-style="Heading1">x</p>', position)

    getattr(body_cursor, goto).assert_called_once_with(False)
    mock_insert.assert_called_once()
    assert mock_insert.call_args.kwargs.get("apply_styles") is True


def test_insert_content_at_position_empty_selection_skips_styles():
    from unittest.mock import MagicMock, patch

    from plugin.writer.html_import import insert_content_at_position

    sel = MagicMock()
    sel.getCount.return_value = 0
    controller = MagicMock()
    controller.getSelection.return_value = sel
    controller.getViewCursor.side_effect = Exception("no view")
    body_cursor = MagicMock()
    body_text = MagicMock()
    body_text.createTextCursor.return_value = body_cursor
    model = MagicMock()
    model.getCurrentController.return_value = controller
    model.getText.return_value = body_text

    with patch("plugin.writer.html_import.is_graphic_object", return_value=False), patch(
        "plugin.writer.html_import._insert_mixed_or_plain_html"
    ) as mock_insert:
        insert_content_at_position(model, MagicMock(), "<p data-lo-style=\"Heading1\">x</p>", "selection")

    mock_insert.assert_called_once()
    assert mock_insert.call_args.kwargs.get("apply_styles") is False


def test_insert_content_at_position_whole_paragraph_selection_applies_styles():
    from unittest.mock import MagicMock, patch

    from plugin.writer.html_import import insert_content_at_position
    from tests.writer.test_html_import import make_paragraph_edge_range

    rng = make_paragraph_edge_range(True, True)

    controller = MagicMock()
    model = MagicMock()
    model.getCurrentController.return_value = controller

    from plugin.writer.html_import_styles import WriteReport

    with patch("plugin.writer.html_import.selected_text_range", return_value=rng), \
         patch("plugin.writer.html_import._selection_is_draw_shape", return_value=False), \
         patch("plugin.writer.html_import.raise_if_range_hosts_nested_table"), \
         patch("plugin.writer.record_changes.is_recording_changes", return_value=False), \
         patch("plugin.writer.html_import._insert_mixed_or_plain_html") as mock_insert:
        mock_insert.return_value = WriteReport()
        report = insert_content_at_position(
            model, MagicMock(), '<p data-lo-style="Heading1">x</p>', "selection"
        )

    rng.setString.assert_called_once_with("")
    mock_insert.assert_called_once()
    assert mock_insert.call_args.kwargs.get("apply_styles") is True
    assert report.style_skips == []


def test_ensure_empty_absorb_inserts_break_when_end_para_has_text():
    from unittest.mock import MagicMock

    from plugin.writer import html_import as hi

    cursor = MagicMock()
    text = MagicMock()
    # Simulate non-empty absorb paragraph
    para = MagicMock()
    para.getString.return_value = "Existing"
    text_obj = MagicMock()
    text_obj.createTextCursorByRange.return_value = para
    cursor.getText.return_value = text_obj
    cursor.getStart.return_value = MagicMock()

    out = hi._ensure_empty_absorb_for_styled_insert(text, cursor, "end")
    assert out is cursor
    text.insertControlCharacter.assert_called_once_with(cursor, 0, False)


def test_ensure_empty_absorb_inserts_break_for_image_only_paragraph():
    from unittest.mock import MagicMock

    from plugin.writer import html_import as hi

    cursor = MagicMock()
    text = MagicMock()
    para = MagicMock()
    para.getString.return_value = ""

    portion = MagicMock()
    portion.getString.return_value = ""
    portion.getPropertyValue.return_value = "TextContent"
    para.createEnumeration.return_value = _FakeReplaceEnum([portion])
    text_obj = MagicMock()
    text_obj.createTextCursorByRange.return_value = para
    cursor.getText.return_value = text_obj
    cursor.getStart.return_value = MagicMock()

    out = hi._ensure_empty_absorb_for_styled_insert(text, cursor, "end")
    assert out is cursor
    text.insertControlCharacter.assert_called_once_with(cursor, 0, False)


def test_ensure_empty_absorb_skips_break_when_end_para_empty():
    from unittest.mock import MagicMock

    from plugin.writer import html_import as hi

    cursor = MagicMock()
    text = MagicMock()
    para = MagicMock()
    para.getString.return_value = "   "
    text_obj = MagicMock()
    text_obj.createTextCursorByRange.return_value = para
    cursor.getText.return_value = text_obj
    cursor.getStart.return_value = MagicMock()

    hi._ensure_empty_absorb_for_styled_insert(text, cursor, "end")
    text.insertControlCharacter.assert_not_called()


def test_ensure_empty_absorb_noop_for_beginning():
    from unittest.mock import MagicMock

    from plugin.writer import html_import as hi

    cursor = MagicMock()
    text = MagicMock()
    hi._ensure_empty_absorb_for_styled_insert(text, cursor, "beginning")
    text.insertControlCharacter.assert_not_called()


def test_insert_content_at_position_nested_table_is_not_appended_at_end():
    from unittest.mock import MagicMock, patch

    from plugin.framework.errors import ToolExecutionError
    from plugin.writer.html_import import insert_content_at_position

    text_rng = MagicMock()
    text_rng.getText.return_value.createTextCursorByRange.return_value = MagicMock()
    text_rng.supportsService.return_value = False
    sel = MagicMock()
    sel.getCount.return_value = 1
    sel.getByIndex.return_value = text_rng
    controller = MagicMock()
    controller.getSelection.return_value = sel
    body_cursor = MagicMock()
    model = MagicMock()
    model.getCurrentController.return_value = controller
    model.getText.return_value.createTextCursor.return_value = body_cursor

    with patch("plugin.writer.html_import.is_graphic_object", return_value=False), patch(
        "plugin.writer.html_import._insert_mixed_or_plain_html"
    ) as mock_insert, patch(
        "plugin.writer.html_import.raise_if_range_hosts_nested_table",
        side_effect=ToolExecutionError("nested tables"),
    ):
        with pytest.raises(ToolExecutionError, match="nested tables"):
            insert_content_at_position(model, MagicMock(), "<p>hi</p>", "selection")
    mock_insert.assert_not_called()
    body_cursor.gotoEnd.assert_not_called()


def test_insert_content_at_position_clear_failure_is_not_moved_to_end():
    from unittest.mock import MagicMock, patch

    from plugin.writer.html_import import insert_content_at_position

    text_rng = MagicMock()
    text_rng.getText.return_value.createTextCursorByRange.return_value = MagicMock()
    text_rng.supportsService.return_value = False
    text_rng.setString.side_effect = RuntimeError("clear failed")
    sel = MagicMock()
    sel.getCount.return_value = 1
    sel.getByIndex.return_value = text_rng
    controller = MagicMock()
    controller.getSelection.return_value = sel
    body_cursor = MagicMock()
    model = MagicMock()
    model.getCurrentController.return_value = controller
    model.getText.return_value.createTextCursor.return_value = body_cursor

    with patch("plugin.writer.html_import.is_graphic_object", return_value=False), patch(
        "plugin.writer.html_import._insert_mixed_or_plain_html"
    ) as mock_insert, patch(
        "plugin.writer.html_import.raise_if_range_hosts_nested_table",
    ):
        with pytest.raises(RuntimeError, match="clear failed"):
            insert_content_at_position(model, MagicMock(), "<p>hi</p>", "selection")
    mock_insert.assert_not_called()
    body_cursor.gotoEnd.assert_not_called()


def test_insert_content_at_position_attribute_error_still_uses_doc_end():
    from unittest.mock import MagicMock, patch

    from plugin.writer.html_import import insert_content_at_position

    text_rng = MagicMock()
    text_rng.getText.side_effect = AttributeError("insertDocumentFromURL")
    text_rng.supportsService.return_value = False
    sel = MagicMock()
    sel.getCount.return_value = 1
    sel.getByIndex.return_value = text_rng
    controller = MagicMock()
    controller.getSelection.return_value = sel
    body_cursor = MagicMock()
    body_text = MagicMock()
    body_text.createTextCursor.return_value = body_cursor
    model = MagicMock()
    model.getCurrentController.return_value = controller
    model.getText.return_value = body_text

    with patch("plugin.writer.html_import.is_graphic_object", return_value=False), patch(
        "plugin.writer.html_import._insert_mixed_or_plain_html"
    ) as mock_insert:
        insert_content_at_position(model, MagicMock(), "<p>hi</p>", "selection")
    body_cursor.gotoEnd.assert_called_once_with(False)
    mock_insert.assert_called_once()
    assert mock_insert.call_args.args[2] is body_cursor


def test_ensure_empty_absorb_raises_when_break_fails():
    from unittest.mock import MagicMock

    from plugin.writer import html_import as hi

    cursor = MagicMock()
    text = MagicMock()
    para = MagicMock()
    para.getString.return_value = "Existing"
    text_obj = MagicMock()
    text_obj.createTextCursorByRange.return_value = para
    cursor.getText.return_value = text_obj
    cursor.getStart.return_value = MagicMock()
    text.insertControlCharacter.side_effect = RuntimeError("break failed")
    with pytest.raises(RuntimeError, match="break failed"):
        hi._ensure_empty_absorb_for_styled_insert(text, cursor, "end")


class _FakeReplaceEnum:
    def __init__(self, items):
        self._items = list(items)

    def hasMoreElements(self):
        return bool(self._items)

    def nextElement(self):
        return self._items.pop(0)


class _FakeReplacePortion:
    def __init__(self, text="", portion_type="Text"):
        self.TextPortionType = portion_type
        self._text = text

    def getString(self):
        return self._text


class _FakeReplacePara:
    def __init__(self, portions):
        self._portions = portions

    def createEnumeration(self):
        return _FakeReplaceEnum(self._portions)


class _FakeReplaceCursor:
    def __init__(self, go_right_fn=None):
        self.moves = []
        self.writes = []
        self._go_right_fn = go_right_fn

    def goRight(self, n, expand):
        self.moves.append((n, expand))
        if self._go_right_fn is not None:
            return self._go_right_fn(n, expand)
        return True

    def setString(self, value):
        self.writes.append(value)

    def getEnd(self):
        return object()

    def gotoRange(self, _end, _expand):
        return None

    def getPropertyValue(self, _name):
        return None


class _FakeReplaceText:
    def __init__(self, cursor=None):
        self.cursor = cursor or _FakeReplaceCursor()
        self.inserts = []

    def createTextCursorByRange(self, _r):
        return self.cursor

    def insertString(self, *_args):
        self.inserts.append("insert")


class _FakeReplaceRange:
    def __init__(self, text, string, paras=None):
        self._text = text
        self._string = string
        self._paras = paras

    def getText(self):
        return self._text

    def getString(self):
        return self._string

    def getStart(self):
        return object()

    def createEnumeration(self):
        return _FakeReplaceEnum(self._paras or [])


@pytest.mark.parametrize(
    "old_str, new_str, go_right_fn",
    [
        pytest.param("AB", "XY", lambda _n, _exp: False, id="replace"),
        pytest.param("AAAAAX", "AAAAAY", lambda _n, _exp: False, id="equal_run"),
        pytest.param("ABCD", "AB", lambda _n, exp: not exp, id="shrink"),
    ],
)
def test_replace_preserving_format_raises_when_go_right_stops(old_str, new_str, go_right_fn):
    from unittest.mock import MagicMock, patch
    from plugin.framework.errors import ToolExecutionError
    from plugin.writer import html_import as hi, record_changes

    cursor = _FakeReplaceCursor(go_right_fn=go_right_fn)
    text = _FakeReplaceText(cursor)
    ctx = MagicMock()
    with patch.object(record_changes, "is_recording_changes", return_value=False):
        with pytest.raises(ToolExecutionError, match="did not advance"):
            hi.replace_preserving_format(MagicMock(), _FakeReplaceRange(text, old_str), new_str, ctx=ctx)
    assert cursor.writes == []
    assert text.inserts == []
    ctx.getServiceManager.assert_not_called()


def test_replace_preserving_format_skips_equal_runs():
    from unittest.mock import MagicMock, patch
    from plugin.writer import html_import as hi, record_changes

    text = _FakeReplaceText()
    with patch.object(record_changes, "is_recording_changes", return_value=False):
        hi.replace_preserving_format(MagicMock(), _FakeReplaceRange(text, "AAAAAX"), "AAAAAY")

    assert text.cursor.moves == [(5, False), (1, True)]
    assert text.cursor.writes == ["Y"]


def test_replace_preserving_format_special_portion_keeps_one_step_loop():
    from unittest.mock import MagicMock, patch
    from plugin.writer import html_import as hi, record_changes

    text = _FakeReplaceText()
    para = _FakeReplacePara([_FakeReplacePortion(portion_type="TextField")])
    with patch.object(record_changes, "is_recording_changes", return_value=False):
        hi.replace_preserving_format(MagicMock(), _FakeReplaceRange(text, "AAAAAX", paras=[para]), "AAAAAY")

    # Special portion: each character is checked 1-by-1
    assert text.cursor.moves == [(1, True)] * 6
    assert text.cursor.writes == ["Y"]


def test_replace_preserving_format_200_char_plain_issues_one_setstring():
    from unittest.mock import MagicMock, patch
    from plugin.writer import html_import as hi, record_changes

    old_str = "A" * 200
    new_str = "B" * 200
    text = _FakeReplaceText()
    para = _FakeReplacePara([_FakeReplacePortion(old_str)])

    with patch.object(record_changes, "is_recording_changes", return_value=False):
        hi.replace_preserving_format(MagicMock(), _FakeReplaceRange(text, old_str, paras=[para]), new_str)

    assert text.cursor.writes == [new_str]
    assert len(text.cursor.writes) == 1


def test_replace_preserving_format_two_portions_issue_two_setstrings():
    from unittest.mock import MagicMock, patch
    from plugin.writer import html_import as hi, record_changes

    p1 = _FakeReplacePortion("plain text ")
    p2 = _FakeReplacePortion("bold text")
    text = _FakeReplaceText()
    para = _FakeReplacePara([p1, p2])

    with patch.object(record_changes, "is_recording_changes", return_value=False):
        hi.replace_preserving_format(
            MagicMock(),
            _FakeReplaceRange(text, "plain text bold text", paras=[para]),
            "plain word BOLD TEXT",
        )

    assert text.cursor.writes == ["word", "BOLD TEXT"]


def test_replace_preserving_format_equal_string_is_noop():
    from unittest.mock import MagicMock, patch
    from plugin.writer import html_import as hi, record_changes

    text = _FakeReplaceText()
    with patch.object(record_changes, "is_recording_changes", return_value=False):
        hi.replace_preserving_format(MagicMock(), _FakeReplaceRange(text, "IDENTICAL"), "IDENTICAL")

    assert text.cursor.moves == []
    assert text.cursor.writes == []


def test_replace_preserving_format_atomic_when_split_author_false_even_in_undo_context():
    # Configurable coloring: split_author=False forces the SINGLE atomic setString (one author -> one
    # color) even INSIDE an open undo context, where split_author=True (the default) would use the
    # two-step delete+insert for split-author coloring. The two-step is gated on BOTH flags, so turning
    # coloring off collapses every recorded replace — surgical or whole-block — to one color, still
    # all-or-nothing.
    from unittest.mock import patch

    import plugin.writer.html_import as fmt

    text = _FakeText()
    target = _FakeRange(text)
    with patch("plugin.writer.record_changes.is_recording_changes", return_value=True):
        fmt.replace_preserving_format(object(), target, "NEW TEXT",
                                      in_undo_context=True, split_author=False)

    assert text.cursor.getString() == "NEW TEXT"  # single atomic replace, not the two-step
    assert text.inserts == []                     # no delete-then-insert two-step at all


def test_replace_preserving_format_two_step_when_split_author_true_in_undo_context():
    # Complement: with split_author=True (default) AND an open undo context, the replace uses the
    # two-step (delete authored distinctly via deletion_author, then insert) so by-author coloring
    # renders two colors. A clean (non-failing) insert leaves exactly the new text.
    import contextlib
    from unittest.mock import patch

    import plugin.writer.html_import as fmt

    class _OkText(_FakeText):
        def insertString(self, _cursor, s, _sel):  # never fails -> no restore
            self.inserts.append(s)

    text = _OkText()
    target = _FakeRange(text)
    with patch("plugin.writer.record_changes.is_recording_changes", return_value=True), \
         patch("plugin.writer.review_authors.deletion_author", lambda: contextlib.nullcontext()):
        fmt.replace_preserving_format(object(), target, "NEW TEXT", in_undo_context=True)

    assert text.cursor.getString() == ""   # the deletion emptied the range (step 1)
    assert text.inserts == ["NEW TEXT"]    # then the new text was inserted (step 2)


def test_run_writer_mutation_with_optional_review_import_error():
    """LibrePy omits edit_review; helper must apply the mutation directly."""
    from unittest.mock import MagicMock, patch

    from plugin.writer.html_import import run_writer_mutation_with_optional_review

    apply_fn = MagicMock()
    real_import = __import__

    def import_without_edit_review(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "plugin.writer.edit_review":
            raise ImportError("No module named 'plugin.writer.edit_review'")
        return real_import(name, globals, locals, fromlist, level)

    with patch("builtins.__import__", side_effect=import_without_edit_review):
        run_writer_mutation_with_optional_review(MagicMock(), MagicMock(), apply_fn)
    apply_fn.assert_called_once()


@contextmanager
def _capture_temp_buffer(content_holder):
    @contextmanager
    def fake_with_temp_buffer(content=None, ext=None):
        content_holder["content"] = content
        yield ("/tmp/fake.html", "file:///tmp/fake.html")

    with patch("plugin.writer.html_import.with_temp_buffer", side_effect=fake_with_temp_buffer):
        yield


def test_wrap_html_fragment_skips_full_document():
    full = "<html><head></head><body><p>Hi</p></body></html>"
    assert _wrap_html_fragment(full, extra_css="ignored") == full


def test_wraps_bare_fragment():
    cursor = MagicMock()
    holder = {}
    with _capture_temp_buffer(holder):
        insert_html_fragment_at_cursor(cursor, "<p>Hi</p>")
    assert "<!DOCTYPE html>" in holder["content"]
    assert "<p>Hi</p>" in holder["content"]
    cursor.insertDocumentFromURL.assert_called_once()


def test_extra_css_in_head():
    cursor = MagicMock()
    holder = {}
    css = "ul, ol { margin-left: 0.2cm; padding-left: 0.3cm; }"
    with _capture_temp_buffer(holder):
        insert_html_fragment_at_cursor(cursor, "<ul><li>x</li></ul>", extra_css=css)
    assert css in holder["content"]
    assert "<style>" in holder["content"]


def test_prewrapped_skips_rewrap():
    cursor = MagicMock()
    holder = {}
    prewrapped = "<html><body><b>Bold</b></body></html>"
    with _capture_temp_buffer(holder):
        insert_html_fragment_at_cursor(cursor, prewrapped, wrap=False)
    assert holder["content"] == prewrapped


def test_advances_cursor_when_advance_true():
    """The cursor continues right after the imported content, in its own text. Jumping to the
    BODY end sent later segments of a mid-document edit to the end (#46/#47) and raised
    RuntimeException for a cursor in a frame or cell (#52)."""
    cursor = MagicMock()
    parked = cursor.getText.return_value.createTextCursorByRange.return_value

    with _capture_temp_buffer({}):
        insert_html_fragment_at_cursor(cursor, "<p>Hi</p>", advance=True)

    cursor.getText.return_value.createTextCursorByRange.assert_called_once_with(cursor.getEnd())
    cursor.gotoRange.assert_called_once_with(parked.getStart(), False)


def test_insert_html_fragment_at_cursor_advance_false_leaves_cursor():
    """Without advance=True, no parked cursor is created and cursor.gotoRange is not called."""
    cursor = MagicMock()

    with _capture_temp_buffer({}):
        insert_html_fragment_at_cursor(cursor, "<p>Hi</p>", advance=False)

    cursor.getText.return_value.createTextCursorByRange.assert_not_called()
    cursor.gotoRange.assert_not_called()


def test_filter_name_starwriter():
    cursor = MagicMock()
    filter_holder = {}

    @contextmanager
    def fake_with_temp_buffer(content=None, ext=None):
        yield ("/tmp/fake.html", "file:///tmp/fake.html")

    def capture_insert(url, props):
        filter_holder["props"] = props

    cursor.insertDocumentFromURL.side_effect = capture_insert

    with patch("plugin.writer.html_import.with_temp_buffer", side_effect=fake_with_temp_buffer):
        insert_html_fragment_at_cursor(cursor, "<p>Hi</p>")

    assert filter_holder["props"][0].Name == "FilterName"
    assert filter_holder["props"][0].Value == HTML_FILTER


def test_replace_full_document_and_insert_content_call_restore_with_text_obj():
    from plugin.writer.html_import import insert_content_at_position, replace_full_document

    doc = MagicMock()
    body_text = MagicMock()
    doc.getText.return_value = body_text
    cur = MagicMock()
    body_text.createTextCursor.return_value = cur
    cur.getText.return_value = body_text

    with (
        patch("plugin.writer.html_import.insert_html_fragment_at_cursor"),
        patch("plugin.writer.field_restore.restore_field_placeholders") as mock_restore,
    ):
        replace_full_document(doc, MagicMock(), "<p>Page <span title='page-number'/></p>")
        mock_restore.assert_called_once()
        assert mock_restore.call_args[0][0] is doc
        assert mock_restore.call_args[0][1] is body_text
        assert mock_restore.call_args[1]["nonce"] is not None

    with (
        patch("plugin.writer.html_import.insert_html_fragment_at_cursor"),
        patch("plugin.writer.field_restore.restore_field_placeholders") as mock_restore,
    ):
        insert_content_at_position(doc, MagicMock(), "<p>Page <span title='page-number'/></p>", "beginning")
        mock_restore.assert_called_once()
        assert mock_restore.call_args[0][0] is doc
        assert mock_restore.call_args[0][1] is body_text
        assert mock_restore.call_args[1]["nonce"] is not None
