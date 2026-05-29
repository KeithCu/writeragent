# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Unit tests for plugin.chatbot.rich_text_control."""

from unittest.mock import MagicMock, patch

from plugin.tests.testing_utils import setup_uno_mocks

setup_uno_mocks()

from plugin.chatbot.rich_text_control import (
    _set_model_property,
    append_rich_text_via_clipboard,
    append_text_chunk,
    build_message_html,
    clear_control,
    insert_transferable_into_rich_control,
    scroll_rich_control_to_bottom,
    truncate_control_from,
)


class TestBuildMessageHtml:
    def test_plain_text_gets_role_prefix_and_escape(self):
        html = build_message_html("hello & world", role="user")
        assert "<strong>You:</strong>" in html
        assert "hello &amp; world" in html

    def test_html_body_passthrough(self):
        body = "<p><strong>Hi</strong></p>"
        html = build_message_html(body, role="assistant")
        assert "<strong>Assistant:</strong>" in html
        assert body in html

    def test_empty_returns_empty(self):
        assert build_message_html("", role="assistant") == ""
        assert build_message_html("   ", role="assistant") == ""


class TestRichControlHelpers:
    def test_content_bounds_inset(self):
        from types import SimpleNamespace

        from plugin.chatbot.rich_text_control import RICH_CONTROL_EDGE_INSET, _content_bounds_for_rich_control

        ps = MagicMock()
        ps.getPosSize.return_value = SimpleNamespace(X=10, Y=20, Width=100, Height=200)
        root = MagicMock()
        root.getPosSize.return_value = SimpleNamespace(Width=180, Height=400)
        root.getControl.return_value = None
        bx, by, bw, bh = _content_bounds_for_rich_control(root, ps)
        assert bx == 10 + RICH_CONTROL_EDGE_INSET
        assert by == 20 + RICH_CONTROL_EDGE_INSET
        assert bw == 100 - 2 * RICH_CONTROL_EDGE_INSET
        assert bh == 200 - 2 * RICH_CONTROL_EDGE_INSET

    def test_content_bounds_clamp_to_clear_button(self):
        from types import SimpleNamespace

        from plugin.chatbot.rich_text_control import RICH_CONTROL_EDGE_INSET, _content_bounds_for_rich_control

        ps = MagicMock()
        ps.getPosSize.return_value = SimpleNamespace(X=4, Y=16, Width=900, Height=110)
        root = MagicMock()
        root.getPosSize.return_value = SimpleNamespace(Width=180, Height=400)
        clear = MagicMock()
        clear.getPosSize.return_value = SimpleNamespace(X=108, Y=186, Width=50, Height=15)
        root.getControl.return_value = clear
        bx, by, bw, _bh = _content_bounds_for_rich_control(root, ps)
        clear_right = 108 + 50
        assert bx + bw <= clear_right
        assert bw < 900 - 2 * RICH_CONTROL_EDGE_INSET

    def test_sidebar_content_right_edge_matches_rich_bounds(self):
        from types import SimpleNamespace

        from plugin.chatbot.rich_text_control import _content_bounds_for_rich_control, sidebar_content_right_edge

        ps = MagicMock()
        ps.getPosSize.return_value = SimpleNamespace(X=4, Y=16, Width=900, Height=110)
        root = MagicMock()
        root.getPosSize.return_value = SimpleNamespace(Width=180, Height=400)
        clear = MagicMock()
        clear.getPosSize.return_value = SimpleNamespace(X=108, Y=186, Width=50, Height=15)
        root.getControl.return_value = clear
        bx, _by, bw, _bh = _content_bounds_for_rich_control(root, ps)
        assert bx + bw == sidebar_content_right_edge(root, ps)

    def test_rich_control_model_gets_theme_background(self):
        from plugin.chatbot.rich_text_control import _apply_rich_control_style_defaults_on_model

        model = MagicMock()
        style_window = MagicMock()
        with patch("plugin.chatbot.rich_text.get_theme_colors", return_value=(0xD8D9DA, 0x2A6099, 0x1E293B)):
            _apply_rich_control_style_defaults_on_model(model, style_window=style_window)
        assert model.CharColor == 0x1E293B
        assert model.CharFontName == "Liberation Sans"

    def test_set_model_property_swallows_unknown(self):
        from plugin.chatbot.rich_text_control import _set_model_property

        class _Model:
            __slots__ = ()

            def setPropertyValue(self, name, value):
                raise RuntimeError("UnknownPropertyException")

        assert _set_model_property(_Model(), "BackgroundColor", 0xFFFFFF) is False

    def test_set_model_property_on_plain_object(self):
        class _Model:
            pass

        model = _Model()
        _set_model_property(model, "PositionX", 42)
        assert model.PositionX == 42

    def test_set_model_property_falls_back_to_setPropertyValue(self):
        class _Model:
            def setPropertyValue(self, name, value):
                self.last = (name, value)

            def __setattr__(self, name, value):
                if name not in ("last",):
                    raise AttributeError(name)
                object.__setattr__(self, name, value)

        model = _Model()
        _set_model_property(model, "PositionX", 42)
        assert model.last == ("PositionX", 42)

    def test_ensure_trailing_line_break(self):
        from plugin.chatbot.rich_text_control import _ensure_trailing_line_break

        control = MagicMock()
        model = MagicMock(Text="You: hello")
        cursor = MagicMock()
        model.createTextCursor.return_value = cursor
        control.getModel.return_value = model

        with patch("plugin.chatbot.rich_text_control._insert_string_at_rich_cursor") as mock_insert:
            _ensure_trailing_line_break(control)
            mock_insert.assert_called_once_with(model, cursor, "\n\n")

        model.Text = "already\n"
        with patch("plugin.chatbot.rich_text_control._insert_string_at_rich_cursor") as mock_insert:
            _ensure_trailing_line_break(control)
            mock_insert.assert_called_once_with(model, cursor, "\n")

        model.Text = "already\n\n"
        with patch("plugin.chatbot.rich_text_control._insert_string_at_rich_cursor") as mock_insert:
            _ensure_trailing_line_break(control)
            mock_insert.assert_not_called()

    def test_append_text_chunk_uses_cursor_insert(self):
        control = MagicMock()
        model = MagicMock()
        cursor = MagicMock()
        model.createTextCursor.return_value = cursor
        control.getModel.return_value = model
        style_window = MagicMock()

        with patch("plugin.chatbot.rich_text.get_theme_colors", return_value=(0, 0, 0x1E293B)), \
             patch("plugin.chatbot.rich_text_control._insert_string_at_rich_cursor") as mock_insert, \
             patch("plugin.chatbot.rich_text_control.scroll_rich_control_to_bottom"), \
             patch("plugin.chatbot.rich_text_control._preserve_focus_window", side_effect=lambda _ctx, fn: fn()):
            append_text_chunk(control, " tail", auto_scroll=False, style_window=style_window)

        cursor.gotoEnd.assert_called_once()
        assert cursor.CharColor == 0x1E293B
        mock_insert.assert_called_once_with(model, cursor, " tail")

    def test_scroll_rich_control_to_bottom_does_not_focus_control(self):
        control = MagicMock()
        model = MagicMock(Text="hello")
        cursor = MagicMock()
        model.createTextCursor.return_value = cursor
        control.getModel.return_value = model

        with patch("plugin.chatbot.rich_text_control._scroll_rich_peer_vertical_bar"), \
             patch("plugin.chatbot.rich_text_control._process_idle"):
            scroll_rich_control_to_bottom(control, ctx=MagicMock(), aggressive=True)

        control.setFocus.assert_not_called()
        control.setSelection.assert_not_called()
        cursor.gotoEnd.assert_called_once_with(False)

    def test_clear_control(self):
        control = MagicMock()
        model = MagicMock(Text="old")
        control.getModel.return_value = model
        clear_control(control)
        assert model.Text == ""

    def test_truncate_control_from(self):
        control = MagicMock()
        model = MagicMock(Text="hello world")
        cursor = MagicMock()
        model.createTextCursor.return_value = cursor
        control.getModel.return_value = model
        truncate_control_from(control, 5)
        cursor.goRight.assert_called_once_with(5, False)
        cursor.gotoEnd.assert_called_once_with(True)
        cursor.setString.assert_called_once_with("")
        assert model.Text == "hello world"


