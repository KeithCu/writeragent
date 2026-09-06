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
    with patch("plugin.writer.html_import._is_recording_changes", return_value=True), \
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
    with patch("plugin.writer.html_import._is_recording_changes", return_value=True):
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
        "plugin.writer.html_import._insert_mixed_or_plain_html"
    ):
        insert_content_at_position(model, MagicMock(), "<p>hi</p>", "selection")

    text_rng.setString.assert_called_once_with("")


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
    with patch("plugin.writer.html_import._is_recording_changes", return_value=True):
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
    with patch("plugin.writer.html_import._is_recording_changes", return_value=True), \
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
        patch.object(html_export, "_autostyle_maps", return_value=({}, {})) as autostyle,
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
    assert "plugin.doc.text_helpers" in mods
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


def test_summarize_char_overrides_is_json_safe_and_first_portion_wins():
    from plugin.writer import format as fmt

    overrides = [
        (object(), {"CharFontName": "Times New Roman", "CharHeight": 12.0, "CharLocale": object()}),
        (object(), {"CharFontName": "Courier"}),
    ]
    summary = fmt._summarize_char_overrides(overrides)

    assert summary == {"CharFontName": "Times New Roman", "CharHeight": 12.0}
    assert "CharLocale" not in summary  # UNO struct -> not serialisable, left out


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
    """Must match get_string_without_tracked_deletions exactly — a divergence would paint one
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


def test_copy_properties_skips_values_the_style_already_gives():
    """Copying an inherited value would make it a hand-set one on the copy, and the read would
    then report margin-right:0cm on every paragraph as if the user had set it."""
    from unittest.mock import MagicMock
    from plugin.writer import html_export as hx

    src, dst, style = MagicMock(), MagicMock(), MagicMock()
    src.getPropertyValue.side_effect = {"ParaLeftMargin": 3251, "ParaRightMargin": 0}.get
    style.getPropertyValue.side_effect = {"ParaLeftMargin": 0, "ParaRightMargin": 0}.get

    hx._copy_properties(src, dst, ("ParaLeftMargin", "ParaRightMargin"), style)

    dst.setPropertyValue.assert_called_once_with("ParaLeftMargin", 3251)


def test_copy_properties_without_a_style_copies_everything_readable():
    from unittest.mock import MagicMock
    from plugin.writer import html_export as hx

    src, dst = MagicMock(), MagicMock()
    src.getPropertyValue.side_effect = {"CharWeight": 150.0}.get

    hx._copy_properties(src, dst, ("CharWeight",))

    dst.setPropertyValue.assert_called_once_with("CharWeight", 150.0)


def test_copy_properties_continues_past_one_the_target_refuses():
    """The temp document is a plain Writer doc and need not offer every source property."""
    from unittest.mock import MagicMock
    from plugin.writer import html_export as hx

    src, dst = MagicMock(), MagicMock()
    src.getPropertyValue.side_effect = {"CharPosture": 1, "CharWeight": 150.0}.get

    def refuse_posture(name, _value):
        if name == "CharPosture":
            raise RuntimeError("unsupported")

    dst.setPropertyValue.side_effect = refuse_posture

    hx._copy_properties(src, dst, ("CharPosture", "CharWeight"))

    assert [c.args[0] for c in dst.setPropertyValue.call_args_list] == ["CharPosture", "CharWeight"]


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
