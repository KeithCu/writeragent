# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Unit tests for plugin.writer format helpers (html_export/html_import, no UNO)."""

import base64

import pytest

from plugin.writer.html_export import (
    _apply_image_export_options,
    strip_embedded_image_data,
)


def test_strip_embedded_image_data_removes_base64_keeps_external_url():
    b64 = base64.b64encode(b"png-bytes").decode("ascii")
    html = (
        f'<p><img src="data:image/png;base64,{b64}" alt="chart"/>'
        f'<img src="image001.png" alt="linked"/></p>'
    )
    out = strip_embedded_image_data(html)
    assert "data:image" not in out
    assert b64 not in out
    assert 'src="image001.png"' in out
    assert 'alt="chart"' in out


def test_strip_embedded_image_data_css_background_url():
    b64 = base64.b64encode(b"x").decode("ascii")
    html = f'<p style="background-image: url(data:image/png;base64,{b64})">x</p>'
    out = strip_embedded_image_data(html)
    assert "data:image" not in out
    assert b64 not in out
    assert "background-image: url()" in out


def test_apply_image_export_options_skips_when_include_images_true():
    b64 = base64.b64encode(b"x").decode("ascii")
    html = f'<img src="data:image/png;base64,{b64}"/>'
    assert _apply_image_export_options(html, include_images=True) == html


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

    para.createEnumeration.return_value = _Enum([portion])
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
    "old_str, new_str, go_right_fn, check_ctx",
    [
        pytest.param("AB", "XY", lambda _n, _exp: False, True, id="replace"),
        pytest.param("AAAAAX", "AAAAAY", lambda _n, _exp: False, False, id="equal_run"),
        pytest.param("ABCD", "AB", lambda _n, exp: not exp, False, id="shrink"),
    ],
)
def test_replace_preserving_format_raises_when_go_right_stops(old_str, new_str, go_right_fn, check_ctx):
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
    if check_ctx:
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


# --- clear_direct: the paragraph half of Format > Clear Direct Formatting -----------------


class _MultiStatesCursor:
    """Range that supports XMultiPropertyStates (the normal LibreOffice text cursor)."""

    def __init__(self):
        self.cleared = None

    def setPropertiesToDefault(self, names):
        self.cleared = tuple(names)


class _SingleStateCursor:
    """Range without the multi variant — exercises the one-at-a-time fallback."""

    def __init__(self, unsupported=()):
        self.cleared = []
        self._unsupported = set(unsupported)

    def setPropertyToDefault(self, name):
        if name in self._unsupported:
            raise RuntimeError("unsupported property: %s" % name)
        self.cleared.append(name)


def test_clear_direct_paragraph_properties_uses_multi_variant():
    from plugin.writer import styles as st

    cursor = _MultiStatesCursor()
    cleared = st.reset_properties_to_default(cursor, st.CLEARABLE_PARA_PROPERTIES)

    assert cursor.cleared == st.CLEARABLE_PARA_PROPERTIES
    assert cleared == list(st.CLEARABLE_PARA_PROPERTIES)


def test_clear_direct_paragraph_properties_falls_back_one_at_a_time():
    from plugin.writer import styles as st

    cursor = _SingleStateCursor(unsupported={"ParaSplit"})
    cleared = st.reset_properties_to_default(cursor, st.CLEARABLE_PARA_PROPERTIES)

    assert "ParaLeftMargin" in cleared and "ParaFirstLineIndent" in cleared
    assert "ParaSplit" not in cleared  # reported honestly as not cleared


def test_summarize_char_overrides_is_json_safe_and_first_portion_wins():
    from plugin.writer import styles as st

    posture = type("FontSlant", (), {"__module__": "uno", "value": "ITALIC"})()

    overrides = [
        (object(), {"CharFontName": "Times New Roman", "CharHeight": 12.0, "CharLocale": object(), "CharPosture": posture}),
        (object(), {"CharFontName": "Courier"}),
    ]
    summary = st._summarize_char_overrides(overrides)

    assert summary == {"CharFontName": "Times New Roman", "CharHeight": 12.0, "CharPosture": 2}
    assert "CharLocale" not in summary  # UNO struct -> not serialisable, left out