class TestAppendRichTextViaClipboard:
    def test_pipeline_order(self):
        control = MagicMock()
        control.getModel.return_value = MagicMock(Text="")
        ctx = MagicMock()
        doc = MagicMock()
        style_window = MagicMock()

        with patch("plugin.chatbot.rich_text_control.create_hidden_html_writer", return_value=doc) as mock_create, \
             patch("plugin.chatbot.rich_text_control._configure_hidden_writer_for_chat") as mock_cfg, \
             patch("plugin.chatbot.rich_text_control.append_rich_text") as mock_append, \
             patch("plugin.chatbot.rich_text_control._copy_formatted_from_hidden_doc_to_control", return_value=True) as mock_copy:
            append_rich_text_via_clipboard(ctx, control, "<p>Hi</p>", role="assistant", style_window=style_window)

        mock_create.assert_called_once_with(ctx)
        mock_cfg.assert_called_once_with(doc)
        mock_append.assert_called_once_with(doc, "<p>Hi</p>", role="assistant", auto_scroll=False, style_window=style_window)
        mock_copy.assert_called_once()
        doc.close.assert_called_once_with(True)

    def test_skips_when_transferable_missing(self):
        control = MagicMock()
        control.getModel.return_value = MagicMock(Text="")
        doc = MagicMock()

        with patch("plugin.chatbot.rich_text_control.create_hidden_html_writer", return_value=doc), \
             patch("plugin.chatbot.rich_text_control._configure_hidden_writer_for_chat"), \
             patch("plugin.chatbot.rich_text_control.append_rich_text"), \
             patch("plugin.chatbot.rich_text_control._copy_formatted_from_hidden_doc_to_control", return_value=False), \
             patch("plugin.chatbot.rich_text_control._transferable_from_hidden_doc", return_value=None), \
             patch("plugin.chatbot.rich_text_control.insert_transferable_into_rich_control") as mock_insert_tf:
            append_rich_text_via_clipboard(MagicMock(), control, "hi", role="user")

        mock_insert_tf.assert_not_called()
        doc.close.assert_called_once_with(True)


