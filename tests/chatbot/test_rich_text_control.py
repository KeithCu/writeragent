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
    HISTORY_RENDER_BATCH_CHARS,
    _is_automatic_char_color,
    _nudge_rich_view_to_end_inner,
    _resolve_portion_char_color,
    _set_model_property,
    append_rich_messages_via_clipboard,
    append_rich_text_via_clipboard,
    append_text_chunk,
    build_message_html,
    clear_control,
    insert_transferable_into_rich_control,
    iter_history_message_batches,
    nudge_rich_control_view_to_end,
    session_history_items,
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
    def test_automatic_char_color(self):
        assert _is_automatic_char_color(None)
        assert _is_automatic_char_color(-1)
        assert _is_automatic_char_color(0xFFFFFFFF)
        assert not _is_automatic_char_color(0x2A6099)

    def test_resolve_portion_char_color_uses_role_and_prefix(self):
        portion = MagicMock(CharColor=-1)
        user = 0x2A6099
        assistant = 0x1E293B
        assert _resolve_portion_char_color(portion, "You: hi", user, assistant, "user") == user
        assert _resolve_portion_char_color(portion, "Assistant: hi", user, assistant, "assistant") == assistant
        assert _resolve_portion_char_color(portion, "body", user, assistant, "user") == user
        portion.CharColor = 0xFF0000
        assert _resolve_portion_char_color(portion, "body", user, assistant, "user") == 0xFF0000

    def test_copy_path_preserves_explicit_portion_color(self):
        from plugin.chatbot.rich_text_control import _resolve_portion_char_color

        red = 0xFF0000
        blue = 0x0000FF
        user = 0x2A6099
        assistant = 0x1E293B
        red_portion = MagicMock(CharColor=red)
        auto_portion = MagicMock(CharColor=-1)
        assert _resolve_portion_char_color(red_portion, "alert", user, assistant, "assistant") == red
        assert _resolve_portion_char_color(auto_portion, "plain", user, assistant, "assistant") == assistant
        assert _resolve_portion_char_color(auto_portion, "You: hi", user, assistant, "user") == user
        assert _resolve_portion_char_color(MagicMock(CharColor=blue), "x", user, assistant, "assistant") == blue

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

    def test_rich_control_model_gets_chat_typography(self):
        from plugin.chatbot.rich_text_control import _apply_rich_control_style_defaults_on_model

        class _Model:
            def __init__(self):
                self.props = {}

            def __setattr__(self, name, value):
                if name == "props":
                    object.__setattr__(self, name, value)
                else:
                    self.props[name] = value

        model = _Model()
        with patch("plugin.chatbot.rich_text.get_theme_colors", return_value=(0xD8D9DA, 0x2A6099, 0x1E293B)):
            _apply_rich_control_style_defaults_on_model(model, style_window=MagicMock())
        assert model.props.get("CharFontName") == "Liberation Sans"
        assert model.props.get("BackgroundColor") == 0xD8D9DA
        assert model.props.get("CharBackColor") == 0xD8D9DA
        assert "CharColor" not in model.props
        assert "TextColor" not in model.props

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
             patch("plugin.chatbot.rich_text_control._nudge_rich_view_to_end_inner"), \
             patch("plugin.chatbot.rich_text_control._preserve_focus_window", side_effect=lambda _ctx, fn: fn()):
            append_text_chunk(control, " tail", auto_scroll=False, style_window=style_window)

        cursor.gotoEnd.assert_called_once()
        mock_insert.assert_called_once_with(model, cursor, " tail", 0x1E293B)

    def test_nudge_rich_control_view_to_end_does_not_focus_control(self):
        control = MagicMock()
        model = MagicMock(Text="hello")
        cursor = MagicMock()
        tail = MagicMock()
        model.createTextCursor.side_effect = [cursor, tail]
        control.getModel.return_value = model

        with patch("plugin.chatbot.rich_text_control._insert_string_at_rich_cursor") as mock_insert, \
             patch("plugin.chatbot.rich_text_control._process_idle") as mock_idle, \
             patch("plugin.chatbot.rich_text_control._preserve_focus_window", side_effect=lambda _ctx, fn: fn()):
            nudge_rich_control_view_to_end(control, ctx=MagicMock())

        control.setFocus.assert_not_called()
        control.setSelection.assert_not_called()
        cursor.gotoEnd.assert_called_once_with(False)
        mock_insert.assert_called_once()
        tail.goLeft.assert_called_once()
        tail.setString.assert_called_once_with("")
        assert mock_idle.call_count == 3

    def test_nudge_inner_runs_idle_rounds(self):
        control = MagicMock()
        model = MagicMock(Text="x" * 2000)
        cursor = MagicMock()
        tail = MagicMock()
        model.createTextCursor.side_effect = [cursor, tail]
        control.getModel.return_value = model

        with patch("plugin.chatbot.rich_text_control._insert_string_at_rich_cursor"), \
             patch("plugin.chatbot.rich_text_control._process_idle") as mock_idle:
            _nudge_rich_view_to_end_inner(control, ctx=MagicMock())

        assert mock_idle.call_count == 3

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

    def test_user_insert_invokes_on_after_insert(self):
        control = MagicMock()
        control.getModel.return_value = MagicMock(Text="")
        ctx = MagicMock()
        doc = MagicMock()
        seen = []

        with patch("plugin.chatbot.rich_text_control.create_hidden_html_writer", return_value=doc), \
             patch("plugin.chatbot.rich_text_control._configure_hidden_writer_for_chat"), \
             patch("plugin.chatbot.rich_text_control.append_rich_text"), \
             patch("plugin.chatbot.rich_text_control._copy_formatted_from_hidden_doc_to_control", return_value=True), \
             patch("plugin.chatbot.rich_text_control.get_control_text_length", return_value=42), \
             patch("plugin.chatbot.rich_text_control._ensure_trailing_line_break"):
            append_rich_text_via_clipboard(
                ctx,
                control,
                "hello",
                role="user",
                on_after_insert=seen.append,
            )

        assert seen == [42]
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