def test_summarize_char_overrides_reports_asian_complex():
    """style_props clears Asian/Complex font slots; the report must say so (honest, not silent)."""
    from plugin.writer import styles as st

    overrides = [(object(), {
        "CharFontName": "Times New Roman",
        "CharFontNameAsian": "SimSun",
        "CharFontNameComplex": "Tahoma",
        "CharHeight": 12.0,
        "CharHeightAsian": 10.5,
        "CharHeightComplex": 11.0,
    })]
    summary = st._summarize_char_overrides(overrides)

    assert summary["CharFontNameAsian"] == "SimSun"
    assert summary["CharFontNameComplex"] == "Tahoma"
    assert summary["CharHeightAsian"] == 10.5
    assert summary["CharHeightComplex"] == 11.0


def test_enum_hit_walk_cap_and_warning_are_honest():
    from unittest.mock import MagicMock
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


def test_reset_properties_to_default_is_a_noop_on_empty_names():
    from plugin.writer import styles as st

    cursor = _MultiStatesCursor()
    assert st.reset_properties_to_default(cursor, ()) == []
    assert cursor.cleared is None  # never call UNO with an empty set


def test_reset_properties_to_default_takes_the_caller_s_names():
    """The clear is scoped by the caller — style_props clears the font, all clears what was found."""
    from plugin.writer import styles as st

    cursor = _MultiStatesCursor()
    st.reset_properties_to_default(cursor, st.STYLE_GOVERNED_CHAR_PROPERTIES)

    assert cursor.cleared == st.STYLE_GOVERNED_CHAR_PROPERTIES


# --- range copy: carrying direct formatting into the temp document -------------------------


def _enum(items):
    from unittest.mock import MagicMock

    e = MagicMock()
    rest = list(items)
    e.hasMoreElements.side_effect = lambda: True if rest else False
    e.nextElement.side_effect = lambda: rest.pop(0)
    return e


def _text_portion(text, ptype="Text", **props):
    from unittest.mock import MagicMock

    p = MagicMock()
    values = {"TextPortionType": ptype}
    values.update(props)
    p.getPropertyValue.side_effect = lambda n: values[n]
    p.getString.return_value = text
    return p


def _para_of(portions):
    from unittest.mock import MagicMock

    para = MagicMock()
    para.createEnumeration.side_effect = lambda: _enum(portions)
    return para


def test_visible_portions_skips_tracked_deletions():
    """Shared walk with get_string_without_tracked_deletions — a divergence would paint one
    run's formatting onto another run's characters."""
    from plugin.writer import html_export as hx

    para = _para_of([
        _text_portion("kept "),
        _text_portion("", "Redline", RedlineType="Delete"),
        _text_portion("deleted"),
        _text_portion("", "Redline", RedlineType="Delete"),
        _text_portion(" tail"),
    ])

    assert "".join(chunk for _p, chunk in hx._visible_portions(para)) == "kept  tail"


def test_visible_portions_ignores_non_delete_redlines():
    from plugin.writer import html_export as hx

    para = _para_of([
        _text_portion("", "Redline", RedlineType="Insert"),
        _text_portion("inserted text"),
    ])

    assert "".join(chunk for _p, chunk in hx._visible_portions(para)) == "inserted text"


def test_visible_portions_warns_when_walk_hits_cap():
    """Silent truncate hid later runs' Char*; the agent needs a warning that the paint is partial."""
    from plugin.writer import html_export as hx

    para = _para_of([_text_portion("a"), _text_portion("b"), _text_portion("c")])
    truncated = []
    chunks = [chunk for _p, chunk in hx._visible_portions(para, limit=2, truncated_out=truncated)]

    assert chunks == ["a", "b"]
    assert len(truncated) == 1


