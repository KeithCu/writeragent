# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Unit tests for plugin.chatbot.rich_text_paste."""

from unittest.mock import MagicMock, patch

from plugin.tests.testing_utils import setup_uno_mocks

setup_uno_mocks()

from plugin.chatbot.rich_text_control import HISTORY_RENDER_BATCH_CHARS
from plugin.chatbot.rich_text_paste import (
    _list_prefix_for_paragraph,
    _resolve_portion_char_color,
    append_rich_messages_via_clipboard,
    append_rich_text_via_clipboard,
    build_message_html,
    insert_transferable_into_rich_control,
    iter_history_message_batches,
    session_history_items,
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


class TestPastePortionColor:
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


class TestEnsureTrailingLineBreak:
    def test_ensure_trailing_line_break(self):
        from plugin.chatbot.rich_text_paste import _ensure_trailing_line_break

        control = MagicMock()
        model = MagicMock(Text="You: hello")
        cursor = MagicMock()
        model.createTextCursor.return_value = cursor
        control.getModel.return_value = model

        with patch("plugin.chatbot.rich_text_paste._insert_string_at_rich_cursor") as mock_insert:
            _ensure_trailing_line_break(control)
            mock_insert.assert_called_once_with(model, cursor, "\n\n")

        model.Text = "already\n"
        with patch("plugin.chatbot.rich_text_paste._insert_string_at_rich_cursor") as mock_insert:
            _ensure_trailing_line_break(control)
            mock_insert.assert_called_once_with(model, cursor, "\n")

        model.Text = "already\n\n"
        with patch("plugin.chatbot.rich_text_paste._insert_string_at_rich_cursor") as mock_insert:
            _ensure_trailing_line_break(control)
            mock_insert.assert_not_called()


class TestAppendRichTextViaClipboard:
    def test_pipeline_order(self):
        control = MagicMock()
        control.getModel.return_value = MagicMock(Text="")
        ctx = MagicMock()
        doc = MagicMock()
        style_window = MagicMock()

        with patch("plugin.chatbot.rich_text_paste.create_hidden_html_writer", return_value=doc) as mock_create, \
             patch("plugin.chatbot.rich_text_paste._configure_hidden_writer_for_chat") as mock_cfg, \
             patch("plugin.chatbot.rich_text_paste.append_rich_text") as mock_append, \
             patch("plugin.chatbot.rich_text_paste._copy_formatted_from_hidden_doc_to_control", return_value=True) as mock_copy:
            append_rich_text_via_clipboard(ctx, control, "<p>Hi</p>", role="assistant", style_window=style_window)

        mock_create.assert_called_once_with(ctx)
        mock_cfg.assert_called_once_with(doc)
        mock_append.assert_called_once_with(doc, "<p>Hi</p>", role="assistant", style_window=style_window)
        mock_copy.assert_called_once()
        doc.close.assert_called_once_with(True)

    def test_user_insert_invokes_on_after_insert(self):
        control = MagicMock()
        control.getModel.return_value = MagicMock(Text="")
        ctx = MagicMock()
        doc = MagicMock()
        seen = []

        with patch("plugin.chatbot.rich_text_paste.create_hidden_html_writer", return_value=doc), \
             patch("plugin.chatbot.rich_text_paste._configure_hidden_writer_for_chat"), \
             patch("plugin.chatbot.rich_text_paste.append_rich_text"), \
             patch("plugin.chatbot.rich_text_paste._copy_formatted_from_hidden_doc_to_control", return_value=True), \
             patch("plugin.chatbot.rich_text_paste.get_control_text_length", return_value=42), \
             patch("plugin.chatbot.rich_text_paste._ensure_trailing_line_break"):
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

        with patch("plugin.chatbot.rich_text_paste.create_hidden_html_writer", return_value=doc), \
             patch("plugin.chatbot.rich_text_paste._configure_hidden_writer_for_chat"), \
             patch("plugin.chatbot.rich_text_paste.append_rich_text"), \
             patch("plugin.chatbot.rich_text_paste._copy_formatted_from_hidden_doc_to_control", return_value=False), \
             patch("plugin.chatbot.rich_text_paste._transferable_from_hidden_doc", return_value=None), \
             patch("plugin.chatbot.rich_text_paste.insert_transferable_into_rich_control") as mock_insert_tf:
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

        with patch("plugin.chatbot.rich_text_paste.create_hidden_html_writer", return_value=doc) as mock_create, \
             patch("plugin.chatbot.rich_text_paste._configure_hidden_writer_for_chat") as mock_cfg, \
             patch("plugin.chatbot.rich_text_paste.append_rich_text") as mock_append, \
             patch("plugin.chatbot.rich_text_paste._append_hidden_doc_to_control", return_value=True) as mock_copy, \
             patch("plugin.chatbot.rich_text_paste.nudge_rich_control_view_to_end") as mock_nudge:
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

        with patch("plugin.chatbot.rich_text_paste.create_hidden_html_writer", return_value=doc) as mock_create, \
             patch("plugin.chatbot.rich_text_paste._configure_hidden_writer_for_chat"), \
             patch("plugin.chatbot.rich_text_paste.append_rich_text") as mock_append, \
             patch("plugin.chatbot.rich_text_paste._append_hidden_doc_to_control", return_value=True) as mock_copy, \
             patch("plugin.chatbot.rich_text_paste.nudge_rich_control_view_to_end") as mock_nudge:
            append_rich_messages_via_clipboard(ctx, control, items, batch_chars=HISTORY_RENDER_BATCH_CHARS)

        assert mock_create.call_count == 2
        assert mock_append.call_count == 2
        assert mock_copy.call_count == 2
        assert mock_nudge.call_count == 2
        assert doc.close.call_count == 2


class TestListPrefix:
    def test_bullet_list_gets_bullet_prefix(self):
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


class TestInsertTransferableIntoRichControl:
    def test_does_not_use_plain_text_fallback(self):
        control = MagicMock()
        model = MagicMock(Text="before")
        control.getModel.return_value = model
        ctx = MagicMock()

        with patch("plugin.chatbot.rich_text_paste._try_insert_transferable_on_target", return_value=False), \
             patch("plugin.chatbot.rich_text_paste.get_control_text_length", return_value=6), \
             patch("plugin.chatbot.rich_text_paste._set_system_clipboard", return_value=False), \
             patch("plugin.chatbot.rich_text_paste._preserve_focus_window", side_effect=lambda _ctx, fn: fn()):
            ok = insert_transferable_into_rich_control(control, MagicMock(), ctx)

        assert ok is False
        assert model.Text == "before"

    def test_clipboard_key_paste_success(self):
        control = MagicMock()
        model = MagicMock(Text="")
        control.getModel.return_value = model
        ctx = MagicMock()
        transferable = MagicMock()

        with patch("plugin.chatbot.rich_text_paste._try_insert_transferable_on_target", return_value=False), \
             patch("plugin.chatbot.rich_text_paste.get_control_text_length", side_effect=[0, 12]), \
             patch("plugin.chatbot.rich_text_paste._set_system_clipboard", return_value=True) as mock_clip, \
             patch("plugin.chatbot.rich_text_paste._try_paste_via_key_event", return_value=True) as mock_key, \
             patch("plugin.chatbot.rich_text_paste._preserve_focus_window", side_effect=lambda _ctx, fn: fn()):
            ok = insert_transferable_into_rich_control(control, transferable, ctx)

        assert ok is True
        mock_clip.assert_called_once_with(ctx, transferable)
        mock_key.assert_called_once()
        control.setFocus.assert_not_called()