class TestStripLegacyAiLabel:
    def test_strips_ai_prefix(self):
        from plugin.chatbot.rich_text import strip_legacy_ai_label

        assert strip_legacy_ai_label("AI: Hello") == "Hello"
        assert strip_legacy_ai_label("  ai:  Hello") == "Hello"

    def test_leaves_user_text(self):
        from plugin.chatbot.rich_text import strip_legacy_ai_label

        assert strip_legacy_ai_label("You: hi") == "You: hi"


class TestListPrefix:
    def test_bullet_list_gets_bullet_prefix(self):
        from plugin.chatbot.rich_text_control import _list_prefix_for_paragraph

        para = MagicMock()
        para.getPropertyValue.side_effect = lambda name: {
            "NumberingIsNumber": True,
            "NumberingLevel": 0,
            "ListId": "L1",
            "NumberingRules": MagicMock(),
        }[name]
        rule_prop = MagicMock()
        rule_prop.Name = "BulletChar"
        rule_prop.Value = "\u2022"
        para.getPropertyValue("NumberingRules").getByIndex.return_value = [rule_prop]

        assert _list_prefix_for_paragraph(para, {}) == "\u2022 "

    def test_ordered_list_gets_number_prefix(self):
        from plugin.chatbot.rich_text_control import _list_prefix_for_paragraph

        para = MagicMock()
        para.getPropertyValue.side_effect = lambda name: {
            "NumberingIsNumber": True,
            "NumberingLevel": 0,
            "ListId": "L1",
            "NumberingType": 4,
            "NumberingRules": MagicMock(),
        }[name]
        rule_prop = MagicMock()
        rule_prop.Name = "NumberingType"
        rule_prop.Value = 4
        para.getPropertyValue("NumberingRules").getByIndex.return_value = [rule_prop]

        counters: dict = {}
        assert _list_prefix_for_paragraph(para, counters) == "1. "
        assert _list_prefix_for_paragraph(para, counters) == "2. "


class TestSkipLegacyStreamChunk:
    def test_skips_ai_prefix(self):
        from plugin.chatbot.rich_text_control import skip_legacy_assistant_stream_chunk

        assert skip_legacy_assistant_stream_chunk("\nAI: ")
        assert skip_legacy_assistant_stream_chunk("AI:")
        assert skip_legacy_assistant_stream_chunk("\n[Using chat model.]\n")

    def test_allows_real_content(self):
        from plugin.chatbot.rich_text_control import skip_legacy_assistant_stream_chunk

        assert not skip_legacy_assistant_stream_chunk("Here is a real answer with tools.")


class TestInsertTransferableIntoRichControl:
    def test_does_not_use_plain_text_fallback(self):
        control = MagicMock()
        model = MagicMock(Text="before")
        control.getModel.return_value = model
        ctx = MagicMock()

        with patch("plugin.chatbot.rich_text_control._try_insert_transferable_on_target", return_value=False), \
             patch("plugin.chatbot.rich_text_control.get_control_text_length", return_value=6), \
             patch("plugin.chatbot.rich_text_control._set_system_clipboard", return_value=False), \
             patch("plugin.chatbot.rich_text_control._preserve_focus_window", side_effect=lambda _ctx, fn: fn()):
            ok = insert_transferable_into_rich_control(control, MagicMock(), ctx)

        assert ok is False
        assert model.Text == "before"

    def test_clipboard_key_paste_success(self):
        control = MagicMock()
        model = MagicMock(Text="")
        control.getModel.return_value = model
        ctx = MagicMock()
        transferable = MagicMock()

        with patch("plugin.chatbot.rich_text_control._try_insert_transferable_on_target", return_value=False), \
             patch("plugin.chatbot.rich_text_control.get_control_text_length", side_effect=[0, 12]), \
             patch("plugin.chatbot.rich_text_control._set_system_clipboard", return_value=True) as mock_clip, \
             patch("plugin.chatbot.rich_text_control._try_paste_via_key_event", return_value=True) as mock_key, \
             patch("plugin.chatbot.rich_text_control._preserve_focus_window", side_effect=lambda _ctx, fn: fn()):
            ok = insert_transferable_into_rich_control(control, transferable, ctx)

        assert ok is True
        mock_clip.assert_called_once_with(ctx, transferable)
        mock_key.assert_called_once()
        control.setFocus.assert_not_called()
