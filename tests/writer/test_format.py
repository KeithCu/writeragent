# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Unit tests for plugin.writer.format helpers (no UNO)."""

import base64
import os
import sys
import tempfile as tempfile_mod

import pytest

from plugin.writer.format import (
    _apply_image_export_options,
    _content_has_block_markup,
    _resolve_temp_dir,
    _with_temp_buffer,
    strip_embedded_image_data,
)


def test_with_temp_buffer_yields_three_slash_file_url():
    with _with_temp_buffer(content="x", ext=".html") as (_path, url):
        assert url.startswith("file:///")


def test_resolve_temp_dir_avoids_gettempdir_under_crosshair(monkeypatch):
    """CrossHair FQN cover must not call gettempdir (auditwall SideEffectDetected)."""
    monkeypatch.setitem(sys.modules, "crosshair", object())
    monkeypatch.delenv("TMPDIR", raising=False)
    monkeypatch.delenv("TEMP", raising=False)
    monkeypatch.delenv("TMP", raising=False)
    monkeypatch.setattr(tempfile_mod, "tempdir", None)

    def boom() -> str:
        raise AssertionError("gettempdir must not be called under CrossHair")

    monkeypatch.setattr(tempfile_mod, "gettempdir", boom)
    assert _resolve_temp_dir() == os.curdir
    monkeypatch.setenv("TMPDIR", "/custom/tmp")
    assert _resolve_temp_dir() == "/custom/tmp"


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

    import plugin.writer.format as fmt

    text = _FakeText()
    target = _FakeRange(text)
    with patch("plugin.writer.image_placeholders._is_recording_changes", return_value=True), \
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

    import plugin.writer.format as fmt

    text = _FakeText()
    target = _FakeRange(text)
    with patch("plugin.writer.image_placeholders._is_recording_changes", return_value=True):
        fmt.replace_preserving_format(object(), target, "NEW TEXT")  # in_undo_context defaults to False

    assert text.cursor.getString() == "NEW TEXT"  # single atomic replace
    assert text.inserts == []                     # no delete-then-insert two-step at all


def test_insert_content_at_position_text_selection_clears_range():
    from unittest.mock import MagicMock, patch

    from plugin.doc import visual_helpers
    from plugin.writer.format import insert_content_at_position

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

    with patch.object(visual_helpers, "is_graphic_object", return_value=False), patch(
        "plugin.writer.html_import._insert_mixed_or_plain_html"
    ), patch(
        # Master nested-table check walks MagicMock enumerations forever.
        "plugin.writer.table_nesting.raise_if_range_hosts_nested_table",
    ):
        insert_content_at_position(model, MagicMock(), "<p>hi</p>", "selection")

    text_rng.setString.assert_called_once_with("")


def test_insert_content_at_position_two_ranges_clears_non_collapsed_range():
    from unittest.mock import MagicMock, patch

    from plugin.doc import visual_helpers
    from plugin.writer.format import insert_content_at_position

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

    with patch.object(visual_helpers, "is_graphic_object", return_value=False), patch(
        "plugin.writer.html_import._insert_mixed_or_plain_html"
    ), patch(
        "plugin.writer.table_nesting.raise_if_range_hosts_nested_table",
    ):
        insert_content_at_position(model, MagicMock(), "<p>hi</p>", "selection")

    rng0.setString.assert_not_called()
    rng1.setString.assert_called_once_with("")


def test_insert_content_at_position_shape_selection_falls_back_to_doc_end():
    """After shape.upsert, controller selects the Draw shape — HTML insert must not use it."""
    from unittest.mock import MagicMock, patch

    from plugin.writer.format import insert_content_at_position

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

    with patch("plugin.doc.visual_helpers.is_graphic_object", return_value=False), patch(
        "plugin.writer.html_import._insert_mixed_or_plain_html"
    ) as mock_insert:
        insert_content_at_position(model, MagicMock(), "<p>hi</p>", "selection")

    body_cursor.gotoEnd.assert_called_once_with(False)
    mock_insert.assert_called_once()
    assert mock_insert.call_args.args[2] is body_cursor
    shape.setString.assert_not_called()


def test_insert_content_at_position_empty_selection_uses_doc_end():
    from unittest.mock import MagicMock, patch

    from plugin.writer.format import insert_content_at_position

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

    with patch("plugin.doc.visual_helpers.is_graphic_object", return_value=False), patch(
        "plugin.writer.html_import._insert_mixed_or_plain_html"
    ) as mock_insert:
        insert_content_at_position(model, MagicMock(), "<p>hi</p>", "selection")

    body_cursor.gotoEnd.assert_called_once_with(False)
    mock_insert.assert_called_once()






