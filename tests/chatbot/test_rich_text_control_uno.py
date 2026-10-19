# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Native UNO tests for RichTextControl sidebar HTML clipboard spike (Phase 0 + 2)."""


from plugin.testing_runner import native_test

SKIP_NATIVE_RUN_ALL = False


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


@native_test
def test_rich_text_control_append_formatted_row(ctx):
    """Direct hidden-Writer copy into RichTextControl without clipboard or idle pumping."""
    assert ctx is not None

    from plugin.chatbot.rich_text import ChatTheme
    from plugin.chatbot.rich_text_control import get_control_text_length
    from plugin.chatbot.rich_text_paste import append_formatted_row

    dlg, control = _create_test_dialog_with_rich_control(ctx)
    try:
        append_formatted_row(ctx, control, "User question", role="user", style_window=dlg, auto_scroll=False)
        sample_html = "<p><strong>Bold</strong> and <em>italic</em></p><ul><li>one</li></ul>"
        append_formatted_row(ctx, control, sample_html, role="assistant", style_window=dlg, auto_scroll=False)

        pasted_len = get_control_text_length(control)
        assert pasted_len is not None and pasted_len > 0, "RichTextControl stayed empty after insert"
        model = control.getModel()
        model_text = model.Text or ""
        assert "<strong>" not in model_text, "Raw HTML tags should not appear as plain text after insert"
        assert "User question" in model_text
        assert "Bold" in model_text
        assert "italic" in model_text
        assert "\n\n" in model_text, "Expected blank line between rows"

        you_weight = _format_at(model, "You:", "CharWeight")
        bold_weight = _format_at(model, "Bold", "CharWeight")
        italic_posture = _format_at(model, "italic", "CharPosture")
        user_color = _format_at(model, "User question", "CharColor")
        theme = ChatTheme.resolve(style_window=dlg)

        assert you_weight >= 150, "You: prefix should have CharWeight >= 150, got %s" % you_weight
        assert bold_weight >= 150, "Bold span should have CharWeight >= 150, got %s" % bold_weight
        assert italic_posture != 0, "Italic span should have non-zero CharPosture, got %s" % italic_posture
        assert user_color == theme.user_color, "User question should match theme user color, got %s" % user_color
    finally:
        try:
            dlg.setVisible(False)
            dlg.dispose()
        except Exception:
            pass