def test_visible_portions_does_not_warn_when_enum_is_exhausted():
    from plugin.writer import html_export as hx

    para = _para_of([_text_portion("a"), _text_portion("b")])
    truncated = []
    chunks = [chunk for _p, chunk in hx._visible_portions(para, limit=2, truncated_out=truncated)]

    assert chunks == ["a", "b"]
    assert truncated == []


def test_copy_properties_skips_values_the_style_already_gives():
    """Copying an inherited value would make it a hand-set one on the copy, and the read would
    then report margin-right:0cm on every paragraph as if the user had set it."""
    from unittest.mock import MagicMock
    from plugin.writer import range_copy as rc

    src, dst, style = MagicMock(), MagicMock(), MagicMock()
    src.getPropertyValue.side_effect = {"ParaLeftMargin": 3251, "ParaRightMargin": 0}.get
    style.getPropertyValue.side_effect = {"ParaLeftMargin": 0, "ParaRightMargin": 0}.get

    rc._copy_properties(src, dst, ("ParaLeftMargin", "ParaRightMargin"), style)

    dst.setPropertyValue.assert_called_once_with("ParaLeftMargin", 3251)


def test_copy_properties_without_a_style_copies_everything_readable():
    from unittest.mock import MagicMock
    from plugin.writer import range_copy as rc

    src, dst = MagicMock(), MagicMock()
    src.getPropertyValue.side_effect = {"CharWeight": 150.0}.get

    rc._copy_properties(src, dst, ("CharWeight",))

    dst.setPropertyValue.assert_called_once_with("CharWeight", 150.0)


def test_copy_properties_continues_past_one_the_target_refuses():
    """The temp document is a plain Writer doc and need not offer every source property."""
    from unittest.mock import MagicMock
    from plugin.writer import range_copy as rc

    src, dst = MagicMock(), MagicMock()
    src.getPropertyValue.side_effect = {"CharPosture": 1, "CharWeight": 150.0}.get

    def refuse_posture(name, _value):
        if name == "CharPosture":
            raise RuntimeError("unsupported")

    dst.setPropertyValue.side_effect = refuse_posture

    rc._copy_properties(src, dst, ("CharPosture", "CharWeight"))

    assert [c.args[0] for c in dst.setPropertyValue.call_args_list] == ["CharPosture", "CharWeight"]


def test_paint_direct_formatting_still_paints_char_when_para_copy_raises():
    """A refused Para* must not skip the portion Char* loop — that paint is why the
    temp-doc path exists (bold/colour on a range read)."""
    from unittest.mock import MagicMock
    from plugin.writer import range_copy as rc

    para = MagicMock()
    portion = MagicMock()
    portion.getPropertyValue.side_effect = lambda name: 150.0 if name == "CharWeight" else None
    para_start = MagicMock(name="para_start")
    para_cursor = MagicMock()
    para_cursor.getStart.return_value = para_start
    para_cursor.gotoEndOfParagraph.side_effect = RuntimeError("refused Para*")
    run = MagicMock()
    temp_text = MagicMock()
    temp_text.createTextCursor.return_value = para_cursor
    temp_text.createTextCursorByRange.return_value = run

    rc.paint_direct_formatting(para, [(portion, "bold")], temp_text, 0, 4)

    run.setPropertyValue.assert_any_call("CharWeight", 150.0)


def test_source_style_is_cached_and_tolerates_a_missing_style():
    from unittest.mock import MagicMock
    from plugin.writer import range_copy as rc

    model = MagicMock()
    families = model.getStyleFamilies.return_value.getByName.return_value
    families.getByName.side_effect = lambda n: "STYLE" if n == "Standard" else (_ for _ in ()).throw(KeyError(n))

    cache = {}
    assert rc.source_style(model, "Standard", cache) == "STYLE"
    assert rc.source_style(model, "Standard", cache) == "STYLE"
    assert families.getByName.call_count == 1  # second read served from the cache
    assert rc.source_style(model, "Nope", cache) is None
    assert rc.source_style(model, "", cache) is None