def test_insert_content_at_position_beginning_applies_styles():
    """beginning path passes apply_styles=True (no PARAGRAPH_BREAK prep needed at para start)."""
    from unittest.mock import MagicMock, patch

    from plugin.writer.format import insert_content_at_position

    body_cursor = MagicMock()
    body_text = MagicMock()
    body_text.createTextCursor.return_value = body_cursor
    model = MagicMock()
    model.getText.return_value = body_text

    with patch("plugin.writer.html_import._insert_mixed_or_plain_html") as mock_insert, patch(
        "plugin.writer.html_import._ensure_empty_absorb_for_styled_insert",
        side_effect=lambda text, cursor, position: cursor,
    ) as mock_prep:
        insert_content_at_position(model, MagicMock(), "<p data-lo-style=\"Heading1\">x</p>", "beginning")

    body_cursor.gotoStart.assert_called_once_with(False)
    mock_prep.assert_called_once()
    assert mock_prep.call_args.args[2] == "beginning"
    mock_insert.assert_called_once()
    assert mock_insert.call_args.kwargs.get("apply_styles") is True


def test_insert_content_at_position_end_preps_then_applies_styles():
    """end path runs absorb prep then passes apply_styles=True."""
    from unittest.mock import MagicMock, patch

    from plugin.writer.format import insert_content_at_position

    body_cursor = MagicMock()
    body_text = MagicMock()
    body_text.createTextCursor.return_value = body_cursor
    model = MagicMock()
    model.getText.return_value = body_text

    with patch("plugin.writer.html_import._insert_mixed_or_plain_html") as mock_insert, patch(
        "plugin.writer.html_import._ensure_empty_absorb_for_styled_insert",
        side_effect=lambda text, cursor, position: cursor,
    ) as mock_prep:
        insert_content_at_position(model, MagicMock(), "<p data-lo-style=\"Caption\">x</p>", "end")

    body_cursor.gotoEnd.assert_called_once_with(False)
    mock_prep.assert_called_once()
    assert mock_prep.call_args.args[2] == "end"
    mock_insert.assert_called_once()
    assert mock_insert.call_args.kwargs.get("apply_styles") is True


def test_insert_content_at_position_selection_still_skips_styles():
    from unittest.mock import MagicMock, patch

    from plugin.writer.format import insert_content_at_position

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

    with patch("plugin.doc.visual_helpers.is_graphic_object", return_value=False), patch(
        "plugin.writer.html_import._insert_mixed_or_plain_html"
    ) as mock_insert:
        insert_content_at_position(model, MagicMock(), "<p data-lo-style=\"Heading1\">x</p>", "selection")

    mock_insert.assert_called_once()
    assert mock_insert.call_args.kwargs.get("apply_styles") is False


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

    from plugin.doc import visual_helpers
    from plugin.framework.errors import ToolExecutionError
    from plugin.writer.format import insert_content_at_position

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

    with patch.object(visual_helpers, "is_graphic_object", return_value=False), patch(
        "plugin.writer.html_import._insert_mixed_or_plain_html"
    ) as mock_insert, patch(
        "plugin.writer.table_nesting.raise_if_range_hosts_nested_table",
        side_effect=ToolExecutionError("nested tables"),
    ):
        with pytest.raises(ToolExecutionError, match="nested tables"):
            insert_content_at_position(model, MagicMock(), "<p>hi</p>", "selection")
    mock_insert.assert_not_called()
    body_cursor.gotoEnd.assert_not_called()


def test_insert_content_at_position_clear_failure_is_not_moved_to_end():
    from unittest.mock import MagicMock, patch

    from plugin.doc import visual_helpers
    from plugin.writer.format import insert_content_at_position

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

    with patch.object(visual_helpers, "is_graphic_object", return_value=False), patch(
        "plugin.writer.html_import._insert_mixed_or_plain_html"
    ) as mock_insert, patch(
        "plugin.writer.table_nesting.raise_if_range_hosts_nested_table",
    ):
        with pytest.raises(RuntimeError, match="clear failed"):
            insert_content_at_position(model, MagicMock(), "<p>hi</p>", "selection")
    mock_insert.assert_not_called()
    body_cursor.gotoEnd.assert_not_called()