class TestHistoryMessageBatching:
    def test_iter_batches_empty(self):
        assert list(iter_history_message_batches([])) == []

    def test_iter_batches_single_message(self):
        items = [("user", "hello")]
        assert list(iter_history_message_batches(items)) == [items]

    def test_iter_batches_multiple_small_messages_one_batch(self):
        items = [("user", "a"), ("assistant", "b"), ("user", "c")]
        assert list(iter_history_message_batches(items, batch_chars=100)) == [items]

    def test_iter_batches_splits_at_limit_without_splitting_message(self):
        chunk = "x" * 10000
        items = [("user", chunk), ("assistant", chunk)]
        batches = list(iter_history_message_batches(items, batch_chars=HISTORY_RENDER_BATCH_CHARS))
        assert len(batches) == 2
        assert batches[0] == [("user", chunk)]
        assert batches[1] == [("assistant", chunk)]

    def test_iter_batches_oversized_message_is_own_batch(self):
        big = "x" * (HISTORY_RENDER_BATCH_CHARS + 1)
        items = [("assistant", big), ("user", "hi")]
        batches = list(iter_history_message_batches(items, batch_chars=HISTORY_RENDER_BATCH_CHARS))
        assert batches[0] == [("assistant", big)]
        assert batches[1] == [("user", "hi")]

    def test_session_history_items_skips_system_and_tool_only(self):
        session = MagicMock()
        session.messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "question"},
            {"role": "assistant", "content": "answer"},
            {"role": "assistant", "tool_calls": [{"id": "1"}]},
        ]
        assert session_history_items(session, greeting="Hi") == [
            ("assistant", "Hi"),
            ("user", "question"),
            ("assistant", "answer"),
            ("assistant", "[Thinking...]"),
        ]

    def test_append_rich_messages_single_batch(self):
        control = MagicMock()
        control.getModel.return_value = MagicMock(Text="")
        ctx = MagicMock()
        doc = MagicMock()
        items = [("user", f"msg{i}") for i in range(10)]

        with patch("plugin.chatbot.rich_text_control.create_hidden_html_writer", return_value=doc) as mock_create, \
             patch("plugin.chatbot.rich_text_control._configure_hidden_writer_for_chat") as mock_cfg, \
             patch("plugin.chatbot.rich_text_control.append_rich_text") as mock_append, \
             patch("plugin.chatbot.rich_text_control._append_hidden_doc_to_control", return_value=True) as mock_copy, \
             patch("plugin.chatbot.rich_text_control.nudge_rich_control_view_to_end") as mock_nudge:
            append_rich_messages_via_clipboard(ctx, control, items)

        mock_create.assert_called_once_with(ctx)
        mock_cfg.assert_called_once_with(doc)
        assert mock_append.call_count == 10
        mock_copy.assert_called_once()
        mock_nudge.assert_called_once_with(control, ctx=ctx, style_window=None)
        doc.close.assert_called_once_with(True)

    def test_append_rich_messages_nudges_after_each_batch(self):
        control = MagicMock()
        control.getModel.return_value = MagicMock(Text="")
        ctx = MagicMock()
        doc = MagicMock()
        chunk = "x" * 10000
        items = [("user", chunk), ("assistant", chunk)]

        with patch("plugin.chatbot.rich_text_control.create_hidden_html_writer", return_value=doc) as mock_create, \
             patch("plugin.chatbot.rich_text_control._configure_hidden_writer_for_chat"), \
             patch("plugin.chatbot.rich_text_control.append_rich_text") as mock_append, \
             patch("plugin.chatbot.rich_text_control._append_hidden_doc_to_control", return_value=True) as mock_copy, \
             patch("plugin.chatbot.rich_text_control.nudge_rich_control_view_to_end") as mock_nudge:
            append_rich_messages_via_clipboard(ctx, control, items, batch_chars=HISTORY_RENDER_BATCH_CHARS)

        assert mock_create.call_count == 2
        assert mock_append.call_count == 2
        assert mock_copy.call_count == 2
        assert mock_nudge.call_count == 2
        assert doc.close.call_count == 2


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


