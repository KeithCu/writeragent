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

from plugin.framework.uno_context import get_desktop
from plugin.writer.math.latex_dialog import insert_latex_math_dialog
from plugin.framework.config import get_config
from plugin.writer.math.math_mml_convert import MATH_CLSID
from plugin.testing_runner import native_test, setup, teardown

_test_doc: Any = None
_test_ctx: Any = None
_desktop_patcher: Any = None


@setup
def setup_latex_dialog_tests(ctx: Any) -> None:
    global _test_doc, _test_ctx, _desktop_patcher
    _test_ctx = ctx
    import uno

    hidden_prop = uno.createUnoStruct(
        "com.sun.star.beans.PropertyValue",
        Name="Hidden",
        Value=True,
    )
    desktop = get_desktop(ctx)
    _test_doc = desktop.loadComponentFromURL(
        "private:factory/swriter", "_blank", 0, (hidden_prop,)
    )
    assert _test_doc is not None

    mock_desktop = MagicMock()
    mock_desktop.getCurrentComponent.return_value = _test_doc
    _desktop_patcher = patch(
        "plugin.writer.math.latex_dialog.get_desktop", return_value=mock_desktop
    )
    _desktop_patcher.start()


@teardown
def teardown_latex_dialog_tests(ctx: Any) -> None:
    global _test_doc, _test_ctx, _desktop_patcher
    if _desktop_patcher:
        _desktop_patcher.stop()
        _desktop_patcher = None
    if _test_doc:
        _test_doc.close(True)
    _test_doc = None
    _test_ctx = None


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
def test_insert_latex_math_dialog_success() -> None:
    assert _test_doc is not None and _test_ctx is not None

    # We patch show_latex_input_dialog to return a valid LaTeX string and True for display_block
    latex_input = r"a^2 + b^2 = c^2"
    with patch("plugin.writer.math.latex_dialog.monaco_editor_available", return_value=(None, False)), \
         patch("plugin.writer.math.latex_dialog.show_latex_input_dialog", return_value=(latex_input, True)):
        # Call the dialog insertion entry point
        insert_latex_math_dialog(_test_ctx)

        # Verify configuration was saved
        saved_latex = get_config("last_latex_input")
        saved_display = get_config("last_latex_display_block")

        assert saved_latex == latex_input
        assert saved_display is True

        # Verify a formula was embedded
        assert _embed_count(_test_doc) >= 1
        formula = _first_math_formula(_test_doc)
        assert formula != ""
        assert "a" in formula
        assert "b" in formula
        assert "c" in formula


@native_test
def test_insert_latex_math_dialog_cancelled() -> None:
    assert _test_doc is not None and _test_ctx is not None

    initial_count = _embed_count(_test_doc)
    with patch("plugin.writer.math.latex_dialog.monaco_editor_available", return_value=(None, False)), \
         patch("plugin.writer.math.latex_dialog.show_latex_input_dialog", return_value=None):
        insert_latex_math_dialog(_test_ctx)
        # Verify no new formula was embedded
        assert _embed_count(_test_doc) == initial_count


@native_test
def test_insert_latex_math_dialog_non_writer_fails() -> None:
    assert _test_ctx is not None

    # Patch is_writer to return False, simulating a spreadsheet or drawing document
    with patch("plugin.writer.math.latex_dialog.is_writer", return_value=False), \
         patch("plugin.writer.math.latex_dialog.msgbox") as mock_msgbox:

        insert_latex_math_dialog(_test_ctx)

        # Verify an error msgbox was shown
        mock_msgbox.assert_called_once()
        args = mock_msgbox.call_args[0]
        assert "available in Writer" in args[2] or "Error" in args[1]


@native_test
def test_insert_latex_math_dialog_monaco_success() -> None:
    assert _test_doc is not None and _test_ctx is not None

    mock_exe = "mock_python"
    latex_input = r"E = m c^2"

    with patch("plugin.writer.math.latex_dialog.monaco_editor_available", return_value=(mock_exe, True)), \
         patch("plugin.writer.math.latex_dialog.launch_monaco_editor") as mock_launch, \
         patch("plugin.writer.math.latex_dialog.set_config") as mock_set_config:

        insert_latex_math_dialog(_test_ctx)

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

        # Verify the on_save closure attempted to persist via set_config (mocked to prevent
        # writing the test example "E = m c^2" into the real user profile's writeragent.json).
        mock_set_config.assert_any_call("last_latex_input", latex_input)
        mock_set_config.assert_any_call("last_latex_display_block", True)

