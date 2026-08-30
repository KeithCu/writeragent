# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Unit tests for plugin.chatbot.rich_text_paste."""

import logging
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from plugin.tests.testing_utils import setup_uno_mocks

setup_uno_mocks()

from plugin.chatbot.rich_text_control import HISTORY_RENDER_BATCH_CHARS
from plugin.chatbot.rich_text_paste import (
    _column_width_twips,
    _copy_formatted_from_hidden_doc_to_control,
    _flatten_text_table_rows,
    _is_writer_text_table,
    _list_prefix_for_paragraph,
    _max_column_chars,
    _resolve_portion_char_color,
    _TABLE_V_PAD_MM100,
    _tab_stop_positions_twips,
    append_rich_messages_via_clipboard,
    append_rich_text_via_clipboard,
    build_message_html,
    iter_history_message_batches,
    session_history_items,
)


@contextmanager
def _immediate_focus(_ctx):
    yield


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
             patch("plugin.chatbot.rich_text_paste.configure_hidden_writer_for_chat") as mock_cfg, \
             patch("plugin.chatbot.rich_text_paste.append_rich_text") as mock_append, \
             patch("plugin.chatbot.rich_text_paste._copy_formatted_from_hidden_doc_to_control", return_value=(True, None)) as mock_copy:
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
             patch("plugin.chatbot.rich_text_paste.configure_hidden_writer_for_chat"), \
             patch("plugin.chatbot.rich_text_paste.append_rich_text"), \
             patch("plugin.chatbot.rich_text_paste._copy_formatted_from_hidden_doc_to_control", return_value=(True, None)), \
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
             patch("plugin.chatbot.rich_text_paste.configure_hidden_writer_for_chat") as mock_cfg, \
             patch("plugin.chatbot.rich_text_paste.append_rich_text") as mock_append, \
             patch("plugin.chatbot.rich_text_paste._append_hidden_doc_to_control", return_value=True) as mock_copy, \
             patch("plugin.chatbot.rich_text_paste._scroll_rich_to_tail") as mock_scroll:
            append_rich_messages_via_clipboard(ctx, control, items)

        mock_create.assert_called_once_with(ctx)
        mock_cfg.assert_called_once_with(doc)
        assert mock_append.call_count == 10
        mock_copy.assert_called_once()
        mock_scroll.assert_called_once_with(control, ctx)
        doc.close.assert_called_once_with(True)

    def test_append_user_message_scrolls_tail_after_trailing_break(self):
        control = MagicMock()
        control.getModel.return_value = MagicMock(Text="prior\n\n")
        ctx = MagicMock()
        doc = MagicMock()

        with patch("plugin.chatbot.rich_text_paste.create_hidden_html_writer", return_value=doc), \
             patch("plugin.chatbot.rich_text_paste.configure_hidden_writer_for_chat"), \
             patch("plugin.chatbot.rich_text_paste.append_rich_text"), \
             patch(
                 "plugin.chatbot.rich_text_paste._copy_formatted_from_hidden_doc_to_control",
                 return_value=(True, None),
             ), \
             patch("plugin.chatbot.rich_text_paste._ensure_trailing_line_break") as mock_trailing, \
             patch("plugin.chatbot.rich_text_paste.focus_preserved", _immediate_focus), \
             patch("plugin.chatbot.rich_text_paste._scroll_rich_to_tail") as mock_scroll, \
             patch("plugin.chatbot.rich_text_paste.get_control_text_length", return_value=100):
            append_rich_text_via_clipboard(ctx, control, "hello", role="user", auto_scroll=True)

        mock_trailing.assert_called_once_with(control)
        assert mock_scroll.call_count >= 1
        doc.close.assert_called_once_with(True)

    def test_append_rich_messages_scrolls_tail_after_each_batch(self):
        control = MagicMock()
        control.getModel.return_value = MagicMock(Text="")
        ctx = MagicMock()
        doc = MagicMock()
        chunk = "x" * 10000
        items = [("user", chunk), ("assistant", chunk)]

        with patch("plugin.chatbot.rich_text_paste.create_hidden_html_writer", return_value=doc) as mock_create, \
             patch("plugin.chatbot.rich_text_paste.configure_hidden_writer_for_chat"), \
             patch("plugin.chatbot.rich_text_paste.append_rich_text") as mock_append, \
             patch("plugin.chatbot.rich_text_paste._append_hidden_doc_to_control", return_value=True) as mock_copy, \
             patch("plugin.chatbot.rich_text_paste._scroll_rich_to_tail") as mock_scroll:
            append_rich_messages_via_clipboard(ctx, control, items, batch_chars=HISTORY_RENDER_BATCH_CHARS)

        assert mock_create.call_count == 2
        assert mock_append.call_count == 2
        assert mock_copy.call_count == 2
        assert mock_scroll.call_count == 2
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


