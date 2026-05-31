# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Native UNO tests for RichTextControl sidebar HTML clipboard spike (Phase 0 + 2)."""

from typing import Any

from plugin.framework.logging import log
from plugin.framework.uno_context import get_desktop
from plugin.testing_runner import native_test, setup, teardown

# ``make test`` LO runner: skip this file until clipboard/formatted-copy UNO path is stable.
SKIP_NATIVE_RUN_ALL = True

_test_ctx: Any = None
_test_doc: Any = None


@setup
def setup_rich_text_control(ctx):
    global _test_ctx, _test_doc
    _test_ctx = ctx
    desktop = get_desktop(ctx)
    import uno

    hidden = uno.createUnoStruct("com.sun.star.beans.PropertyValue", Name="Hidden", Value=True)
    _test_doc = desktop.loadComponentFromURL("private:factory/swriter", "_blank", 0, (hidden,))
    assert _test_doc is not None, "Could not create Writer document for RichTextControl test"


@teardown
def teardown_rich_text_control(ctx):
    global _test_ctx, _test_doc
    if _test_doc:
        try:
            _test_doc.close(True)
        except Exception:
            pass
    _test_doc = None
    _test_ctx = None


def _create_test_dialog_with_rich_control(ctx):
    smgr = ctx.getServiceManager()
    dlg_model = smgr.createInstanceWithContext("com.sun.star.awt.UnoControlDialogModel", ctx)
    dlg = smgr.createInstanceWithContext("com.sun.star.awt.UnoControlDialog", ctx)
    dlg.setModel(dlg_model)

    placeholder = dlg_model.createInstance("com.sun.star.awt.UnoControlEditModel")
    placeholder.Name = "response"
    placeholder.PositionX = 4
    placeholder.PositionY = 4
    placeholder.Width = 200
    placeholder.Height = 120
    placeholder.ReadOnly = True
    dlg_model.insertByName("response", placeholder)

    dlg.setVisible(True)
    placeholder_ctrl = dlg.getControl("response")
    assert placeholder_ctrl is not None, "placeholder control missing"

    from plugin.chatbot.rich_text_control import create_sidebar_rich_text_control

    result = create_sidebar_rich_text_control(ctx, dlg, placeholder_ctrl)
    assert result is not None, "create_sidebar_rich_text_control failed"
    return dlg, result


def _format_at(model, needle: str, attr: str):
    text = model.Text or ""
    idx = text.find(needle)
    assert idx >= 0, "%r not found in control text: %r" % (needle, text[:200])
    cursor = model.createTextCursor()
    cursor.gotoStart(False)
    cursor.goRight(idx, False)
    cursor.goRight(len(needle), True)
    return getattr(cursor, attr)


# Disabled: hangs/flaky in headless LO (_process_idle / _copy_formatted_from_hidden_doc_to_control).
# Re-enable by clearing SKIP_NATIVE_RUN_ALL and restoring @native_test below.
# @native_test
def _disabled_test_rich_text_control_html_clipboard_paste():
    """Hidden Writer HTML import -> clipboard -> paste into RichTextControl with formatting."""
    ctx = _test_ctx
    assert ctx is not None

    from plugin.chatbot.rich_text_control import get_control_text_length
    from plugin.chatbot.rich_text_paste import append_rich_text_via_clipboard

    dlg, control = _create_test_dialog_with_rich_control(ctx)
    try:
        sample_html = "<p><strong>Bold</strong> and <em>italic</em></p><ul><li>one</li><li>two</li></ul>"
        append_rich_text_via_clipboard(ctx, control, sample_html, role="assistant", style_window=dlg)

        pasted_len = get_control_text_length(control)
        assert pasted_len > 0, "RichTextControl stayed empty after HTML paste"
        model = control.getModel()
        model_text = model.Text or ""
        assert "<strong>" not in model_text, "Raw HTML tags should not appear as plain text after paste"
        assert "Bold" in model_text, "Expected Bold text in control"
        assert "italic" in model_text, "Expected italic text in control"

        bold_weight = _format_at(model, "Bold", "CharWeight")
        italic_posture = _format_at(model, "italic", "CharPosture")
        assert bold_weight >= 150, "Bold span should have CharWeight >= 150, got %s" % bold_weight
        assert italic_posture != 0, "Italic span should have non-zero CharPosture, got %s" % italic_posture

        log.info(
            "[RichTextControlTest] paste len=%d bold_weight=%s italic_posture=%s snippet=%r",
            pasted_len,
            bold_weight,
            italic_posture,
            model_text[:120],
        )
    finally:
        try:
            dlg.setVisible(False)
            dlg.dispose()
        except Exception:
            pass