class TestRichControlListenerResize:
    def test_resize_syncs_bounds_without_scroll(self):
        from plugin.chatbot.rich_text_control import RichTextControlListener

        listener = RichTextControlListener(MagicMock(), MagicMock(), MagicMock(), MagicMock())
        listener.rich_control = MagicMock()
        listener.placeholder_ctrl = MagicMock()

        with patch("plugin.chatbot.rich_text_control.sync_rich_control_bounds") as mock_sync, \
             patch("plugin.framework.queue_executor.post_to_main_thread") as mock_post:
            listener.on_window_resized(MagicMock())

        mock_sync.assert_called_once()
        mock_post.assert_not_called()


class TestRerenderRichControlScroll:
    def test_rerender_nudges_after_truncate_before_html_paste(self):
        from plugin.chatbot.panel import SendButtonListener

        with patch.object(SendButtonListener, "__init__", lambda self, *a, **k: None):
            send = SendButtonListener.__new__(SendButtonListener)
            send.rich_text_control = MagicMock()
            send.ctx = MagicMock()
            send._rich_control_style_window = None
            send._assistant_stream_start_len = 100
            send.session = MagicMock()
            send.session.messages = [{"role": "assistant", "content": "<p>Hi</p>"}]

            call_order = []

            with patch(
                "plugin.chatbot.rich_text_control.truncate_control_from",
                side_effect=lambda *a, **k: call_order.append("truncate"),
            ), patch(
                "plugin.chatbot.rich_text_control.nudge_rich_control_view_to_end",
                side_effect=lambda *a, **k: call_order.append("nudge"),
            ), patch(
                "plugin.chatbot.rich_text_control.append_rich_text_via_clipboard",
                side_effect=lambda *a, **k: call_order.append("append"),
            ):
                send.rerender_rich_text_session()

            assert call_order == ["truncate", "nudge", "append"]