class TestRichInsertFallbackLogging:
    def test_append_logs_direct_copy_reason_on_failure(self, caplog):
        control = MagicMock()
        control.getModel.return_value = MagicMock(Text="")
        ctx = MagicMock()
        doc = MagicMock()

        with caplog.at_level(logging.WARNING, logger="plugin.chatbot.rich_text_paste"), \
             patch("plugin.chatbot.rich_text_paste.create_hidden_html_writer", return_value=doc), \
             patch("plugin.chatbot.rich_text_paste.configure_hidden_writer_for_chat"), \
             patch("plugin.chatbot.rich_text_paste.append_rich_text"), \
             patch(
                 "plugin.chatbot.rich_text_paste._copy_formatted_from_hidden_doc_to_control",
                 return_value=(False, "no_content_inserted"),
             ), \
             patch("plugin.chatbot.rich_text_paste.get_control_text_length", return_value=10):
            append_rich_text_via_clipboard(ctx, control, "<p>x</p>", role="assistant")

        joined = " ".join(r.message for r in caplog.records)
        assert "formatted copy failed direct_copy_reason=no_content_inserted" in joined

    def test_copy_logs_no_content_inserted_when_nothing_written(self, caplog):
        control = MagicMock()
        model = MagicMock()
        model.createTextCursor.return_value = MagicMock()
        control.getModel.return_value = model
        src_doc = MagicMock()
        para_enum = MagicMock()
        para_enum.hasMoreElements.return_value = False
        src_text = MagicMock()
        src_text.createEnumeration.return_value = para_enum
        src_doc.getText.return_value = src_text

        with caplog.at_level(logging.WARNING, logger="plugin.chatbot.rich_text_paste"), \
             patch("plugin.chatbot.rich_text_paste.focus_preserved", _immediate_focus), \
             patch("plugin.chatbot.rich_text_paste.ChatTheme.resolve"), \
             patch("plugin.chatbot.rich_text_paste._rich_control_bg_color", return_value=0), \
             patch("plugin.chatbot.rich_text_paste.get_control_text_length", return_value=0):
            ok, reason = _copy_formatted_from_hidden_doc_to_control(src_doc, control, MagicMock(), role="user")

        assert ok is False
        assert reason == "no_content_inserted"
        assert any("reason=no_content_inserted" in r.message for r in caplog.records)


def _uno_enum(items):
    remaining = list(items)
    enum = MagicMock()
    enum.hasMoreElements.side_effect = lambda: bool(remaining)
    enum.nextElement.side_effect = lambda: remaining.pop(0)
    return enum


def _body_paragraph(text: str):
    para = MagicMock()
    para.supportsService.return_value = False
    para.getPropertyValue.side_effect = Exception("not a list")
    portion = MagicMock()
    portion.getString.return_value = text
    portion.CharColor = -1
    para.createEnumeration.return_value = _uno_enum([portion])
    return para


def _body_table(cells: list[list[str]]):
    table = MagicMock()
    table.supportsService.side_effect = lambda name: name == "com.sun.star.text.TextTable"
    table.createEnumeration.side_effect = RuntimeError("TextTable has no text portions")
    n_rows = len(cells)
    n_cols = len(cells[0]) if cells else 0
    table.getRows.return_value.getCount.return_value = n_rows
    table.getColumns.return_value.getCount.return_value = n_cols

    def _cell(col, row):
        cell = MagicMock()
        cell.getString.return_value = cells[row][col]
        return cell

    table.getCellByPosition.side_effect = _cell
    return table


