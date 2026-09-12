# WriterAgent - LaTeX Math Insertion Dialog UNO Tests
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""UNO integration tests for LaTeX Math Insertion Dialog in Writer."""

from __future__ import annotations

from plugin.tests.testing_utils import setup_uno_mocks
setup_uno_mocks()

from typing import Any
from unittest.mock import MagicMock, patch

from plugin.writer.math.latex_dialog import insert_latex_math_dialog
from plugin.framework.config import get_config
from plugin.writer.math.math_mml_convert import MATH_CLSID
from plugin.testing_runner import native_test
from plugin.tests.testing_utils import skip_windows_leftover_hidden_mathml, with_native_doc


def _skip_windows_leftover_hidden_mathml() -> None:
    """Skip leftover Hidden ``_blank`` MathML loads on Windows.

    GHA 34675151298 (master ``71640e30``, #744+#745): leftover
    ``simpress`` at leftover_open=3 correctly SKIPPED. Slash TOP
    dialog also SKIPPED. Then leftover writer reuse printed and
    ``convert_mathml_to_starmath`` ``loadComponentFromURL(...,
    "_blank", Hidden)`` hung 30s (office alive). XDL
    ``LatexInputDialog`` is patched — hang is leftover Hidden
    ``_blank``, not AWT TOP ``createPeer`` / ``setVisible``.
    ``skip_windows_awt_top_dialog`` is the wrong class.
    GHA 34678020608: this skip fired; next suite hung the same load.
    """
    skip_windows_leftover_hidden_mathml("latex dialog Hidden _blank .mml")


def _embed_count(doc: Any) -> int:
    eo = doc.getEmbeddedObjects()
    return len(eo.getElementNames())


def _first_math_formula(doc: Any) -> str:
    eo = doc.getEmbeddedObjects()
    names = eo.getElementNames()
    for n in names:
        obj = eo.getByName(n)
        try:
            if str(getattr(obj, "CLSID", "")).lower() == MATH_CLSID.lower():
                inner = obj.getEmbeddedObject()
                return str(inner.Formula)
        except Exception:
            continue
    return ""


@native_test
@with_native_doc("writer")
def test_insert_latex_math_dialog_success(ctx: Any, doc: Any) -> None:
    _skip_windows_leftover_hidden_mathml()
    mock_desktop = MagicMock()
    mock_desktop.getCurrentComponent.return_value = doc

    latex_input = r"a^2 + b^2 = c^2"
    with patch("plugin.writer.math.latex_dialog.get_desktop", return_value=mock_desktop), \
         patch("plugin.writer.math.latex_dialog.monaco_editor_available", return_value=(None, False)), \
         patch("plugin.writer.math.latex_dialog.show_latex_input_dialog", return_value=(latex_input, True)):
        # Call the dialog insertion entry point
        insert_latex_math_dialog(ctx)

        # Verify configuration was saved
        saved_latex = get_config("last_latex_input")
        saved_display = get_config("last_latex_display_block")

        assert saved_latex == latex_input
        assert saved_display is True

        # Verify a formula was embedded
        assert _embed_count(doc) >= 1
        formula = _first_math_formula(doc)
        assert formula != ""
        assert "a" in formula
        assert "b" in formula
        assert "c" in formula


@native_test
@with_native_doc("writer")
def test_insert_latex_math_dialog_cancelled(ctx: Any, doc: Any) -> None:
    mock_desktop = MagicMock()
    mock_desktop.getCurrentComponent.return_value = doc

    initial_count = _embed_count(doc)
    with patch("plugin.writer.math.latex_dialog.get_desktop", return_value=mock_desktop), \
         patch("plugin.writer.math.latex_dialog.monaco_editor_available", return_value=(None, False)), \
         patch("plugin.writer.math.latex_dialog.show_latex_input_dialog", return_value=None):
        insert_latex_math_dialog(ctx)
        # Verify no new formula was embedded
        assert _embed_count(doc) == initial_count


@native_test
@with_native_doc("writer")
def test_insert_latex_math_dialog_non_writer_fails(ctx: Any, doc: Any) -> None:
    mock_desktop = MagicMock()
    mock_desktop.getCurrentComponent.return_value = doc

    # Patch is_writer to return False, simulating a spreadsheet or drawing document
    with patch("plugin.writer.math.latex_dialog.get_desktop", return_value=mock_desktop), \
         patch("plugin.writer.math.latex_dialog.is_writer", return_value=False), \
         patch("plugin.writer.math.latex_dialog.msgbox") as mock_msgbox:

        insert_latex_math_dialog(ctx)

        # Verify an error msgbox was shown
        mock_msgbox.assert_called_once()
        args = mock_msgbox.call_args[0]
        assert "available in Writer" in args[2] or "Error" in args[1]


@native_test
@with_native_doc("writer")
def test_insert_latex_math_dialog_monaco_success(ctx: Any, doc: Any) -> None:
    _skip_windows_leftover_hidden_mathml()
    mock_desktop = MagicMock()
    mock_desktop.getCurrentComponent.return_value = doc

    mock_exe = "mock_python"
    latex_input = r"E = m c^2"

    with patch("plugin.writer.math.latex_dialog.get_desktop", return_value=mock_desktop), \
         patch("plugin.writer.math.latex_dialog.monaco_editor_available", return_value=(mock_exe, True)), \
         patch("plugin.writer.math.latex_dialog.launch_monaco_editor") as mock_launch, \
         patch("plugin.writer.math.latex_dialog.set_config") as mock_set_config:

        insert_latex_math_dialog(ctx)

        # Verify launch_monaco_editor was called with the correct options
        mock_launch.assert_called_once()
        kwargs = mock_launch.call_args[1]
        assert kwargs["exe"] == mock_exe

        load_msg = kwargs["load_message"]
        assert load_msg["mode"] == "latex"
        assert load_msg["language"] == "latex"
        assert "centered paragraph" in load_msg["plain_text_label"]
        assert load_msg["show_data_binding"] is False

        # Test the on_save callback defined in insert_latex_math_dialog
        on_save = kwargs["on_save"]

        # Call on_save manually to verify the conversion and insertion logic
        res = on_save(latex_input, True)
        assert res["type"] == "saved"
        assert res["ok"] is True
        assert res["status_ok_text"] == "Formula inserted."

        # Verify the on_save closure attempted to persist via set_config
        mock_set_config.assert_any_call("last_latex_input", latex_input)
        mock_set_config.assert_any_call("last_latex_display_block", True)


@native_test
@with_native_doc("writer")
def test_insert_latex_math_dialog_multi_select_refuses(ctx: Any, doc: Any) -> None:
    mock_desktop = MagicMock()
    mock_desktop.getCurrentComponent.return_value = doc
    initial_count = _embed_count(doc)
    with patch("plugin.writer.math.latex_dialog.get_desktop", return_value=mock_desktop), \
         patch("plugin.writer.math.math_mml_export.selected_math_embeds", return_value=[object(), object()]), \
         patch("plugin.writer.math.latex_dialog.msgbox") as mock_msgbox, \
         patch("plugin.writer.math.latex_dialog.show_latex_input_dialog") as mock_dlg, \
         patch("plugin.writer.math.latex_dialog.monaco_editor_available", return_value=(None, False)):
        insert_latex_math_dialog(ctx)
        mock_dlg.assert_not_called()
        mock_msgbox.assert_called_once()
        assert _embed_count(doc) == initial_count


@native_test
@with_native_doc("writer")
def test_replace_selected_formula_keeps_embed_count(ctx: Any, doc: Any) -> None:
    from plugin.writer.math.math_mml_convert import insert_writer_math_formula, replace_writer_math_formula
    from plugin.writer.math.math_mml_export import math_embed_name

    text = doc.getText()
    cur = text.createTextCursor()
    cur.gotoEnd(False)
    insert_writer_math_formula(doc, cur, "a + b", display_block=False)
    assert _embed_count(doc) == 1
    eo = doc.getEmbeddedObjects()
    name = eo.getElementNames()[0]
    embed = eo.getByName(name)
    replace_writer_math_formula(embed, "x + y")
    assert _embed_count(doc) == 1
    assert "x" in _first_math_formula(doc)
    assert math_embed_name(embed) in (name, str(name))


@native_test
@with_native_doc("writer")
def test_insert_latex_dialog_update_path_does_not_add_embed(ctx: Any, doc: Any) -> None:
    _skip_windows_leftover_hidden_mathml()
    from plugin.writer.math.math_mml_convert import insert_writer_math_formula

    text = doc.getText()
    cur = text.createTextCursor()
    cur.gotoEnd(False)
    insert_writer_math_formula(doc, cur, "a + b", display_block=False)
    eo = doc.getEmbeddedObjects()
    name = str(eo.getElementNames()[0])
    embed = eo.getByName(name)

    mock_desktop = MagicMock()
    mock_desktop.getCurrentComponent.return_value = doc
    latex_input = r"x + y"
    with patch("plugin.writer.math.latex_dialog.get_desktop", return_value=mock_desktop), \
         patch("plugin.writer.math.latex_dialog.monaco_editor_available", return_value=(None, False)), \
         patch("plugin.writer.math.math_mml_export.selected_math_embeds", return_value=[embed]), \
         patch("plugin.writer.math.latex_dialog.show_latex_input_dialog", return_value=(latex_input, False)):
        insert_latex_math_dialog(ctx)

    assert _embed_count(doc) == 1
    formula = _first_math_formula(doc)
    assert "x" in formula and "y" in formula

