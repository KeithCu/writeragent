# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from plugin.writer.math.latex_dialog import show_latex_input_dialog  # noqa: E402


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


def test_selection_latex_prefill_failed_export_returns_empty() -> None:
    from plugin.writer.math.latex_dialog import _selection_latex_prefill
    from plugin.writer.math.math_mml_export import MathExportResult

    embed = MagicMock()
    embed.getEmbeddedObject.return_value.Formula = "bad starmath"
    doc = MagicMock()
    ctx = MagicMock()

    with patch("plugin.writer.math.latex_dialog.selected_math_embeds", return_value=[embed]), \
         patch("plugin.writer.math.latex_dialog.resolve_math_embed_name", return_value="Object1"), \
         patch("plugin.writer.math.latex_dialog.convert_starmath_to_latex", return_value=MathExportResult(False, None, None, "err")), \
         patch("plugin.writer.math.latex_dialog.msgbox") as mock_msgbox:
        name, latex, display, abort = _selection_latex_prefill(ctx, doc, "last_latex_value", True)

    assert name == "Object1"
    assert latex == ""
    assert display is True
    assert abort is False
    mock_msgbox.assert_called_once()


def test_latex_dialog_uno_skips_windows_leftover_hidden_mml() -> None:
    """GHA 34675151298: leftover writer reuse then Hidden _blank .mml hung 30s."""
    src = Path(__file__).with_name("test_latex_dialog_uno.py").read_text(encoding="utf-8")
    assert "skip_windows_leftover_hidden_load" in src
    assert "latex dialog Hidden _blank .mml" in src
    assert "34675151298" in src
    # AWT TOP is cited as the wrong class; leftover Hidden is the skip.
    assert 'skip_windows_leftover_hidden_load("latex dialog Hidden _blank .mml")' in src
    assert "is the wrong class" in src