class TestFlattenTextTableCopy:
    def test_is_writer_text_table_requires_bool_true(self):
        table = MagicMock()
        table.supportsService.return_value = True
        assert _is_writer_text_table(table) is True
        para = MagicMock()
        para.supportsService.return_value = False
        assert _is_writer_text_table(para) is False
        # Unconfigured MagicMock must not look like a table (``is True``).
        assert _is_writer_text_table(MagicMock()) is False

    def test_flatten_tab_separated_rows(self):
        table = _body_table([["a", "b"], ["c", "d"]])
        assert _flatten_text_table_rows(table) == ["a\tb", "c\td"]

    def test_tab_stops_use_max_column_width_not_header(self):
        rows = [
            ["Item", "Description", "Quantity"],
            ["Apples", "Fresh red apples", "12"],
            ["Bananas", "Ripe yellow bananas", "8"],
            ["Oranges", "Juicy orange citrus", "15"],
        ]
        max_chars = _max_column_chars(rows)
        assert max_chars == [7, 19, 8]  # Bananas, Ripe yellow bananas, Quantity
        stops = _tab_stop_positions_twips(max_chars)
        assert stops == (
            _column_width_twips(7),
            _column_width_twips(7) + _column_width_twips(19),
        )
        # Header-only widths would overrun column 1 (Item=4 < Bananas=7).
        header_only = _tab_stop_positions_twips([4, 11, 8])
        assert header_only[0] < stops[0]

    def test_copy_flattens_table_between_paragraphs(self):
        control = MagicMock()
        model = MagicMock()
        model.createTextCursor.return_value = MagicMock()
        control.getModel.return_value = model
        src_doc = MagicMock()
        src_doc.getText.return_value.createEnumeration.return_value = _uno_enum(
            [
                _body_paragraph("before"),
                _body_table([["Col A", "Col B"], ["stream", "plain"]]),
                _body_paragraph("after"),
            ]
        )
        theme = MagicMock(user_color=1, assistant_color=2)
        inserted: list[str] = []
        header_flags: list[tuple[str, bool, bool]] = []

        def _capture(_model, _cursor, text, char_color=None, **kwargs):
            inserted.append(text)
            header_flags.append((text, bool(kwargs.get("bold")), bool(kwargs.get("underline"))))

        with patch("plugin.chatbot.rich_text_paste.focus_preserved", _immediate_focus), \
             patch("plugin.chatbot.rich_text_paste.ChatTheme.resolve", return_value=theme), \
             patch("plugin.chatbot.rich_text_paste._rich_control_bg_color", return_value=0), \
             patch("plugin.chatbot.rich_text_paste.get_control_text_length", return_value=1), \
             patch("plugin.chatbot.rich_text_paste._apply_sidebar_para_margins"), \
             patch("plugin.chatbot.rich_text_paste._scroll_rich_to_tail"), \
             patch("plugin.chatbot.rich_text_paste._insert_string_at_rich_cursor", side_effect=_capture):
            ok, reason = _copy_formatted_from_hidden_doc_to_control(
                src_doc, control, MagicMock(), role="assistant", auto_scroll=False,
            )

        assert ok is True
        assert reason is None
        assert "before" in inserted
        assert "Col A\tCol B" in inserted
        assert "stream\tplain" in inserted
        assert "after" in inserted
        assert ("Col A\tCol B", True, True) in header_flags
        assert ("stream\tplain", False, False) in header_flags
        assert ("before", False, False) in header_flags
        assert ("after", False, False) in header_flags

    def test_copy_applies_tab_stops_from_max_width(self):
        control = MagicMock()
        model = MagicMock()
        dest_cursor = MagicMock()
        model.createTextCursor.return_value = dest_cursor
        control.getModel.return_value = model
        src_doc = MagicMock()
        cells = [
            ["Item", "Description", "Quantity"],
            ["Bananas", "Ripe yellow bananas", "8"],
        ]
        src_doc.getText.return_value.createEnumeration.return_value = _uno_enum(
            [_body_table(cells)]
        )
        theme = MagicMock(user_color=1, assistant_color=2)
        applied: list[tuple[int, ...]] = []

        with patch("plugin.chatbot.rich_text_paste.focus_preserved", _immediate_focus), \
             patch("plugin.chatbot.rich_text_paste.ChatTheme.resolve", return_value=theme), \
             patch("plugin.chatbot.rich_text_paste._rich_control_bg_color", return_value=0), \
             patch("plugin.chatbot.rich_text_paste.get_control_text_length", return_value=1), \
             patch("plugin.chatbot.rich_text_paste._apply_sidebar_para_margins"), \
             patch("plugin.chatbot.rich_text_paste._scroll_rich_to_tail"), \
             patch("plugin.chatbot.rich_text_paste._insert_string_at_rich_cursor"), \
             patch(
                 "plugin.chatbot.rich_text_paste._apply_table_tab_stops",
                 side_effect=lambda _cursor, positions: applied.append(tuple(positions)),
             ):
            ok, reason = _copy_formatted_from_hidden_doc_to_control(
                src_doc, control, MagicMock(), role="assistant", auto_scroll=False,
            )

        assert ok is True, reason
        expected = _tab_stop_positions_twips(_max_column_chars(cells))
        assert applied == [expected, expected]

    def test_copy_applies_vertical_pad_on_first_and_last_row(self):
        control = MagicMock()
        model = MagicMock()
        dest_cursor = MagicMock()
        model.createTextCursor.return_value = dest_cursor
        control.getModel.return_value = model
        src_doc = MagicMock()
        cells = [
            ["Item", "Description"],
            ["Apples", "Fresh red apples"],
            ["Bananas", "Ripe yellow bananas"],
        ]
        src_doc.getText.return_value.createEnumeration.return_value = _uno_enum(
            [_body_table(cells)]
        )
        theme = MagicMock(user_color=1, assistant_color=2)
        pads: list[tuple[int, int]] = []

        def _capture_vpad(cursor, *, top=0, bottom=0):
            pads.append((top, bottom))

        with patch("plugin.chatbot.rich_text_paste.focus_preserved", _immediate_focus), \
             patch("plugin.chatbot.rich_text_paste.ChatTheme.resolve", return_value=theme), \
             patch("plugin.chatbot.rich_text_paste._rich_control_bg_color", return_value=0), \
             patch("plugin.chatbot.rich_text_paste.get_control_text_length", return_value=1), \
             patch("plugin.chatbot.rich_text_paste._apply_sidebar_para_margins"), \
             patch("plugin.chatbot.rich_text_paste._scroll_rich_to_tail"), \
             patch("plugin.chatbot.rich_text_paste._insert_string_at_rich_cursor"), \
             patch("plugin.chatbot.rich_text_paste._apply_table_tab_stops"), \
             patch(
                 "plugin.chatbot.rich_text_paste._apply_table_row_vpad",
                 side_effect=_capture_vpad,
             ):
            ok, reason = _copy_formatted_from_hidden_doc_to_control(
                src_doc, control, MagicMock(), role="assistant", auto_scroll=False,
            )

        assert ok is True, reason
        pad = _TABLE_V_PAD_MM100
        assert pads == [(pad, 0), (0, 0), (0, pad)]

    def test_copy_ok_when_table_portion_enum_would_throw(self):
        control = MagicMock()
        model = MagicMock()
        model.createTextCursor.return_value = MagicMock()
        control.getModel.return_value = model
        src_doc = MagicMock()
        src_doc.getText.return_value.createEnumeration.return_value = _uno_enum(
            [_body_table([["only"]])]
        )
        theme = MagicMock(user_color=1, assistant_color=2)

        with patch("plugin.chatbot.rich_text_paste.focus_preserved", _immediate_focus), \
             patch("plugin.chatbot.rich_text_paste.ChatTheme.resolve", return_value=theme), \
             patch("plugin.chatbot.rich_text_paste._rich_control_bg_color", return_value=0), \
             patch("plugin.chatbot.rich_text_paste.get_control_text_length", return_value=1), \
             patch("plugin.chatbot.rich_text_paste._apply_sidebar_para_margins"), \
             patch("plugin.chatbot.rich_text_paste._scroll_rich_to_tail"), \
             patch("plugin.chatbot.rich_text_paste._insert_string_at_rich_cursor"):
            ok, reason = _copy_formatted_from_hidden_doc_to_control(
                src_doc, control, MagicMock(), role="assistant",
            )

        assert ok is True
        assert reason is None

