# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

from unittest.mock import MagicMock, patch

from plugin.tests.testing_utils import setup_uno_mocks

setup_uno_mocks()

from plugin.writer.math.latex_dialog import show_latex_input_dialog  # noqa: E402


def test_show_latex_input_dialog_sets_update_label() -> None:
    dlg = MagicMock()
    btn = MagicMock()
    dlg.getControl.side_effect = lambda name: btn if name == "BtnInsert" else MagicMock()
    with patch("plugin.writer.math.latex_dialog.load_writeragent_dialog", return_value=dlg):
        show_latex_input_dialog(object(), update=True)
    assert btn.getModel().Label == "Update"


def test_show_latex_input_dialog_sets_insert_label() -> None:
    dlg = MagicMock()
    btn = MagicMock()
    dlg.getControl.side_effect = lambda name: btn if name == "BtnInsert" else MagicMock()
    with patch("plugin.writer.math.latex_dialog.load_writeragent_dialog", return_value=dlg):
        show_latex_input_dialog(object(), update=False)
    assert btn.getModel().Label == "Insert"


def test_latex_dialog_uno_skips_windows_leftover_hidden_mml() -> None:
    """GHA 34675151298: leftover writer reuse then Hidden _blank .mml hung 30s."""
    from pathlib import Path

    src = Path(__file__).with_name("test_latex_dialog_uno.py").read_text(encoding="utf-8")
    assert "skip_windows_leftover_hidden_mathml" in src
    assert "latex dialog Hidden _blank .mml" in src
    assert "34675151298" in src
    # AWT TOP is cited as the wrong class; leftover Hidden is the skip.
    assert 'skip_windows_leftover_hidden_mathml("latex dialog Hidden _blank .mml")' in src
    assert "is the wrong class" in src
