# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from plugin.writer.math.latex_dialog import show_latex_input_dialog  # noqa: E402


class _EmptyMessageError(Exception):
    Message = ""

    def __str__(self) -> str:
        return ""


@pytest.mark.parametrize(
    "update, expected",
    [
        pytest.param(True, "Update", id="test_show_latex_input_dialog_sets_update_label"),
        pytest.param(False, "Insert", id="test_show_latex_input_dialog_sets_insert_label"),
    ],
)
def test_show_latex_input_dialog_sets_update_label(update, expected) -> None:
    dlg = MagicMock()
    btn = MagicMock()
    dlg.getControl.side_effect = lambda name: btn if name == "BtnInsert" else MagicMock()
    with patch("plugin.writer.math.latex_dialog.load_writeragent_dialog", return_value=dlg):
        show_latex_input_dialog(object(), update=update)
    assert btn.getModel().Label == expected


def test_show_latex_input_dialog_failure_disposes_and_reports() -> None:
    dlg = MagicMock()
    dlg.getControl.side_effect = RuntimeError("boom")
    with patch("plugin.writer.math.latex_dialog.load_writeragent_dialog", return_value=dlg), \
         patch("plugin.writer.math.latex_dialog.msgbox") as mock_msgbox:
        res = show_latex_input_dialog(object())
    assert res is None
    dlg.dispose.assert_called_once()
    mock_msgbox.assert_called_once()


def test_show_latex_input_dialog_missing_dialog_reports() -> None:
    with patch("plugin.writer.math.latex_dialog.load_writeragent_dialog", return_value=None), \
         patch("plugin.writer.math.latex_dialog.msgbox") as mock_msgbox:
        res = show_latex_input_dialog(object())
    assert res is None
    mock_msgbox.assert_called_once()


def test_selection_latex_prefill_failed_export_returns_empty() -> None:
    from plugin.writer.math.latex_dialog import _selection_latex_prefill
    from plugin.writer.math.math_mml_export import MathExportResult

    embed = MagicMock()
    embed.getEmbeddedObject.return_value.Formula = "bad starmath"
    doc = MagicMock()
    ctx = MagicMock()

    with patch("plugin.writer.math.math_mml_export.selected_math_embeds", return_value=[embed]), \
         patch("plugin.writer.math.math_mml_export.resolve_math_embed_name", return_value="Object1"), \
         patch("plugin.writer.math.math_mml_export.convert_starmath_to_latex", return_value=MathExportResult(False, None, None, "err")), \
         patch("plugin.writer.math.latex_dialog.msgbox") as mock_msgbox:
        name, latex, display, abort = _selection_latex_prefill(ctx, doc, "last_latex_value", True)

    assert name == "Object1"
    assert latex == ""
    assert display is True
    assert abort is False
    mock_msgbox.assert_called_once()


def test_selection_latex_prefill_unreadable_formula_reports() -> None:
    from plugin.writer.math.latex_dialog import _selection_latex_prefill

    embed = MagicMock()
    embed.getEmbeddedObject.side_effect = RuntimeError("gone")
    embed.Formula = ""
    doc = MagicMock()
    ctx = MagicMock()

    with patch("plugin.writer.math.math_mml_export.selected_math_embeds", return_value=[embed]), \
         patch("plugin.writer.math.math_mml_export.resolve_math_embed_name", return_value="Object1"), \
         patch("plugin.writer.math.latex_dialog.msgbox") as mock_msgbox:
        name, latex, display, abort = _selection_latex_prefill(ctx, doc, "last_latex_value", True)

    assert name == "Object1"
    assert latex == ""
    assert display is True
    assert abort is False
    mock_msgbox.assert_called_once()


def test_apply_latex_replace_failure_reports_exception_type() -> None:
    from plugin.writer.math.latex_dialog import _apply_latex_to_writer
    from plugin.writer.math.math_mml_convert import MathConversionResult

    with patch(
        "plugin.writer.math.latex_dialog.convert_latex_to_starmath",
        return_value=MathConversionResult(True, "x", None),
    ), patch(
        "plugin.writer.math.math_mml_export.lookup_math_embed",
        return_value=object(),
    ), patch(
        "plugin.writer.math.latex_dialog.replace_writer_math_formula",
        side_effect=_EmptyMessageError(),
    ):
        ok, message = _apply_latex_to_writer(object(), MagicMock(), "x", False, "Object1")

    assert ok is False
    assert message == "_EmptyMessageError"


def test_latex_dialog_uno_skips_windows_leftover_hidden_mml() -> None:
    """GHA 34675151298: leftover writer reuse then Hidden _blank .mml hung 30s."""
    src = Path(__file__).with_name("test_latex_dialog_uno.py").read_text(encoding="utf-8")
    assert "skip_windows_leftover_hidden_load" in src
    assert "latex dialog Hidden _blank .mml" in src
    assert "34675151298" in src
    # AWT TOP is cited as the wrong class; leftover Hidden is the skip.
    assert 'skip_windows_leftover_hidden_load("latex dialog Hidden _blank .mml")' in src
    assert "is the wrong class" in src