def test_insert_content_at_position_attribute_error_still_uses_doc_end():
    from unittest.mock import MagicMock, patch

    from plugin.doc import visual_helpers
    from plugin.writer.format import insert_content_at_position

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

    with patch.object(visual_helpers, "is_graphic_object", return_value=False), patch(
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


def test_replace_preserving_format_raises_when_go_right_stops():
    from unittest.mock import MagicMock, patch

    from plugin.framework.errors import ToolExecutionError
    from plugin.writer import html_import as hi, image_placeholders

    class _Cursor:
        def __init__(self):
            self.writes = []

        def goRight(self, _n, _expand):
            return False

        def setString(self, value):
            self.writes.append(value)

        def getEnd(self):
            return object()

        def gotoRange(self, _end, _expand):
            return None

        def getPropertyValue(self, _name):
            return None

    class _Text:
        def __init__(self):
            self.cursor = _Cursor()
            self.inserts = []

        def createTextCursorByRange(self, _r):
            return self.cursor

        def insertString(self, *_args):
            self.inserts.append("insert")

    class _Range:
        def __init__(self, text):
            self._text = text

        def getText(self):
            return self._text

        def getString(self):
            return "AB"

        def getStart(self):
            return object()

    text = _Text()
    ctx = MagicMock()
    with patch.object(image_placeholders, "_is_recording_changes", return_value=False):
        with pytest.raises(ToolExecutionError, match="did not advance"):
            hi.replace_preserving_format(MagicMock(), _Range(text), "XY", ctx=ctx)
    assert text.cursor.writes == []
    assert text.inserts == []
    ctx.getServiceManager.assert_not_called()


def test_replace_preserving_format_skips_equal_runs():
    from unittest.mock import MagicMock, patch
    from plugin.writer import html_import as hi, image_placeholders

    moves = []
    writes = []

    class _Cursor:
        def goRight(self, n, expand):
            moves.append((n, expand))
            return True

        def setString(self, value):
            writes.append(value)

        def getEnd(self):
            return object()

        def gotoRange(self, _end, _expand):
            pass

    cursor = _Cursor()

    class _Text:
        def createTextCursorByRange(self, _r):
            return cursor

        def insertString(self, *_args):
            pass

    class _Range:
        def __init__(self, text, string):
            self._text = text
            self._string = string

        def getText(self):
            return self._text

        def getString(self):
            return self._string

        def getStart(self):
            return object()

    text = _Text()
    with patch.object(image_placeholders, "_is_recording_changes", return_value=False):
        hi.replace_preserving_format(MagicMock(), _Range(text, "AAAAAX"), "AAAAAY")

    assert moves == [(5, False), (1, True)]
    assert writes == ["Y"]


def test_replace_preserving_format_special_portion_keeps_one_step_loop():
    from unittest.mock import MagicMock, patch
    from plugin.writer import html_import as hi, image_placeholders

    moves = []
    writes = []

    class _Cursor:
        def goRight(self, n, expand):
            moves.append((n, expand))
            return True

        def setString(self, value):
            writes.append(value)

        def getEnd(self):
            return object()

        def gotoRange(self, _end, _expand):
            pass

    cursor = _Cursor()

    class _Text:
        def createTextCursorByRange(self, _r):
            return cursor

        def insertString(self, *_args):
            pass

    class _Enum:
        def __init__(self, items):
            self._items = list(items)
        def hasMoreElements(self):
            return bool(self._items)
        def nextElement(self):
            return self._items.pop(0)

    class _Portion:
        TextPortionType = "TextField"

    class _Para:
        def createEnumeration(self):
            return _Enum([_Portion()])

    class _Range:
        def __init__(self, text, string):
            self._text = text
            self._string = string

        def getText(self):
            return self._text

        def getString(self):
            return self._string

        def getStart(self):
            return object()

        def createEnumeration(self):
            return _Enum([_Para()])

    text = _Text()
    with patch.object(image_placeholders, "_is_recording_changes", return_value=False):
        hi.replace_preserving_format(MagicMock(), _Range(text, "AAAAAX"), "AAAAAY")

    # Special portion: each character is checked 1-by-1
    assert moves == [(1, True)] * 6
    assert writes == ["Y"]


def test_replace_preserving_format_200_char_plain_issues_one_setstring():
    from unittest.mock import MagicMock, patch
    from plugin.writer import html_import as hi, image_placeholders

    writes = []
    moves = []

    class _Cursor:
        def goRight(self, n, expand):
            moves.append((n, expand))
            return True
        def setString(self, val):
            writes.append(val)
        def getEnd(self):
            return object()
        def gotoRange(self, _end, _expand):
            pass

    class _Text:
        def createTextCursorByRange(self, _r):
            return _Cursor()
        def insertString(self, *_args):
            pass

    class _Enum:
        def __init__(self, items):
            self._items = list(items)
        def hasMoreElements(self):
            return bool(self._items)
        def nextElement(self):
            return self._items.pop(0)

    class _Portion:
        def __init__(self, text):
            self.TextPortionType = "Text"
            self._text = text
        def getString(self):
            return self._text

    class _Para:
        def __init__(self, portions):
            self._portions = portions
        def createEnumeration(self):
            return _Enum(self._portions)

    old_str = "A" * 200
    new_str = "B" * 200

    class _Range:
        def getText(self):
            return _Text()
        def getString(self):
            return old_str
        def getStart(self):
            return object()
        def createEnumeration(self):
            return _Enum([_Para([_Portion(old_str)])])

    with patch.object(image_placeholders, "_is_recording_changes", return_value=False):
        hi.replace_preserving_format(MagicMock(), _Range(), new_str)

    assert writes == [new_str]
    assert len(writes) == 1


def test_replace_preserving_format_two_portions_issue_two_setstrings():
    from unittest.mock import MagicMock, patch
    from plugin.writer import html_import as hi, image_placeholders

    writes = []

    class _Cursor:
        def goRight(self, n, expand):
            return True
        def setString(self, val):
            writes.append(val)
        def getEnd(self):
            return object()
        def gotoRange(self, _end, _expand):
            pass

    class _Text:
        def createTextCursorByRange(self, _r):
            return _Cursor()
        def insertString(self, *_args):
            pass

    class _Enum:
        def __init__(self, items):
            self._items = list(items)
        def hasMoreElements(self):
            return bool(self._items)
        def nextElement(self):
            return self._items.pop(0)

    class _Portion:
        def __init__(self, text, is_bold=False):
            self.TextPortionType = "Text"
            self._text = text
            self.is_bold = is_bold
        def getString(self):
            return self._text

    p1 = _Portion("plain text ", is_bold=False)
    p2 = _Portion("bold text", is_bold=True)
    old_str = "plain text bold text"
    new_str = "plain word BOLD TEXT"

    class _Para:
        def createEnumeration(self):
            return _Enum([p1, p2])

    class _Range:
        def getText(self):
            return _Text()
        def getString(self):
            return old_str
        def getStart(self):
            return object()
        def createEnumeration(self):
            return _Enum([_Para()])

    with patch.object(image_placeholders, "_is_recording_changes", return_value=False):
        hi.replace_preserving_format(MagicMock(), _Range(), new_str)

    assert len(writes) == 2
    assert writes == ["word", "BOLD TEXT"]


def test_replace_preserving_format_equal_string_is_noop():
    from unittest.mock import MagicMock, patch
    from plugin.writer import html_import as hi, image_placeholders

    moves = []
    writes = []

    class _Cursor:
        def goRight(self, n, expand):
            moves.append((n, expand))
            return True

        def setString(self, value):
            writes.append(value)

    cursor = _Cursor()

    class _Text:
        def createTextCursorByRange(self, _r):
            return cursor

    class _Range:
        def getText(self):
            return _Text()

        def getString(self):
            return "IDENTICAL"

        def getStart(self):
            return object()

    with patch.object(image_placeholders, "_is_recording_changes", return_value=False):
        hi.replace_preserving_format(MagicMock(), _Range(), "IDENTICAL")

    assert moves == []
    assert writes == []


def test_replace_preserving_format_equal_run_go_right_false_raises():
    from unittest.mock import MagicMock, patch
    from plugin.framework.errors import ToolExecutionError
    from plugin.writer import html_import as hi, image_placeholders

    writes = []

    class _Cursor:
        def goRight(self, n, expand):
            return False

        def setString(self, value):
            writes.append(value)

    cursor = _Cursor()

    class _Text:
        def createTextCursorByRange(self, _r):
            return cursor

    class _Range:
        def getText(self):
            return _Text()

        def getString(self):
            return "AAAAAX"

        def getStart(self):
            return object()

    with patch.object(image_placeholders, "_is_recording_changes", return_value=False):
        with pytest.raises(ToolExecutionError, match="did not advance"):
            hi.replace_preserving_format(MagicMock(), _Range(), "AAAAAY")

    assert writes == []


def test_replace_preserving_format_atomic_when_split_author_false_even_in_undo_context():
    # Configurable coloring: split_author=False forces the SINGLE atomic setString (one author -> one
    # color) even INSIDE an open undo context, where split_author=True (the default) would use the
    # two-step delete+insert for split-author coloring. The two-step is gated on BOTH flags, so turning
    # coloring off collapses every recorded replace — surgical or whole-block — to one color, still
    # all-or-nothing.
    from unittest.mock import patch

    import plugin.writer.format as fmt

    text = _FakeText()
    target = _FakeRange(text)
    with patch("plugin.writer.image_placeholders._is_recording_changes", return_value=True):
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

    import plugin.writer.format as fmt

    class _OkText(_FakeText):
        def insertString(self, _cursor, s, _sel):  # never fails -> no restore
            self.inserts.append(s)

    text = _OkText()
    target = _FakeRange(text)
    with patch("plugin.writer.image_placeholders._is_recording_changes", return_value=True), \
         patch("plugin.writer.review_authors.deletion_author", lambda: contextlib.nullcontext()):
        fmt.replace_preserving_format(object(), target, "NEW TEXT", in_undo_context=True)

    assert text.cursor.getString() == ""   # the deletion emptied the range (step 1)
    assert text.inserts == ["NEW TEXT"]    # then the new text was inserted (step 2)


def test_run_writer_mutation_with_optional_review_import_error():
    """LibrePy omits edit_review; helper must apply the mutation directly."""
    from unittest.mock import MagicMock, patch

    from plugin.writer.format import run_writer_mutation_with_optional_review

    apply_fn = MagicMock()
    real_import = __import__

    def import_without_edit_review(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "plugin.writer.edit_review":
            raise ImportError("No module named 'plugin.writer.edit_review'")
        return real_import(name, globals, locals, fromlist, level)

    with patch("builtins.__import__", side_effect=import_without_edit_review):
        run_writer_mutation_with_optional_review(MagicMock(), MagicMock(), apply_fn)
    apply_fn.assert_called_once()


def test_document_to_content_full_timing_path_with_mocks():
    """Phase timing logs must not change the full-scope export result."""
    from unittest.mock import patch

    import plugin.writer.format as fmt
    import plugin.writer.html_export as html_export

    xhtml = '<html><body><p class="paragraph-Text_20_body">hello</p></body></html>'
    with (
        patch.object(html_export, "_export_xhtml", return_value=xhtml) as export_xhtml,
        patch.object(html_export, "_autostyle_maps", return_value=({}, {}, False)) as autostyle,
        patch.object(fmt.xhtml_post, "xhtml_to_semantic_html", return_value="<p>hello</p>") as post,
    ):
        out = fmt.document_to_content(object(), object(), None, scope="full")

    export_xhtml.assert_called_once()
    autostyle.assert_called_once()
    post.assert_called_once_with(xhtml, {}, {})
    assert out == "<p>hello</p>"


def test_format_module_avoids_document_helpers_ops_review_authors():
    """LibrePy RPS insert loads format.py; chat export/replace stay local imports."""
    import ast
    from pathlib import Path

    import plugin.writer.format as fmt

    tree = ast.parse(Path(fmt.__file__).read_text(encoding="utf-8"))
    mods: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module:
            mods.append(node.module)
        elif isinstance(node, ast.Import):
            mods.extend(alias.name for alias in node.names)
    assert "plugin.doc.document_helpers" not in mods
    assert ".ops" not in mods
    assert "ops" not in mods
    assert ".review_authors" not in mods
    assert "review_authors" not in mods


def test_deletion_author_noop_when_review_authors_missing(monkeypatch):
    """LibrePy omits review_authors; tracked-delete helper must not ImportError."""
    import contextlib
    import sys

    import plugin.writer.format as fmt

    monkeypatch.setitem(sys.modules, "plugin.writer.review_authors", None)
    cm = fmt._deletion_author()
    with cm:
        pass
    assert type(cm).__name__ == type(contextlib.nullcontext()).__name__ or hasattr(cm, "__enter__")


def test_resolve_style_name():
    from unittest.mock import MagicMock
    import plugin.writer.format as fmt

    fam = MagicMock()
    fam.hasByName.side_effect = lambda name: name == "Heading 1"
    fam.getElementNames.return_value = ("Heading 1", "Standard")
    families = MagicMock()
    families.getByName.return_value = fam
    model = MagicMock()
    model.getStyleFamilies.return_value = families

    # Exact match
    assert fmt._resolve_style_name(model, "Heading 1") == "Heading 1"
    # Case-insensitive match
    assert fmt._resolve_style_name(model, "heading 1") == "Heading 1"
    # Fallback to input string on unknown style
    assert fmt._resolve_style_name(model, "UnknownStyle") == "UnknownStyle"


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
    from plugin.writer import format as fmt

    cursor = _MultiStatesCursor()
    cleared = fmt._reset_properties_to_default(cursor, fmt.CLEARABLE_PARA_PROPERTIES)

    assert cursor.cleared == fmt.CLEARABLE_PARA_PROPERTIES
    assert cleared == list(fmt.CLEARABLE_PARA_PROPERTIES)


def test_clear_direct_paragraph_properties_falls_back_one_at_a_time():
    from plugin.writer import format as fmt

    cursor = _SingleStateCursor(unsupported={"ParaSplit"})
    cleared = fmt._reset_properties_to_default(cursor, fmt.CLEARABLE_PARA_PROPERTIES)

    assert "ParaLeftMargin" in cleared and "ParaFirstLineIndent" in cleared
    assert "ParaSplit" not in cleared  # reported honestly as not cleared


def test_clear_direct_never_touches_non_paragraph_properties():
    """Deliberately narrower than setAllPropertiesToDefault: numbering, borders and language are
    not the caller's to lose."""
    from plugin.writer import format as fmt

    assert all(name.startswith("Para") for name in fmt.CLEARABLE_PARA_PROPERTIES)
    for unwanted in ("NumberingRules", "ParaStyleName", "CharLocale"):
        assert unwanted not in fmt.CLEARABLE_PARA_PROPERTIES


def test_clearable_para_properties_are_the_single_source_for_styles_schema():
    """styles.py used to duplicate the name list (stale 'cycle' comment). format owns it."""
    from plugin.writer import format as fmt
    from plugin.writer import styles as st

    assert tuple(st._KNOWN_PARAGRAPH_PROPERTIES) == fmt.CLEARABLE_PARA_PROPERTIES
    assert set(st._PARA_PROPERTY_SCHEMA) == set(fmt.CLEARABLE_PARA_PROPERTIES)


def test_summarize_char_overrides_is_json_safe_and_first_portion_wins():
    from plugin.writer import format as fmt

    posture = type("FontSlant", (), {"__module__": "uno", "value": "ITALIC"})()

    overrides = [
        (object(), {"CharFontName": "Times New Roman", "CharHeight": 12.0, "CharLocale": object(), "CharPosture": posture}),
        (object(), {"CharFontName": "Courier"}),
    ]
    summary = fmt._summarize_char_overrides(overrides)

    assert summary == {"CharFontName": "Times New Roman", "CharHeight": 12.0, "CharPosture": 2}
    assert "CharLocale" not in summary  # UNO struct -> not serialisable, left out


def test_summarize_char_overrides_reports_asian_complex():
    """style_props clears Asian/Complex font slots; the report must say so (honest, not silent)."""
    from plugin.writer import format as fmt

    overrides = [(object(), {
        "CharFontName": "Times New Roman",
        "CharFontNameAsian": "SimSun",
        "CharFontNameComplex": "Tahoma",
        "CharHeight": 12.0,
        "CharHeightAsian": 10.5,
        "CharHeightComplex": 11.0,
    })]
    summary = fmt._summarize_char_overrides(overrides)

    assert summary["CharFontNameAsian"] == "SimSun"
    assert summary["CharFontNameComplex"] == "Tahoma"
    assert summary["CharHeightAsian"] == 10.5
    assert summary["CharHeightComplex"] == 11.0


def test_reported_char_properties_cover_style_governed():
    """Keep the clear-set and the report-set aligned: every slot style_props wipes is echoed."""
    from plugin.writer import format as fmt

    for name in fmt.STYLE_GOVERNED_CHAR_PROPERTIES:
        assert name in fmt.REPORTED_CHAR_PROPERTIES


def test_enum_hit_walk_cap_and_warning_are_honest():
    from unittest.mock import MagicMock
    from plugin.writer import format as fmt

    more = MagicMock()
    more.hasMoreElements.return_value = True
    done = MagicMock()
    done.hasMoreElements.return_value = False

    assert fmt.enum_hit_walk_cap(50, 50, more) is True
    assert fmt.enum_hit_walk_cap(49, 50, more) is False
    assert fmt.enum_hit_walk_cap(50, 50, done) is False
    dest = []
    assert fmt.record_walk_cap(more, 50, 50, "text portions", dest) is True
    assert dest and "cap 50" in dest[0]
    assert "incomplete" in dest[0]
    assert fmt.record_walk_cap(done, 50, 50, "text portions", dest) is False


def test_apply_style_origin_limitation_still_value_compare() -> None:
    """GHA 34683742049 canary FAIL was leftover reuse, not a product fix."""
    from pathlib import Path

    from plugin.writer import format as fmt

    src = Path(fmt.__file__).read_text(encoding="utf-8")
    assert "KNOWN LIMITATION: a direct override whose value equals the old style default" in src
    assert "differs from the paragraph's current style default" in src


def test_style_governed_char_properties_spare_bold_and_italic():
    """style_props drops the font the style owns; hand-set emphasis is the user's, not the style's."""
    from plugin.writer import format as fmt

    assert "CharFontName" in fmt.STYLE_GOVERNED_CHAR_PROPERTIES
    assert "CharHeight" in fmt.STYLE_GOVERNED_CHAR_PROPERTIES
    assert "CharWeight" not in fmt.STYLE_GOVERNED_CHAR_PROPERTIES   # bold survives
    assert "CharPosture" not in fmt.STYLE_GOVERNED_CHAR_PROPERTIES  # italic survives


def test_report_lists_only_the_overrides_actually_removed():
    """Under style_props, bold/italic are restored — listing them as removed would be wrong."""
    from plugin.writer import format as fmt

    found = {"CharFontName": "Times New Roman", "CharHeight": 12.0, "CharWeight": 150.0}
    skip = set(fmt.STYLE_GOVERNED_CHAR_PROPERTIES)
    removed = {k: v for k, v in found.items() if k in skip}

    assert removed == {"CharFontName": "Times New Roman", "CharHeight": 12.0}
    assert "CharWeight" not in removed


def test_reset_properties_to_default_is_a_noop_on_empty_names():
    from plugin.writer import format as fmt

    cursor = _MultiStatesCursor()
    assert fmt._reset_properties_to_default(cursor, ()) == []
    assert cursor.cleared is None  # never call UNO with an empty set


def test_reset_properties_to_default_takes_the_caller_s_names():
    """The clear is scoped by the caller — style_props clears the font, all clears what was found."""
    from plugin.writer import format as fmt

    cursor = _MultiStatesCursor()
    fmt._reset_properties_to_default(cursor, fmt.STYLE_GOVERNED_CHAR_PROPERTIES)

    assert cursor.cleared == fmt.STYLE_GOVERNED_CHAR_PROPERTIES


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
    assert truncated
    assert "cap 2" in truncated[0]
    assert "incomplete" in truncated[0]


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
    from unittest.mock import MagicMock, patch
    from plugin.writer import range_copy as rc

    portion = MagicMock()
    para = MagicMock()
    para_start = MagicMock(name="para_start")
    para_cursor = MagicMock()
    para_cursor.getStart.return_value = para_start
    run = MagicMock()
    temp_text = MagicMock()
    temp_text.createTextCursor.return_value = para_cursor
    temp_text.createTextCursorByRange.return_value = run

    def copy_side_effect(src, dst, names, style=None):
        if names is rc._COPIED_PARA_PROPERTIES:
            raise RuntimeError("refused Para*")

    with patch.object(rc, "_copy_properties", side_effect=copy_side_effect) as copy:
        rc._paint_direct_formatting(para, [(portion, "bold")], temp_text, 0, 4)

    names_seen = [c.args[2] for c in copy.call_args_list]
    assert rc._COPIED_PARA_PROPERTIES in names_seen
    assert rc._COPIED_CHAR_PROPERTIES in names_seen
    char_call = next(c for c in copy.call_args_list if c.args[2] is rc._COPIED_CHAR_PROPERTIES)
    assert char_call.args[0] is portion


def test_source_style_is_cached_and_tolerates_a_missing_style():
    from unittest.mock import MagicMock
    from plugin.writer import html_export as hx

    model = MagicMock()
    families = model.getStyleFamilies.return_value.getByName.return_value
    families.getByName.side_effect = lambda n: "STYLE" if n == "Standard" else (_ for _ in ()).throw(KeyError(n))

    cache = {}
    assert hx._source_style(model, "Standard", cache) == "STYLE"
    assert hx._source_style(model, "Standard", cache) == "STYLE"
    assert families.getByName.call_count == 1  # second read served from the cache
    assert hx._source_style(model, "Nope", cache) is None
    assert hx._source_style(model, "", cache) is None


def test_format_uno_skips_windows_leftover_hidden_apply() -> None:
    """GHA 34683742049 / 34685648395 / 35470191616: leftover Hidden and pool reuse."""
    from pathlib import Path

    src = Path(__file__).with_name("test_format_uno.py").read_text(encoding="utf-8")
    assert "skip_windows_leftover_hidden_load" in src
    assert "apply_document_content Hidden _default swriter" in src
    assert "34683742049" in src
    assert "format_uno Hidden _blank small_doc" in src
    assert "34685648395" in src
    assert "format_uno cross-paragraph color leftover reuse" in src
    assert "non-linebreak" in src
    assert "35466498641" in src
    assert "skip_windows_pooled_writer_reuse" in src
    assert "format_uno cross-paragraph color pooled reuse" in src
    assert "35470191616" in src


# --- table-cell HTML block gate (from test_cell_block_html_gate.py) ---
# R3: pins the block-vs-inline classification that gates the clear "rich HTML in a table cell"
# error in replace_single_range_with_content.

def test_plain_text_is_not_block():
    assert _content_has_block_markup("hello world") is False


def test_inline_tags_are_not_block():
    # <b>/<span>/<i> are inline -> they go through the in-cell path that works; the cell-error
    # gate must NOT fire for them.
    assert _content_has_block_markup("<b>bold</b> text") is False


def test_paragraph_is_block():
    # <p> is block -> the path that can raise inside a table cell; the clear-error gate fires here.
    assert _content_has_block_markup("<p>a paragraph</p>") is True


def test_heading_is_block():
    assert _content_has_block_markup("<h1>title</h1>") is True


def test_apply_paragraph_style_preserving_direct_char_caches_style_and_props():
    from unittest.mock import MagicMock
    from plugin.writer import format as fmt

    doc = MagicMock()
    para_styles = MagicMock()
    doc.getStyleFamilies.return_value.getByName.return_value = para_styles

    style = MagicMock()
    para_styles.getByName.return_value = style
    style.getPropertyValue.side_effect = lambda name: "DefaultVal"

    # Paragraph 1 with 2 portions
    portion1 = MagicMock()
    portion1.getPropertyValue.side_effect = lambda name: "Text" if name == "TextPortionType" else "CustomVal"
    prop_char = MagicMock()
    prop_char.Name = "CharWeight"
    prop_char.Attributes = 0
    prop_set_info = MagicMock()
    prop_set_info.getProperties.return_value = [prop_char]
    portion1.getPropertySetInfo.return_value = prop_set_info

    portion2 = MagicMock()
    portion2.getPropertyValue.side_effect = lambda name: "Text" if name == "TextPortionType" else "CustomVal2"
    portion2.getPropertySetInfo.return_value = MagicMock()

    para1 = MagicMock()
    para1.supportsService.side_effect = lambda s: s == "com.sun.star.text.Paragraph"
    para1.getPropertyValue.side_effect = lambda name: "Standard" if name == "ParaStyleName" else None
    para1_enum = MagicMock()
    para1_enum.hasMoreElements.side_effect = [True, True, False]
    para1_enum.nextElement.side_effect = [portion1, portion2]
    para1.createEnumeration.return_value = para1_enum

    # Paragraph 2 with 1 portion (same style "Standard")
    portion3 = MagicMock()
    portion3.getPropertyValue.side_effect = lambda name: "Text" if name == "TextPortionType" else "CustomVal3"
    para2 = MagicMock()
    para2.supportsService.side_effect = lambda s: s == "com.sun.star.text.Paragraph"
    para2.getPropertyValue.side_effect = lambda name: "Standard" if name == "ParaStyleName" else None
    para2_enum = MagicMock()
    para2_enum.hasMoreElements.side_effect = [True, False]
    para2_enum.nextElement.side_effect = [portion3]
    para2.createEnumeration.return_value = para2_enum

    # Call 1: applies to para1 with cache
    cursor1 = MagicMock()
    doc_enum1 = MagicMock()
    doc_enum1.hasMoreElements.side_effect = [True, False]
    doc_enum1.nextElement.side_effect = [para1]
    cursor1.getText.return_value.createTextCursorByRange.return_value = cursor1
    cursor1.createEnumeration.return_value = doc_enum1

    style_cache = {}
    fmt.apply_paragraph_style_preserving_direct_char(doc, cursor1, "Heading 1", style_cache=style_cache)

    portion1.getPropertySetInfo.assert_called_once()
    portion2.getPropertySetInfo.assert_not_called()
    assert style.getPropertyValue.call_count == 1  # once for CharWeight
    assert para_styles.getByName.call_count == 1

    # Call 2: applies to para2 with the same shared cache
    cursor2 = MagicMock()
    doc_enum2 = MagicMock()
    doc_enum2.hasMoreElements.side_effect = [True, False]
    doc_enum2.nextElement.side_effect = [para2]
    cursor2.getText.return_value.createTextCursorByRange.return_value = cursor2
    cursor2.createEnumeration.return_value = doc_enum2

    fmt.apply_paragraph_style_preserving_direct_char(doc, cursor2, "Heading 1", style_cache=style_cache)

    # para_styles.getByName should NOT be called again for "Standard"
    assert para_styles.getByName.call_count == 1
    portion3.getPropertySetInfo.assert_not_called()


def test_apply_paragraph_style_capture_failure_and_disposal():
    from unittest.mock import MagicMock
    import pytest
    from plugin.framework.errors import DocumentDisposedError
    from plugin.writer import format as fmt

    doc = MagicMock()
    para_styles = MagicMock()
    doc.getStyleFamilies.return_value.getByName.return_value = para_styles
    style = MagicMock()
    para_styles.getByName.return_value = style
    style.getPropertyValue.return_value = 100

    prop1 = MagicMock()
    prop1.Name = "CharWeight"
    prop1.Attributes = 0
    prop2 = MagicMock()
    prop2.Name = "CharHeight"
    prop2.Attributes = 0
    info = MagicMock()
    info.getProperties.return_value = [prop1, prop2]

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

    para = MagicMock()
    para.supportsService.side_effect = lambda s: s == "com.sun.star.text.Paragraph"
    para.getPropertyValue.side_effect = lambda name: "Standard" if name == "ParaStyleName" else None
    para_enum = MagicMock()
    para_enum.hasMoreElements.side_effect = [True, False]
    para_enum.nextElement.side_effect = [portion]
    para.createEnumeration.return_value = para_enum

    cursor = MagicMock()
    doc_enum = MagicMock()
    doc_enum.hasMoreElements.side_effect = [True, False]
    doc_enum.nextElement.side_effect = [para]
    cursor.getText.return_value.createTextCursorByRange.return_value = cursor
    cursor.createEnumeration.return_value = doc_enum

    rep = fmt.apply_paragraph_style_preserving_direct_char(doc, cursor, "Heading 1", clear_direct="none")
    assert rep.get("capture_incomplete") is True
    assert "warning" in rep
    assert rep.get("preserved_char_overrides", {}).get("CharWeight") == 150

    # Disposal exception propagates as DocumentDisposedError
    DisposedExc = type("DisposedException", (Exception,), {})
    portion_disp = MagicMock()
    portion_disp.getPropertyValue.side_effect = lambda name: "Text" if name == "TextPortionType" else (_ for _ in ()).throw(DisposedExc("object disposed"))
    portion_disp.getPropertySetInfo.return_value = info

    para_disp = MagicMock()
    para_disp.supportsService.side_effect = lambda s: s == "com.sun.star.text.Paragraph"
    para_disp.getPropertyValue.side_effect = lambda name: "Standard" if name == "ParaStyleName" else None
    para_disp_enum = MagicMock()
    para_disp_enum.hasMoreElements.side_effect = [True, False]
    para_disp_enum.nextElement.side_effect = [portion_disp]
    para_disp.createEnumeration.return_value = para_disp_enum

    cursor_disp = MagicMock()
    doc_disp_enum = MagicMock()
    doc_disp_enum.hasMoreElements.side_effect = [True, False]
    doc_disp_enum.nextElement.side_effect = [para_disp]
    cursor_disp.getText.return_value.createTextCursorByRange.return_value = cursor_disp
    cursor_disp.createEnumeration.return_value = doc_disp_enum

    with pytest.raises(DocumentDisposedError):
        fmt.apply_paragraph_style_preserving_direct_char(doc, cursor_disp, "Heading 1")

