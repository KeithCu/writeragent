# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Unit tests for plugin.writer.format helpers (no UNO)."""

import base64

import pytest

from plugin.writer.format import strip_embedded_image_data, _apply_image_export_options


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
    with patch("plugin.writer.format._is_recording_changes", return_value=True), \
         patch("plugin.writer.format.deletion_author", lambda: contextlib.nullcontext()), \
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
    with patch("plugin.writer.format._is_recording_changes", return_value=True):
        fmt.replace_preserving_format(object(), target, "NEW TEXT")  # in_undo_context defaults to False

    assert text.cursor.getString() == "NEW TEXT"  # single atomic replace
    assert text.inserts == []                     # no delete-then-insert two-step at all


def test_insert_content_at_position_text_selection_clears_range():
    from unittest.mock import MagicMock, patch

    from plugin.doc import visual_helpers
    from plugin.writer.format import insert_content_at_position

    text_rng = MagicMock()
    text_rng.getText.return_value.createTextCursorByRange.return_value = MagicMock()

    sel = MagicMock()
    sel.getCount.return_value = 1
    sel.getByIndex.return_value = text_rng

    controller = MagicMock()
    controller.getSelection.return_value = sel

    model = MagicMock()
    model.getCurrentController.return_value = controller
    model.getText.return_value.createTextCursor.return_value = MagicMock()

    with patch.object(visual_helpers, "is_graphic_object", return_value=False), patch(
        "plugin.writer.format._insert_mixed_or_plain_html"
    ):
        insert_content_at_position(model, MagicMock(), "<p>hi</p>", "selection")

    text_rng.setString.assert_called_once_with("")


def test_replace_preserving_format_atomic_when_split_author_false_even_in_undo_context():
    # Configurable coloring: split_author=False forces the SINGLE atomic setString (one author -> one
    # color) even INSIDE an open undo context, where split_author=True (the default) would use the
    # two-step delete+insert for split-author coloring. The two-step is gated on BOTH flags, so turning
    # coloring off collapses every recorded replace -- surgical or whole-block -- to one color, still
    # all-or-nothing.
    from unittest.mock import patch

    import plugin.writer.format as fmt

    text = _FakeText()
    target = _FakeRange(text)
    with patch("plugin.writer.format._is_recording_changes", return_value=True):
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
    with patch("plugin.writer.format._is_recording_changes", return_value=True), \
         patch("plugin.writer.format.deletion_author", lambda: contextlib.nullcontext()):
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