def _make_style_doc(style_default=100):
    from unittest.mock import MagicMock

    doc = MagicMock()
    para_styles = MagicMock()
    doc.getStyleFamilies.return_value.getByName.return_value = para_styles
    style = MagicMock()
    para_styles.getByName.return_value = style
    style.getPropertyValue.return_value = style_default
    return doc, para_styles, style


def _make_prop_info(*names):
    from unittest.mock import MagicMock

    props = []
    for name in names:
        prop = MagicMock()
        prop.Name = name
        prop.Attributes = 0
        props.append(prop)
    info = MagicMock()
    info.getProperties.return_value = props
    return info


def _make_para(portions, style_name="Standard"):
    from unittest.mock import MagicMock

    para = MagicMock()
    para.supportsService.side_effect = lambda s: s == "com.sun.star.text.Paragraph"
    para.getPropertyValue.side_effect = lambda name: style_name if name == "ParaStyleName" else None
    para_enum = MagicMock()
    para_enum.hasMoreElements.side_effect = [True] * len(portions) + [False]
    para_enum.nextElement.side_effect = list(portions)
    para.createEnumeration.return_value = para_enum
    return para


def _make_style_cursor(paras):
    from unittest.mock import MagicMock

    cursor = MagicMock()
    doc_enum = MagicMock()
    doc_enum.hasMoreElements.side_effect = [True] * len(paras) + [False]
    doc_enum.nextElement.side_effect = list(paras)
    cursor.getText.return_value.createTextCursorByRange.return_value = cursor
    cursor.createEnumeration.return_value = doc_enum
    return cursor


def test_apply_paragraph_style_preserving_direct_char_caches_style_and_props():
    from unittest.mock import MagicMock
    from plugin.writer import styles as fmt

    doc, para_styles, style = _make_style_doc(style_default="DefaultVal")
    prop_set_info = _make_prop_info("CharWeight")

    portion1 = MagicMock()
    portion1.getPropertyValue.side_effect = lambda name: "Text" if name == "TextPortionType" else "CustomVal"
    portion1.getPropertySetInfo.return_value = prop_set_info

    portion2 = MagicMock()
    portion2.getPropertyValue.side_effect = lambda name: "Text" if name == "TextPortionType" else "CustomVal2"
    portion2.getPropertySetInfo.return_value = MagicMock()

    para1 = _make_para([portion1, portion2])

    portion3 = MagicMock()
    portion3.getPropertyValue.side_effect = lambda name: "Text" if name == "TextPortionType" else "CustomVal3"
    para2 = _make_para([portion3])

    style_cache = {}
    fmt.apply_paragraph_style_preserving_direct_char(doc, _make_style_cursor([para1]), "Heading 1", style_cache=style_cache)

    portion1.getPropertySetInfo.assert_called_once()
    portion2.getPropertySetInfo.assert_not_called()
    assert style.getPropertyValue.call_count == 1  # once for CharWeight
    assert para_styles.getByName.call_count == 1

    fmt.apply_paragraph_style_preserving_direct_char(doc, _make_style_cursor([para2]), "Heading 1", style_cache=style_cache)

    # para_styles.getByName should NOT be called again for "Standard"
    assert para_styles.getByName.call_count == 1
    portion3.getPropertySetInfo.assert_not_called()


def test_apply_paragraph_style_capture_failure_and_disposal():
    from unittest.mock import MagicMock
    import pytest
    from plugin.framework.errors import DocumentDisposedError
    from plugin.writer import styles as fmt

    doc, _para_styles, _style = _make_style_doc()
    info = _make_prop_info("CharWeight", "CharHeight")

    def get_prop_side_effect(name):
        if name == "TextPortionType":
            return "Text"
        if name == "CharWeight":
            return 150
        if name == "CharHeight":
            raise RuntimeError("Property unavailable")
        return None

    portion = MagicMock()
    portion.getPropertyValue.side_effect = get_prop_side_effect
    portion.getPropertySetInfo.return_value = info

    rep = fmt.apply_paragraph_style_preserving_direct_char(
        doc, _make_style_cursor([_make_para([portion])]), "Heading 1", clear_direct="none"
    )
    assert rep.get("capture_incomplete") is True
    assert "warning" in rep
    assert rep.get("preserved_char_overrides", {}).get("CharWeight") == 150

    # Disposal exception propagates as DocumentDisposedError
    DisposedExc = type("DisposedException", (Exception,), {})
    portion_disp = MagicMock()
    portion_disp.getPropertyValue.side_effect = lambda name: "Text" if name == "TextPortionType" else (_ for _ in ()).throw(DisposedExc("object disposed"))
    portion_disp.getPropertySetInfo.return_value = info

    with pytest.raises(DocumentDisposedError):
        fmt.apply_paragraph_style_preserving_direct_char(doc, _make_style_cursor([_make_para([portion_disp])]), "Heading 1")


def test_apply_paragraph_style_restore_failure_is_not_capture_incomplete():
    from unittest.mock import MagicMock
    from plugin.writer import styles as fmt

    doc, _para_styles, _style = _make_style_doc()
    info = _make_prop_info("CharWeight")

    portion = MagicMock()
    portion.getPropertyValue.side_effect = lambda name: "Text" if name == "TextPortionType" else (150 if name == "CharWeight" else None)
    portion.getPropertySetInfo.return_value = info

    restore_cursor = MagicMock()
    restore_cursor.setPropertyValue.side_effect = RuntimeError("read-only")
    portion.getText.return_value.createTextCursorByRange.return_value = restore_cursor

    rep = fmt.apply_paragraph_style_preserving_direct_char(
        doc, _make_style_cursor([_make_para([portion])]), "Heading 1", clear_direct="none"
    )
    assert rep.get("capture_incomplete") is not True
    assert "could not be re-applied" in rep["warning"]
    assert "could not be read" not in rep["warning"]
    assert rep["preserved_char_overrides"]["CharWeight"] == 150


def test_apply_paragraph_style_negative_caches_failed_property_set_info():
    from unittest.mock import MagicMock
    from plugin.writer import styles as fmt

    doc, _para_styles, _style = _make_style_doc()

    portion = MagicMock()
    portion.getPropertyValue.side_effect = lambda name: "Text" if name == "TextPortionType" else 150
    portion.getPropertySetInfo.return_value = None

    style_cache = {}
    rep1 = fmt.apply_paragraph_style_preserving_direct_char(
        doc, _make_style_cursor([_make_para([portion])]), "Heading 1", style_cache=style_cache
    )
    rep2 = fmt.apply_paragraph_style_preserving_direct_char(
        doc, _make_style_cursor([_make_para([portion])]), "Heading 1", style_cache=style_cache
    )

    assert portion.getPropertySetInfo.call_count == 1
    assert rep1.get("capture_incomplete") is True
    assert rep2.get("capture_incomplete") is True
    assert "preserved_char_overrides" not in rep1
    assert "preserved_char_overrides" not in rep2


def test_capture_direct_char_overrides_counts_one_failure_and_continues():
    from unittest.mock import MagicMock
    from plugin.writer import styles as fmt

    doc, _para_styles, _style = _make_style_doc()

    # Para 1: getPropertyValue("ParaStyleName") raises RuntimeError
    para1 = MagicMock()
    para1.supportsService.side_effect = lambda s: s == "com.sun.star.text.Paragraph"
    para1.getPropertyValue.side_effect = RuntimeError("failed to read style")

    # Para 2: normal paragraph with 1 portion having CharWeight=150 override
    info = _make_prop_info("CharWeight")
    portion = MagicMock()
    portion.getPropertyValue.side_effect = lambda name: "Text" if name == "TextPortionType" else (150 if name == "CharWeight" else None)
    portion.getPropertySetInfo.return_value = info

    para2 = _make_para([portion])

    overrides, failures, notes = fmt._capture_direct_char_overrides(doc, _make_style_cursor([para1, para2]), {})
    assert len(overrides) == 1
    assert failures == 1
    assert notes == []
    assert overrides[0][1] == {"CharWeight": 150}

