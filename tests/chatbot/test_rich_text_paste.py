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
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from plugin.chatbot import rich_text_paste as rich_text_paste_module
from tests.harness.strip_bundle import is_release_build, module_source_contains
from plugin.chatbot.rich_text_paste import (
    _cell_width_em,
    _column_width_twips,
    _copy_formatted_from_hidden_doc_to_control,
    _flatten_text_table_rows,
    _is_writer_text_table,
    _list_prefix_for_paragraph,
    _max_column_ems,
    _plain_fallback_text,
    _resolve_portion_char_color,
    _TABLE_V_PAD_MM100,
    _tab_stop_positions_twips,
    append_formatted_rows,
    append_formatted_row,
    fold_transcript_chunk,
    paint_message_items,
    session_history_items,
)


@contextmanager
def _immediate_focus(_ctx, _restore=None, **_kwargs):
    yield



def _uno_enum(items):
    remaining = list(items)
    enum = MagicMock()
    enum.hasMoreElements.side_effect = lambda: bool(remaining)
    enum.nextElement.side_effect = lambda: remaining.pop(0)
    return enum


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


class TestPastePortionColor:
    def test_resolve_portion_char_color_uses_role_and_prefix(self):
        portion = MagicMock(CharColor=-1)
        user = 0x2A6099
        assistant = 0x1E293B
        assert _resolve_portion_char_color(portion, "You: hi", user, assistant, "user") == user
        assert _resolve_portion_char_color(portion, "Assistant: hi", user, assistant, "assistant") == assistant
        with patch("plugin.chatbot.rich_text_paste._", side_effect=lambda message: "アシスタント:" if message == "Assistant:" else message):
            assert _resolve_portion_char_color(portion, "アシスタント: hi", user, assistant, "assistant") == assistant
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

    def test_copy_loop_tracks_role_for_automatic_colors_and_tables(self):
        def _portion(txt, color=-1):
            p = MagicMock()
            p.getString.return_value = txt
            p.CharColor = color
            p.CharFontName = "Liberation Sans"
            p.CharHeight = 10.0
            p.CharWeight = 100.0
            p.HyperLinkURL = ""
            return p

        def _para(portions):
            p = MagicMock()
            p.supportsService.return_value = False
            p.getPropertyValue.return_value = None
            p.createEnumeration.return_value = _uno_enum(portions)
            return p

        p1 = _para([_portion("You: "), _portion("hello")])
        tbl = _body_table([["user_cell"]])
        p2 = _para([_portion("error alert", color=0xFF0000)])
        p3 = _para([_portion("Assistant: "), _portion("hi")])

        src_doc = MagicMock()
        src_doc.getText.return_value.createEnumeration.return_value = _uno_enum([p1, tbl, p2, p3])

        control = MagicMock()
        model = MagicMock()
        model.createTextCursor.return_value = MagicMock()
        control.getModel.return_value = model

        written: list[tuple[str, Any]] = []
        def _capture(_model, _cursor, text, char_color=None, **_kw):
            written.append((text, char_color))

        user_color = 0x2A6099
        assistant_color = 0x1E293B
        theme = MagicMock(user_color=user_color, assistant_color=assistant_color)

        with patch("plugin.chatbot.rich_text_paste.focus_preserved", _immediate_focus), \
             patch("plugin.chatbot.rich_text_paste.ChatTheme.resolve", return_value=theme), \
             patch("plugin.chatbot.rich_text_paste._rich_control_bg_color", return_value=0), \
             patch("plugin.chatbot.rich_text_paste.get_control_text_length", return_value=0), \
             patch("plugin.chatbot.rich_text_paste._apply_sidebar_para_margins"), \
             patch("plugin.chatbot.rich_text_paste._scroll_rich_to_tail"), \
             patch("plugin.chatbot.rich_text_paste._insert_string_at_rich_cursor", side_effect=_capture):
            ok, reason = _copy_formatted_from_hidden_doc_to_control(
                src_doc, control, MagicMock(), role="assistant",
            )

        assert ok is True, reason
        writes_dict = {txt: color for txt, color in written}
        assert writes_dict.get("You: ") == user_color
        assert writes_dict.get("hello") == user_color
        assert writes_dict.get("user_cell") == user_color
        assert writes_dict.get("error alert") == 0xFF0000
        assert writes_dict.get("Assistant: ") == assistant_color
        assert writes_dict.get("hi") == assistant_color



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


class TestEnsureMessageSeparator:
    def test_does_not_insert_when_text_already_ends_with_blank_line(self):
        from plugin.chatbot.rich_text_paste import _ensure_message_separator

        control = MagicMock()
        model = MagicMock()
        cursor = MagicMock()
        model.createTextCursor.return_value = cursor
        control.getModel.return_value = model

        model.Text = "already\n\n"
        with patch("plugin.chatbot.rich_text_paste._insert_string_at_rich_cursor") as mock_insert:
            _ensure_message_separator(control)
            mock_insert.assert_not_called()

        model.Text = "needs gap"
        with patch("plugin.chatbot.rich_text_paste._insert_string_at_rich_cursor") as mock_insert:
            _ensure_message_separator(control)
            mock_insert.assert_called_once_with(model, cursor, "\n\n")

        model.Text = "already\n"
        with patch("plugin.chatbot.rich_text_paste._insert_string_at_rich_cursor") as mock_insert:
            _ensure_message_separator(control)
            mock_insert.assert_called_once_with(model, cursor, "\n")

    def test_plain_append_respects_existing_newlines(self):
        from plugin.chatbot.rich_text_paste import _plain_append_messages

        control = MagicMock()
        model = MagicMock()
        cursor = MagicMock()
        model.createTextCursor.return_value = cursor
        control.getModel.return_value = model

        model.Text = "needs gap"
        inserted = []
        with patch("plugin.chatbot.rich_text_paste._insert_string_at_rich_cursor", side_effect=lambda m, c, s, **kw: inserted.append(s)):
            _plain_append_messages(control, [("user", "next")], MagicMock(), MagicMock())
        assert inserted[0] == "\n\n"

        model.Text = "already\n"
        inserted.clear()
        with patch("plugin.chatbot.rich_text_paste._insert_string_at_rich_cursor", side_effect=lambda m, c, s, **kw: inserted.append(s)):
            _plain_append_messages(control, [("user", "next")], MagicMock(), MagicMock())
        assert inserted[0] == "\n"

        model.Text = "already\n\n"
        inserted.clear()
        with patch("plugin.chatbot.rich_text_paste._insert_string_at_rich_cursor", side_effect=lambda m, c, s, **kw: inserted.append(s)):
            _plain_append_messages(control, [("user", "next")], MagicMock(), MagicMock())
        assert inserted[0] == "You: "


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
            append_formatted_row(ctx, control, "<p>Hi</p>", role="assistant", style_window=style_window)

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
            append_formatted_row(
                ctx,
                control,
                "<p>hello</p>",
                role="user",
                on_after_insert=seen.append,
            )

        assert seen == [42]
        doc.close.assert_called_once_with(True)

    def test_plain_insert_skips_hidden_writer(self):
        control = MagicMock()
        model = MagicMock(Text="")
        cursor = MagicMock()
        model.createTextCursor.return_value = cursor
        control.getModel.return_value = model
        ctx = MagicMock()
        seen = []
        inserts = []

        def record_insert(_model, _cursor, text, char_color=None, bold=None, **_k):
            inserts.append((text, bold))

        with patch("plugin.chatbot.rich_text_paste.create_hidden_html_writer") as mock_create, \
             patch("plugin.chatbot.rich_text_paste._insert_string_at_rich_cursor", side_effect=record_insert), \
             patch("plugin.chatbot.rich_text_paste.get_control_text_length", return_value=15):
            ok = append_formatted_row(
                ctx,
                control,
                "hello",
                role="user",
                on_after_insert=seen.append,
            )

        assert ok is True
        mock_create.assert_not_called()
        assert seen == [15]
        assert any(txt.startswith("You:") and bold is True for txt, bold in inserts)

    def test_trailing_break_restores_through_the_frame_session_callback(self):
        """BUG A fix 3: the paint path's focus_preserved must use the gated callback."""
        control = MagicMock()
        control.getModel.return_value = MagicMock(Text="")
        query = MagicMock(name="query")
        restore_focus = MagicMock(name="restore_focus")
        calls: list[tuple[object, dict]] = []

        @contextmanager
        def _recording_focus(_ctx, restore=None, **kwargs):
            calls.append((restore, kwargs))
            yield

        with patch("plugin.chatbot.rich_text_paste.create_hidden_html_writer", return_value=MagicMock()), \
             patch("plugin.chatbot.rich_text_paste.configure_hidden_writer_for_chat"), \
             patch("plugin.chatbot.rich_text_paste.append_rich_text"), \
             patch("plugin.chatbot.rich_text_paste._copy_formatted_from_hidden_doc_to_control", return_value=(True, None)), \
             patch("plugin.chatbot.rich_text_paste.get_control_text_length", return_value=1), \
             patch("plugin.chatbot.rich_text_paste._scroll_rich_to_tail"), \
             patch("plugin.chatbot.rich_text_paste.focus_preserved", _recording_focus), \
             patch("plugin.chatbot.rich_text_paste._ensure_trailing_line_break"):
            append_formatted_row(
                MagicMock(), control, "<p>hello</p>", role="user", restore=query, restore_focus=restore_focus,
            )

        assert calls, "user row should restore focus around the trailing break"
        assert all(kwargs.get("restore_focus") is restore_focus for _restore, kwargs in calls)

    def test_not_success_when_later_element_skipped(self):
        control = MagicMock()
        model = MagicMock()
        model.Text = ""
        model.createTextCursor.return_value = MagicMock()
        control.getModel.return_value = model
        ctx = MagicMock()
        doc = MagicMock()
        good = _body_paragraph("kept")
        bad = MagicMock()
        bad.supportsService.return_value = False
        bad.getPropertyValue.return_value = None
        bad.createEnumeration.side_effect = RuntimeError("portion enum failed")
        bad.getString.side_effect = RuntimeError("getString failed")
        doc.getText.return_value.createEnumeration.return_value = _uno_enum([good, bad])
        theme = MagicMock(user_color=1, assistant_color=2)
        inserted: list[str] = []

        def _capture(_model, _cursor, text, char_color=None, **kwargs):
            inserted.append(text)

        with patch("plugin.chatbot.rich_text_paste.create_hidden_html_writer", return_value=doc), \
             patch("plugin.chatbot.rich_text_paste.configure_hidden_writer_for_chat"), \
             patch("plugin.chatbot.rich_text_paste.append_rich_text"), \
             patch("plugin.chatbot.rich_text_paste.focus_preserved", _immediate_focus), \
             patch("plugin.chatbot.rich_text_paste.process_events_to_idle"), \
             patch("plugin.chatbot.rich_text_paste.ChatTheme.resolve", return_value=theme), \
             patch("plugin.chatbot.rich_text_paste._rich_control_bg_color", return_value=0), \
             patch("plugin.chatbot.rich_text_paste.get_control_text_length", return_value=1), \
             patch("plugin.chatbot.rich_text_paste._apply_sidebar_para_margins"), \
             patch("plugin.chatbot.rich_text_paste._scroll_rich_to_tail"), \
             patch("plugin.chatbot.rich_text_paste._insert_string_at_rich_cursor", side_effect=_capture):
            ok = append_formatted_row(ctx, control, "<p>Hi</p>", role="assistant")

        assert ok is False
        assert "kept" in inserted
        bad.createEnumeration.assert_called()

    def test_element_enum_error_before_insert_degrades_to_plain_string(self):
        control = MagicMock()
        model = MagicMock()
        model.Text = ""
        model.createTextCursor.return_value = MagicMock()
        control.getModel.return_value = model
        ctx = MagicMock()
        doc = MagicMock()
        bad = MagicMock()
        bad.supportsService.return_value = False
        bad.getPropertyValue.return_value = None
        bad.createEnumeration.side_effect = RuntimeError("portion enum failed")
        bad.getString.return_value = "plain"
        good = _body_paragraph("kept")
        doc.getText.return_value.createEnumeration.return_value = _uno_enum([bad, good])
        theme = MagicMock(user_color=1, assistant_color=2)
        inserted: list[str] = []

        def _capture(_model, _cursor, text, char_color=None, **kwargs):
            inserted.append(text)

        with patch("plugin.chatbot.rich_text_paste.create_hidden_html_writer", return_value=doc), \
             patch("plugin.chatbot.rich_text_paste.configure_hidden_writer_for_chat"), \
             patch("plugin.chatbot.rich_text_paste.append_rich_text"), \
             patch("plugin.chatbot.rich_text_paste.focus_preserved", _immediate_focus), \
             patch("plugin.chatbot.rich_text_paste.process_events_to_idle"), \
             patch("plugin.chatbot.rich_text_paste.ChatTheme.resolve", return_value=theme), \
             patch("plugin.chatbot.rich_text_paste._rich_control_bg_color", return_value=0), \
             patch("plugin.chatbot.rich_text_paste.get_control_text_length", return_value=1), \
             patch("plugin.chatbot.rich_text_paste._apply_sidebar_para_margins"), \
             patch("plugin.chatbot.rich_text_paste._scroll_rich_to_tail"), \
             patch("plugin.chatbot.rich_text_paste._insert_string_at_rich_cursor", side_effect=_capture):
            ok = append_formatted_row(ctx, control, "<p>Hi</p>", role="assistant")

        assert ok is True
        assert "plain" in inserted
        assert "kept" in inserted

    def test_not_success_when_exception_in_copy_loop(self):
        control = MagicMock()
        model = MagicMock()
        model.Text = ""
        model.createTextCursor.return_value = MagicMock()
        control.getModel.return_value = model
        ctx = MagicMock()
        doc = MagicMock()
        good = _body_paragraph("kept")
        doc.getText.return_value.createEnumeration.return_value = _uno_enum([good])
        theme = MagicMock(user_color=1, assistant_color=2)

        def _raise_on_insert(*args, **kwargs):
            raise RuntimeError("insert failed")

        with patch("plugin.chatbot.rich_text_paste.create_hidden_html_writer", return_value=doc), \
             patch("plugin.chatbot.rich_text_paste.configure_hidden_writer_for_chat"), \
             patch("plugin.chatbot.rich_text_paste.append_rich_text"), \
             patch("plugin.chatbot.rich_text_paste.focus_preserved", _immediate_focus), \
             patch("plugin.chatbot.rich_text_paste.process_events_to_idle"), \
             patch("plugin.chatbot.rich_text_paste.ChatTheme.resolve", return_value=theme), \
             patch("plugin.chatbot.rich_text_paste._rich_control_bg_color", return_value=0), \
             patch("plugin.chatbot.rich_text_paste.get_control_text_length", return_value=1), \
             patch("plugin.chatbot.rich_text_paste._apply_sidebar_para_margins"), \
             patch("plugin.chatbot.rich_text_paste._scroll_rich_to_tail"), \
             patch("plugin.chatbot.rich_text_paste._insert_string_at_rich_cursor", side_effect=_raise_on_insert):
            ok = append_formatted_row(ctx, control, "<p>Hi</p>", role="assistant")

        assert ok is False

class TestHistoryMessageBatching:

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

    def test_session_history_items_puts_the_stop_line_inside_the_stopped_answer(self):
        """No empty 'Assistant:' row and no trailing blank line for the stop banner."""
        from plugin.chatbot.tool_loop_actions import _STOP_LINE

        session = MagicMock()
        session.messages = [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "Paragraph 1\n\nParagraph 2 can press"},
            {"role": "assistant", "content": _STOP_LINE},
            {"role": "user", "content": "q2"},
        ]
        assert session_history_items(session) == [
            ("user", "q"),
            ("assistant", "Paragraph 1\n\nParagraph 2 can press\n\n[Stopped by user]"),
            ("user", "q2"),
        ]
        session.messages[1]["content"] = "<p>partial</p>"
        assert session_history_items(session)[1] == ("assistant", "<p>partial</p><p>[Stopped by user]</p>")

    def test_session_history_items_stop_before_first_token_keeps_a_bare_banner_row(self):
        from plugin.chatbot.tool_loop_actions import _STOP_LINE

        session = MagicMock()
        session.messages = [{"role": "user", "content": "q"}, {"role": "assistant", "content": _STOP_LINE}]
        assert session_history_items(session) == [("user", "q"), ("assistant", "[Stopped by user]")]

    def test_append_formatted_rows_renders_all_messages_in_one_hidden_doc(self):
        control = MagicMock()
        control.getModel.return_value = MagicMock(Text="")
        ctx = MagicMock()
        doc = MagicMock()
        items = [("user", f"<p>msg{i}</p>") for i in range(10)]

        with patch("plugin.chatbot.rich_text_paste.create_hidden_html_writer", return_value=doc) as mock_create, \
             patch("plugin.chatbot.rich_text_paste.configure_hidden_writer_for_chat") as mock_cfg, \
             patch("plugin.chatbot.rich_text_paste.render_messages_to_hidden_doc") as mock_render, \
             patch("plugin.chatbot.rich_text_paste._copy_formatted_from_hidden_doc_to_control", return_value=(True, None)) as mock_copy, \
             patch("plugin.chatbot.rich_text_paste.get_control_text_length", return_value=0), \
             patch("plugin.chatbot.rich_text_paste._scroll_rich_to_tail") as mock_scroll:
            append_formatted_rows(ctx, control, items)

        mock_create.assert_called_once_with(ctx)
        mock_cfg.assert_called_once_with(doc)
        mock_render.assert_called_once_with(doc, items, style_window=None)
        mock_copy.assert_called_once()
        mock_scroll.assert_called_once_with(control, ctx, restore_focus=None)
        doc.close.assert_called_once_with(True)

    def test_failed_batch_plain_appends_with_role_prefix(self):
        control = MagicMock()
        model = MagicMock()
        model.Text = "prior"
        model.createTextCursor.return_value = MagicMock()
        control.getModel.return_value = model
        ctx = MagicMock()
        doc = MagicMock()
        items = [("user", "<p>Hello</p>"), ("assistant", "<p>it's done</p>")]
        written: list[tuple[str, Any, Any]] = []
        theme = MagicMock(user_color=1, assistant_color=2)

        def _capture(_model, _cursor, text, char_color=None, bold=None, **_kw):
            written.append((text, char_color, bold))
            model.Text = (model.Text or "") + text

        with patch("plugin.chatbot.rich_text_paste.create_hidden_html_writer", return_value=doc), \
             patch("plugin.chatbot.rich_text_paste.configure_hidden_writer_for_chat"), \
             patch("plugin.chatbot.rich_text_paste.render_messages_to_hidden_doc"), \
             patch("plugin.chatbot.rich_text_paste._copy_formatted_from_hidden_doc_to_control", return_value=(False, "failed")), \
             patch("plugin.chatbot.rich_text_paste.get_control_text_length", return_value=5), \
             patch("plugin.chatbot.rich_text_paste.ChatTheme.resolve", return_value=theme), \
             patch("plugin.chatbot.rich_text_paste._insert_string_at_rich_cursor", side_effect=_capture), \
             patch("plugin.chatbot.rich_text_paste.truncate_control_from") as mock_trunc:
            append_formatted_rows(ctx, control, items)

        mock_trunc.assert_called_once_with(control, 5)
        content_writes = [w for w in written if w[0] != "\n\n"]
        assert content_writes == [("You: ", 1, True), ("Hello", 1, False), ("Assistant: ", 2, True), ("it's done", 2, False)]
        doc.close.assert_called_once_with(True)

    def test_unknown_length_skips_rollback(self):
        control = MagicMock()
        control.getModel.return_value = MagicMock(Text="prior")
        ctx = MagicMock()
        items = [("assistant", "<p>kept</p>")]

        with patch("plugin.chatbot.rich_text_paste.create_hidden_html_writer") as mock_create, \
             patch("plugin.chatbot.rich_text_paste._copy_formatted_from_hidden_doc_to_control") as mock_copy, \
             patch("plugin.chatbot.rich_text_paste.get_control_text_length", return_value=None), \
             patch("plugin.chatbot.rich_text_paste._plain_append_messages") as mock_plain, \
             patch("plugin.chatbot.rich_text_paste.truncate_control_from") as mock_trunc:
            append_formatted_rows(ctx, control, items)

        mock_create.assert_not_called()
        mock_copy.assert_not_called()
        mock_trunc.assert_not_called()
        mock_plain.assert_called_once()
        assert mock_plain.call_args.args[1] == items

    def test_append_formatted_rows_tags_left_in_the_hidden_doc_are_not_copied(self):
        control = MagicMock()
        model = MagicMock()
        model.Text = "prior"
        model.createTextCursor.return_value = MagicMock()
        control.getModel.return_value = model
        ctx = MagicMock()
        doc = MagicMock()
        doc.getText.return_value.getString.return_value = "Assistant: <script>alert(1)</script>"

        with patch("plugin.chatbot.rich_text_paste.create_hidden_html_writer", return_value=doc), \
             patch("plugin.chatbot.rich_text_paste.configure_hidden_writer_for_chat"), \
             patch("plugin.chatbot.rich_text_paste.render_messages_to_hidden_doc"), \
             patch("plugin.chatbot.rich_text_paste.get_control_text_length", return_value=5), \
             patch("plugin.chatbot.rich_text_paste._rollback_rich_insert") as mock_rollback, \
             patch("plugin.chatbot.rich_text_paste._copy_formatted_from_hidden_doc_to_control") as mock_copy, \
             patch("plugin.chatbot.rich_text_paste._plain_append_messages") as mock_plain:
            append_formatted_rows(ctx, control, [("assistant", "<p>Hi</p><script>alert(1)</script>")])

        mock_copy.assert_not_called()
        mock_rollback.assert_called_once_with(control, 5)
        mock_plain.assert_called_once()
        doc.close.assert_called_once_with(True)

    def test_append_formatted_row_unknown_length_returns_true_and_plain_appends(self):
        control = MagicMock()
        control.getModel.return_value = MagicMock(Text="prior")
        ctx = MagicMock()

        with patch("plugin.chatbot.rich_text_paste.create_hidden_html_writer") as mock_create, \
             patch("plugin.chatbot.rich_text_paste._copy_formatted_from_hidden_doc_to_control") as mock_copy, \
             patch("plugin.chatbot.rich_text_paste.get_control_text_length", return_value=None), \
             patch("plugin.chatbot.rich_text_paste._plain_append_messages") as mock_plain:
            ok = append_formatted_row(ctx, control, "<p>hello</p>", role="assistant")

        assert ok is True
        mock_create.assert_not_called()
        mock_copy.assert_not_called()
        mock_plain.assert_called_once()

    def test_append_formatted_row_copy_failure_with_known_length_returns_false_and_does_not_plain_append(self):
        control = MagicMock()
        control.getModel.return_value = MagicMock(Text="prior")
        ctx = MagicMock()
        doc = MagicMock()

        with patch("plugin.chatbot.rich_text_paste.create_hidden_html_writer", return_value=doc), \
             patch("plugin.chatbot.rich_text_paste.configure_hidden_writer_for_chat"), \
             patch("plugin.chatbot.rich_text_paste.append_rich_text", return_value=True), \
             patch("plugin.chatbot.rich_text_paste._copy_formatted_from_hidden_doc_to_control", return_value=(False, "failed")), \
             patch("plugin.chatbot.rich_text_paste.get_control_text_length", return_value=10), \
             patch("plugin.chatbot.rich_text_paste._rollback_rich_insert") as mock_rollback, \
             patch("plugin.chatbot.rich_text_paste._plain_append_messages") as mock_plain:
            ok = append_formatted_row(ctx, control, "<p>hello</p>", role="assistant")

        assert ok is False
        mock_rollback.assert_called_once_with(control, 10)
        mock_plain.assert_not_called()
        doc.close.assert_called_once_with(True)

    def test_html_import_failure_plain_appends(self):
        control = MagicMock()
        model = MagicMock()
        model.Text = ""
        model.createTextCursor.return_value = MagicMock()
        control.getModel.return_value = model
        ctx = MagicMock()
        doc = MagicMock()

        with patch("plugin.chatbot.rich_text_paste.create_hidden_html_writer", return_value=doc), \
             patch("plugin.chatbot.rich_text_paste.configure_hidden_writer_for_chat"), \
             patch("plugin.chatbot.rich_text_paste.append_rich_text", return_value=False), \
             patch("plugin.chatbot.rich_text_paste._copy_formatted_from_hidden_doc_to_control") as mock_copy, \
             patch("plugin.chatbot.rich_text_paste.get_control_text_length", return_value=0), \
             patch("plugin.chatbot.rich_text_paste._plain_append_messages") as mock_plain:
            ok = append_formatted_row(ctx, control, "<p>Hi</p>", role="assistant")

        assert ok is True
        mock_copy.assert_not_called()
        mock_plain.assert_called_once()
        assert mock_plain.call_args.args[1] == [("assistant", "<p>Hi</p>")]
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
            append_formatted_row(ctx, control, "<p>hello</p>", role="user", auto_scroll=True)

        mock_trailing.assert_called_once_with(control)
        assert mock_scroll.call_count == 1
        doc.close.assert_called_once_with(True)


class TestListPrefix:
    def _prop(self, name, value):
        prop = MagicMock()
        prop.Name = name
        prop.Value = value
        return prop

    def _para(self, mapping, rules):
        para = MagicMock()
        store = dict(mapping)
        store["NumberingRules"] = rules
        para.getPropertyValue.side_effect = lambda name: store[name]
        return para

    def test_bullet_list_gets_bullet_prefix(self):
        rules = MagicMock()
        rules.getByIndex.return_value = [self._prop("BulletChar", "\u2022")]
        para = self._para(
            {"NumberingIsNumber": True, "NumberingLevel": 0, "ListId": "L1"},
            rules,
        )
        assert _list_prefix_for_paragraph(para, {}) == "\u2022 "

    def test_ordered_list_gets_number_prefix(self):
        rules = MagicMock()
        rules.getByIndex.return_value = [self._prop("NumberingType", 4)]
        para = self._para(
            {
                "NumberingIsNumber": True,
                "NumberingLevel": 0,
                "ListId": "L1",
                "ListLabelString": None,
            },
            rules,
        )
        counters: dict = {}
        assert _list_prefix_for_paragraph(para, counters) == "1. "
        assert _list_prefix_for_paragraph(para, counters) == "2. "

    def test_ordered_list_uses_writer_label_for_start_value(self):
        """<ol start="11"> was drawn from 1: the counter ignored the start value."""
        rules = MagicMock()
        rules.getByIndex.return_value = [self._prop("NumberingType", 4)]
        for label, expected in (("11.", "11. "), ("iv.", "iv. "), ("", "1. ")):
            para = self._para(
                {
                    "NumberingIsNumber": True,
                    "NumberingLevel": 0,
                    "ListId": "L1",
                    "ListLabelString": label,
                },
                rules,
            )
            assert _list_prefix_for_paragraph(para, {}) == expected

    def test_list_prefix_numbering_types_and_labels(self):
        rules_type9 = MagicMock()
        rules_type9.getByIndex.return_value = [self._prop("NumberingType", 9)]
        para9 = self._para(
            {"NumberingIsNumber": True, "NumberingLevel": 0, "ListId": "L1", "ListLabelString": "A."},
            rules_type9,
        )
        assert _list_prefix_for_paragraph(para9, {}) == "A. "

        rules_type6 = MagicMock()
        rules_type6.getByIndex.return_value = [
            self._prop("NumberingType", 6),
            self._prop("BulletChar", "\u2022"),
        ]
        para6 = self._para(
            {"NumberingIsNumber": True, "NumberingLevel": 0, "ListId": "L2", "ListLabelString": ""},
            rules_type6,
        )
        assert _list_prefix_for_paragraph(para6, {}) == "\u2022 "

        rules_type4 = MagicMock()
        rules_type4.getByIndex.return_value = [self._prop("NumberingType", 4)]
        para4 = self._para(
            {"NumberingIsNumber": True, "NumberingLevel": 0, "ListId": "L3", "ListLabelString": ""},
            rules_type4,
        )
        counters: dict = {}
        assert _list_prefix_for_paragraph(para4, counters) == "1. "
        assert _list_prefix_for_paragraph(para4, counters) == "2. "

    def test_missing_type_and_no_bullet_char_returns_number_prefix(self):
        rules = MagicMock()
        rules.getByIndex.return_value = []
        para = self._para(
            {"NumberingIsNumber": True, "NumberingLevel": 0, "ListId": "L1", "ListLabelString": ""},
            rules,
        )
        assert _list_prefix_for_paragraph(para, {}) == "1. "

    def test_not_number_with_large_margin_returns_empty(self):
        para = self._para(
            {"NumberingIsNumber": False, "ParaLeftMargin": 1000},
            None,
        )
        assert _list_prefix_for_paragraph(para, {}) == ""


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
            append_formatted_row(ctx, control, "<p>x</p>", role="assistant")

        joined = " ".join(r.message for r in caplog.records)
        assert "formatted copy failed direct_copy_reason=no_content_inserted" in joined

    def test_failed_copy_rolls_back_separator_and_drops_spans(self):
        from plugin.calc.navigation import cell_link_registry

        control = MagicMock()
        model = MagicMock()
        model.Text = "hello"
        model.createTextCursor.return_value = MagicMock()
        control.getModel.return_value = model
        ctx = MagicMock()
        doc = MagicMock()
        cell_link_registry.clear(control)
        cell_link_registry.add(control, 0, 5, "A1")
        cell_link_registry.add(control, 5, 9, "B2")

        def _grow(_model, _cursor, text, char_color=None, **kwargs):
            model.Text = model.Text + text

        with patch("plugin.chatbot.rich_text_paste.create_hidden_html_writer", return_value=doc), \
             patch("plugin.chatbot.rich_text_paste.configure_hidden_writer_for_chat"), \
             patch("plugin.chatbot.rich_text_paste.append_rich_text"), \
             patch(
                 "plugin.chatbot.rich_text_paste._copy_formatted_from_hidden_doc_to_control",
                 return_value=(False, "no_content_inserted"),
             ), \
             patch("plugin.chatbot.rich_text_paste._insert_string_at_rich_cursor", side_effect=_grow):
            ok = append_formatted_row(ctx, control, "<p>x</p>", role="assistant")

        assert ok is False
        model.createTextCursor.return_value.setString.assert_called_with("")
        assert cell_link_registry.lookup(control, 0) == "A1"
        assert cell_link_registry.lookup(control, 5) is None
        cell_link_registry.clear(control)

    def test_missing_hidden_writer_plain_appends(self):
        control = MagicMock()
        model = MagicMock()
        model.Text = "prior"
        model.createTextCursor.return_value = MagicMock()
        control.getModel.return_value = model
        ctx = MagicMock()

        def _grow(_model, _cursor, text, char_color=None, **kwargs):
            model.Text = model.Text + text

        with patch("plugin.chatbot.rich_text_paste.create_hidden_html_writer", return_value=None), \
             patch("plugin.chatbot.rich_text_paste._insert_string_at_rich_cursor", side_effect=_grow), \
             patch("plugin.chatbot.rich_text_paste._plain_append_messages") as mock_plain:
            ok = append_formatted_row(ctx, control, "<p>Hi &amp; you</p>", role="assistant")

        assert ok is True
        mock_plain.assert_called_once()
        assert mock_plain.call_args.args[1] == [("assistant", "<p>Hi &amp; you</p>")]
        model.createTextCursor.return_value.setString.assert_called_with("")


class TestPaintMessageItems:
    def test_failed_copy_does_not_keep_text_outside_the_list(self):
        """A copy that fails halfway must not leave that partial in the control."""
        control = MagicMock()
        model = MagicMock()
        model.Text = "OLD_SPLICE"
        model.createTextCursor.return_value = MagicMock()
        control.getModel.return_value = model
        ctx = MagicMock()
        doc = MagicMock()
        doc.getText.return_value.getString.return_value = "You: keep me\n\nAssistant: second"
        written: list[str] = []

        def _copy(*_args, **_kwargs):
            model.Text = "PARTIAL_NOT_IN_MESSAGES"
            return False

        def _capture(_model, _cursor, text, **_kwargs):
            written.append(text)
            model.Text = (model.Text or "") + text

        items = [("user", "<p>keep me</p>"), ("assistant", "<p>second</p>")]
        with patch("plugin.chatbot.rich_text_paste.create_hidden_html_writer", return_value=doc), \
             patch("plugin.chatbot.rich_text_paste.configure_hidden_writer_for_chat"), \
             patch("plugin.chatbot.rich_text_paste.render_messages_to_hidden_doc"), \
             patch("plugin.chatbot.rich_text_paste._copy_formatted_from_hidden_doc_to_control", side_effect=_copy), \
             patch("plugin.chatbot.rich_text_paste._insert_string_at_rich_cursor", side_effect=_capture), \
             patch("plugin.chatbot.rich_text_paste._scroll_rich_to_tail"):
            paint_message_items(ctx, control, items)

        assert "PARTIAL_NOT_IN_MESSAGES" not in (model.Text or "")
        assert "OLD_SPLICE" not in (model.Text or "")
        assert any("keep me" in row for row in written)
        assert any("second" in row for row in written)
        doc.close.assert_called_once_with(True)

    def test_tags_left_in_the_hidden_doc_are_not_copied(self):
        control = MagicMock()
        model = MagicMock()
        model.Text = ""
        model.createTextCursor.return_value = MagicMock()
        control.getModel.return_value = model
        ctx = MagicMock()
        doc = MagicMock()
        doc.getText.return_value.getString.return_value = "Assistant: <script>alert(1)</script>"
        written: list[str] = []

        def _capture(_model, _cursor, text, **_kwargs):
            written.append(text)

        with patch("plugin.chatbot.rich_text_paste.create_hidden_html_writer", return_value=doc), \
             patch("plugin.chatbot.rich_text_paste.configure_hidden_writer_for_chat"), \
             patch("plugin.chatbot.rich_text_paste.render_messages_to_hidden_doc"), \
             patch("plugin.chatbot.rich_text_paste._copy_formatted_from_hidden_doc_to_control") as mock_copy, \
             patch("plugin.chatbot.rich_text_paste._insert_string_at_rich_cursor", side_effect=_capture):
            paint_message_items(ctx, control, [("assistant", "<p>Hi</p><script>alert(1)</script>")])

        mock_copy.assert_not_called()
        assert any("Hi" in row for row in written)
        assert not any("<script>" in row for row in written)
        doc.close.assert_called_once_with(True)

    def test_generic_token_in_the_hidden_doc_is_copied(self):
        """``<String>`` is not leftover HTML, so the paint must not strip it."""
        control = MagicMock()
        model = MagicMock()
        model.Text = ""
        model.createTextCursor.return_value = MagicMock()
        control.getModel.return_value = model
        ctx = MagicMock()
        pos = MagicMock()
        pos.Width = 1
        control.getPosSize.return_value = pos
        doc = MagicMock()
        doc.getText.return_value.getString.return_value = "Assistant: List<String> <user@example.com>"

        with patch("plugin.chatbot.rich_text_paste.create_hidden_html_writer", return_value=doc), \
             patch("plugin.chatbot.rich_text_paste.configure_hidden_writer_for_chat"), \
             patch("plugin.chatbot.rich_text_paste.render_messages_to_hidden_doc"), \
             patch("plugin.chatbot.rich_text_paste._copy_formatted_from_hidden_doc_to_control", return_value=(True, None)) as mock_copy, \
             patch("plugin.chatbot.rich_text_paste._plain_append_messages") as mock_plain, \
             patch("plugin.chatbot.rich_text_paste._scroll_rich_to_tail"):
            paint_message_items(ctx, control, [("assistant", "<p>List<String> <user@example.com></p>")])

        mock_copy.assert_called_once()
        mock_plain.assert_not_called()
        doc.close.assert_called_once_with(True)

    def test_mixed_paint_creates_hidden_writer_once(self):
        control = MagicMock()
        control.getModel.return_value = MagicMock(Text="")
        ctx = MagicMock()
        doc = MagicMock()
        doc.getText.return_value.getString.return_value = "You: hello\n\nAssistant: bold"

        with patch("plugin.chatbot.rich_text_paste.create_hidden_html_writer", return_value=doc) as mock_create, \
             patch("plugin.chatbot.rich_text_paste.configure_hidden_writer_for_chat"), \
             patch("plugin.chatbot.rich_text_paste.render_messages_to_hidden_doc"), \
             patch("plugin.chatbot.rich_text_paste._copy_formatted_from_hidden_doc_to_control", return_value=(True, None)), \
             patch("plugin.chatbot.rich_text_paste._force_rich_full_reformat"), \
             patch("plugin.chatbot.rich_text_paste._scroll_rich_to_tail"):
            items = [("user", "hello"), ("assistant", "<p><b>bold</b></p>")]
            paint_message_items(ctx, control, items)

        mock_create.assert_called_once_with(ctx)
        doc.close.assert_called_once_with(True)

    def test_stop_text_folds_like_any_other_chunk(self):
        """The stop line is not detected by comparing text. The turn writes it."""
        session = MagicMock()
        session.messages = [{"role": "assistant", "content": "partial answer", "_open_transcript": True}]
        assert fold_transcript_chunk(session, "\n[Stopped by user]\n") is True
        assert session.messages[0]["content"] == "partial answer\n[Stopped by user]\n"
        assert len(session.messages) == 1

    def test_user_message_already_present_returns_true_without_duplication(self):
        """User message recorded at send time returns True so panel paints it immediately."""
        session = MagicMock()
        session.messages = [{"role": "user", "content": "What is Python?"}]
        assert fold_transcript_chunk(session, "What is Python?", role="user") is True
        assert len(session.messages) == 1
        assert session.messages[0]["content"] == "What is Python?"

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


def _body_paragraph(text: str):
    para = MagicMock()
    para.supportsService.return_value = False
    # Non-list paragraphs: NumberingIsNumber is void, not an exception.
    para.getPropertyValue.return_value = None
    portion = MagicMock()
    portion.getString.return_value = text
    portion.CharColor = -1
    # Compared with <= after _normalize_portion_font lost its try.
    portion.CharFontName = ""
    portion.CharHeight = 10.0
    portion.CharWeight = 100.0
    para.createEnumeration.return_value = _uno_enum([portion])
    return para


class TestFlattenTextTableCopy:
    def test_is_writer_text_table(self):
        table = MagicMock()
        table.supportsService.return_value = True
        assert _is_writer_text_table(table) is True
        para = MagicMock()
        para.supportsService.return_value = False
        assert _is_writer_text_table(para) is False

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
        max_ems = _max_column_ems(rows)
        assert max_ems[0] == _cell_width_em("Bananas")  # wider than bold Item
        stops = _tab_stop_positions_twips(rows)
        assert stops == (
            _column_width_twips(max_ems[0]),
            _column_width_twips(max_ems[0]) + _column_width_twips(max_ems[1]),
        )
        header_only = _tab_stop_positions_twips([rows[0]])
        assert header_only[0] < stops[0]
        # Tab is past the longest glyph so Bananas cannot skip the first stop.
        bananas_twips = int(round(_cell_width_em("Bananas") * 10.0 * 20))
        assert stops[0] > bananas_twips

    def test_tab_stops_tighter_than_char_count_plus_8pt_for_even_cells(self):
        rows = [
            ["Header 1", "Header 2", "Header 3"],
            ["Row 1 Col 1", "Row 1 Col 2", "Row 1 Col 3"],
            ["Row 2 Col 1", "Row 2 Col 2", "Row 2 Col 3"],
        ]
        stops = _tab_stop_positions_twips(rows)
        old = int(round((11 * 10.0 * 0.5 + 8.0) * 20))  # previous 0.5em + 8pt slack
        assert stops[0] < old
        assert stops[1] - stops[0] < old

    def test_cjk_emoji_and_combining_widths(self):
        # CJK characters (W) add 1.0 em each; ASCII adds ~0.55 em each
        cjk_width = _cell_width_em("中文")
        ascii_width = _cell_width_em("ab")
        assert cjk_width == 2.0
        assert ascii_width < cjk_width

        # Combining mark (Mn) adds 0 width
        assert _cell_width_em("e\u0301") == _cell_width_em("e")

        # Emoji with East Asian width W adds 1.0 em
        assert _cell_width_em("😀") == 1.0

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

    def test_list_number_is_inserted_normal_weight(self):
        """The first number after "Assistant: " drew bold (sticky label weight)."""
        control = MagicMock()
        model = MagicMock()
        model.createTextCursor.return_value = MagicMock()
        control.getModel.return_value = model
        src_doc = MagicMock()
        label = _body_paragraph("Assistant: ")
        item = _body_paragraph("alpha")
        src_doc.getText.return_value.createEnumeration.return_value = _uno_enum([label, item])
        theme = MagicMock(user_color=1, assistant_color=2)
        calls: list[tuple[str, object, object]] = []

        def _capture(_model, _cursor, text, char_color=None, **kwargs):
            calls.append((text, kwargs.get("bold"), kwargs.get("underline")))

        with patch("plugin.chatbot.rich_text_paste.focus_preserved", _immediate_focus), \
             patch("plugin.chatbot.rich_text_paste.ChatTheme.resolve", return_value=theme), \
             patch("plugin.chatbot.rich_text_paste._rich_control_bg_color", return_value=0), \
             patch("plugin.chatbot.rich_text_paste.get_control_text_length", return_value=1), \
             patch("plugin.chatbot.rich_text_paste._apply_sidebar_para_margins"), \
             patch("plugin.chatbot.rich_text_paste._scroll_rich_to_tail"), \
             patch(
                 "plugin.chatbot.rich_text_paste._list_prefix_for_paragraph",
                 side_effect=lambda para, _counters: "1. " if para is item else "",
             ), \
             patch("plugin.chatbot.rich_text_paste._insert_string_at_rich_cursor", side_effect=_capture):
            ok, reason = _copy_formatted_from_hidden_doc_to_control(
                src_doc, control, MagicMock(), role="assistant", auto_scroll=False,
            )

        assert ok is True, reason
        assert ("1. ", False, False) in calls

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
        expected = _tab_stop_positions_twips(cells)
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

    def test_copy_formatted_portion_font_and_link_optimization(self):
        control = MagicMock()
        model = MagicMock()
        font_sets: list[str] = []

        class MockCursor(MagicMock):
            def __setattr__(self, name: str, value: object = None) -> None:
                if name == "CharFontName":
                    font_sets.append(str(value))
                super().__setattr__(name, value)

        dest_cursor = MockCursor()
        model.createTextCursor.return_value = dest_cursor
        control.getModel.return_value = model
        src_doc = MagicMock()

        portion1 = MagicMock()
        portion1.getString.return_value = "Normal prose"
        portion1.CharColor = -1
        portion1.CharFontName = "Times New Roman"
        portion1.CharHeight = 10.0
        portion1.CharWeight = 100.0
        portion1.HyperLinkURL = ""

        portion2 = MagicMock()
        portion2.getString.return_value = "Sheet1.A1"
        portion2.CharColor = -1
        portion2.CharFontName = "Liberation Sans"
        portion2.CharHeight = 10.0
        portion2.CharWeight = 100.0
        portion2.HyperLinkURL = ""

        p1 = MagicMock()
        p1.supportsService.return_value = False
        p1.getPropertyValue.return_value = None
        p1.createEnumeration.return_value = _uno_enum([portion1])

        p2 = MagicMock()
        p2.supportsService.return_value = False
        p2.getPropertyValue.return_value = None
        p2.createEnumeration.return_value = _uno_enum([portion2])

        src_doc.getText.return_value.createEnumeration.return_value = _uno_enum([p1, p2])
        theme = MagicMock(user_color=1, assistant_color=2)

        from plugin.calc.navigation import cell_link_registry
        cell_link_registry.clear(control)
        try:
            with patch("plugin.chatbot.rich_text_paste.focus_preserved", _immediate_focus), \
                 patch("plugin.chatbot.rich_text_paste.ChatTheme.resolve", return_value=theme), \
                 patch("plugin.chatbot.rich_text_paste._rich_control_bg_color", return_value=0), \
                 patch("plugin.chatbot.rich_text_paste.get_control_text_length", return_value=10) as mock_len, \
                 patch("plugin.chatbot.rich_text_paste._apply_sidebar_para_margins"), \
                 patch("plugin.chatbot.rich_text_paste._scroll_rich_to_tail"), \
                 patch("plugin.chatbot.rich_text_paste._insert_string_at_rich_cursor"):
                ok, reason = _copy_formatted_from_hidden_doc_to_control(
                    src_doc,
                    control,
                    MagicMock(),
                    role="assistant",
                    auto_scroll=False,
                    cell_link_targets=[("Sheet1.A1", "Sheet1.A1")],
                )

            assert ok is True, reason
            from plugin.chatbot.rich_text import CHAT_FONT_NAME
            assert font_sets == [CHAT_FONT_NAME, "Liberation Sans"]
            assert mock_len.call_count == 1
            spans = cell_link_registry._spans.get(id(control), [])
            assert spans == [(23, 32, "Sheet1.A1")]
        finally:
            cell_link_registry.clear(control)

    @pytest.mark.parametrize(
        ("portions_spec", "queue", "expected_spans"),
        [
            # Failing case 1: href then plain portion with same label consumes both queue entries
            ([("See", "writeragent-cell://A1"), ("See", "")], [("See", "A1"), ("See", "C3")], ["A1", "C3"]),
            # Failing case 2: href consumes queue entry; later plain portion does not take stale address
            ([("See", "writeragent-cell://A1"), ("See", "")], [("See", "A1")], ["A1"]),
            # Correct case 3: two plain portions with matching queue entries
            ([("See", ""), ("See", "")], [("See", "A1"), ("See", "C3")], ["A1", "C3"]),
            # Correct case 4: split label without href matches neither
            ([("Sheet1.", ""), ("A1", "")], [("Sheet1.A1", "Sheet1.A1")], []),
            # Correct case 5: label inside longer portion does not match
            ([("See A1 for details", "")], [("A1", "A1")], []),
            # Correct case 6: single pending link without href
            ([("Sheet1.A1", "")], [("Sheet1.A1", "Sheet1.A1")], ["Sheet1.A1"]),
        ],
    )
    def test_cell_link_queue_consumption(self, portions_spec, queue, expected_spans):
        from plugin.calc.navigation import cell_link_registry
        from plugin.chatbot.rich_text_paste import _copy_formatted_from_hidden_doc_to_control

        control = MagicMock()
        model = MagicMock()
        model.createTextCursor.return_value = MagicMock()
        control.getModel.return_value = model
        src_doc = MagicMock()

        portions = []
        for txt, href in portions_spec:
            p = MagicMock()
            p.getString.return_value = txt
            p.CharColor = -1
            p.CharFontName = "Liberation Sans"
            p.CharHeight = 10.0
            p.CharWeight = 100.0
            p.HyperLinkURL = href
            p.CharUnderline = 0
            portions.append(p)

        para = MagicMock()
        para.supportsService.return_value = False
        para.getPropertyValue.return_value = None
        para.createEnumeration.return_value = _uno_enum(portions)
        src_doc.getText.return_value.createEnumeration.return_value = _uno_enum([para])

        theme = MagicMock(user_color=1, assistant_color=2)

        try:
            with patch("plugin.chatbot.rich_text_paste.focus_preserved", _immediate_focus), \
                 patch("plugin.chatbot.rich_text_paste.ChatTheme.resolve", return_value=theme), \
                 patch("plugin.chatbot.rich_text_paste._rich_control_bg_color", return_value=0), \
                 patch("plugin.chatbot.rich_text_paste.get_control_text_length", return_value=0) as mock_len, \
                 patch("plugin.chatbot.rich_text_paste._apply_sidebar_para_margins"), \
                 patch("plugin.chatbot.rich_text_paste._scroll_rich_to_tail"), \
                 patch("plugin.chatbot.rich_text_paste._insert_string_at_rich_cursor"):
                ok, reason = _copy_formatted_from_hidden_doc_to_control(
                    src_doc,
                    control,
                    MagicMock(),
                    role="assistant",
                    auto_scroll=False,
                    cell_link_targets=list(queue),
                )
            assert ok is True, reason
            assert mock_len.call_count == 1
            registered = [addr for _s, _e, addr in cell_link_registry._spans.get(id(control), [])]
            assert registered == expected_spans
        finally:
            cell_link_registry.clear(control)

    def test_copy_loop_partial_element_breaks_and_does_not_insert_next_element(self):
        p1_portion1 = MagicMock()
        p1_portion1.getString.return_value = "portion1"
        p1_portion1.CharColor = -1
        p1_portion1.CharFontName = "Liberation Sans"
        p1_portion1.CharHeight = 10.0
        p1_portion1.CharWeight = 100.0
        p1_portion1.HyperLinkURL = ""

        p1_portion2 = MagicMock()
        p1_portion2.getString.side_effect = RuntimeError("portion2 broke")

        p1 = MagicMock()
        p1.supportsService.return_value = False
        p1.getPropertyValue.return_value = None
        p1.createEnumeration.return_value = _uno_enum([p1_portion1, p1_portion2])

        p2_portion = MagicMock()
        p2_portion.getString.return_value = "p2_text"
        p2 = MagicMock()
        p2.supportsService.return_value = False
        p2.getPropertyValue.return_value = None
        p2.createEnumeration.return_value = _uno_enum([p2_portion])

        src_doc = MagicMock()
        src_doc.getText.return_value.createEnumeration.return_value = _uno_enum([p1, p2])

        control = MagicMock()
        model = MagicMock()
        model.createTextCursor.return_value = MagicMock()
        control.getModel.return_value = model

        inserted_strings: list[str] = []
        def _capture(_model, _cursor, text, char_color=None, **_kw):
            inserted_strings.append(text)

        theme = MagicMock(user_color=1, assistant_color=2)
        with patch("plugin.chatbot.rich_text_paste.focus_preserved", _immediate_focus), \
             patch("plugin.chatbot.rich_text_paste.ChatTheme.resolve", return_value=theme), \
             patch("plugin.chatbot.rich_text_paste._rich_control_bg_color", return_value=0), \
             patch("plugin.chatbot.rich_text_paste.get_control_text_length", return_value=0), \
             patch("plugin.chatbot.rich_text_paste._apply_sidebar_para_margins"), \
             patch("plugin.chatbot.rich_text_paste._scroll_rich_to_tail"), \
             patch("plugin.chatbot.rich_text_paste._insert_string_at_rich_cursor", side_effect=_capture):
            ok, reason = _copy_formatted_from_hidden_doc_to_control(
                src_doc, control, MagicMock(), role="assistant",
            )

        assert ok is False
        assert reason == "element_skipped"
        assert "portion1" in inserted_strings
        assert "p2_text" not in inserted_strings


def test_plain_fallback_text_drops_script_and_unescapes():
    assert _plain_fallback_text("<script>alert(1)</script><p>a &amp; b</p>") == "a & b"


def test_plain_transcript_text_strips_html():
    from plugin.chatbot.rich_text_paste import plain_transcript_text
    from unittest.mock import MagicMock

    session = MagicMock()
    session.messages = [
        {"role": "user", "content": "<b>hello</b>"},
        {"role": "assistant", "content": "<p>done</p>"},
    ]
    plain = plain_transcript_text(session, greeting="<i>Welcome</i>")
    assert "<p>" not in plain
    assert "</p>" not in plain
    assert "<b>" not in plain
    assert "<i>" not in plain
    assert "Welcome" in plain
    assert "You: hello" in plain
    assert "User:" not in plain
    assert "done" in plain


@pytest.mark.skipif(
    is_release_build() or not module_source_contains(rich_text_paste_module, "hidden writer close failed"),
    reason="Stripped release bundle removes logger .debug call sites (scripts/strip_code.py)",
)
def test_hidden_writer_close_failure_is_logged(caplog):
    """CloseVeto-style failure is logged with traceback and does not escape."""
    control = MagicMock()
    control.getModel.return_value = MagicMock(Text="")
    ctx = MagicMock()

    def _doc():
        doc = MagicMock()
        doc.getText.return_value.getString.return_value = "Hi"
        doc.close.side_effect = RuntimeError("hidden close veto")
        return doc

    with caplog.at_level(logging.DEBUG, logger="plugin.chatbot.rich_text_paste"):
        doc = _doc()
        with (
            patch("plugin.chatbot.rich_text_paste.create_hidden_html_writer", return_value=doc),
            patch("plugin.chatbot.rich_text_paste.configure_hidden_writer_for_chat"),
            patch("plugin.chatbot.rich_text_paste.render_messages_to_hidden_doc"),
            patch("plugin.chatbot.rich_text_paste.clear_control"),
            patch("plugin.chatbot.rich_text_paste._copy_formatted_from_hidden_doc_to_control", return_value=(True, None)),
            patch("plugin.chatbot.rich_text_paste._force_rich_full_reformat"),
            patch("plugin.chatbot.rich_text_paste._scroll_rich_to_tail"),
        ):
            assert paint_message_items(ctx, control, [("assistant", "<p>Hi</p>")]) is True
        doc.close.assert_called_once_with(True)

        doc = _doc()
        with (
            patch("plugin.chatbot.rich_text_paste.create_hidden_html_writer", return_value=doc),
            patch("plugin.chatbot.rich_text_paste.configure_hidden_writer_for_chat"),
            patch("plugin.chatbot.rich_text_paste.render_messages_to_hidden_doc"),
            patch("plugin.chatbot.rich_text_paste._copy_formatted_from_hidden_doc_to_control", return_value=(True, None)),
            patch("plugin.chatbot.rich_text_paste._scroll_rich_to_tail"),
            patch("plugin.chatbot.rich_text_paste.get_control_text_length", return_value=0),
        ):
            append_formatted_rows(ctx, control, [("assistant", "<p>Hi</p>")])
        doc.close.assert_called_once_with(True)

        doc = _doc()
        with (
            patch("plugin.chatbot.rich_text_paste.create_hidden_html_writer", return_value=doc),
            patch("plugin.chatbot.rich_text_paste.configure_hidden_writer_for_chat"),
            patch("plugin.chatbot.rich_text_paste.append_rich_text", return_value=True),
            patch("plugin.chatbot.rich_text_paste._copy_formatted_from_hidden_doc_to_control", return_value=(True, None)),
            patch("plugin.chatbot.rich_text_paste.get_control_text_length", return_value=0),
            patch("plugin.chatbot.rich_text_paste._ensure_message_separator"),
        ):
            assert append_formatted_row(ctx, control, "<p>Hi</p>", role="assistant") is True
        doc.close.assert_called_once_with(True)

    close_records = [r for r in caplog.records if "hidden writer close failed" in r.message]
    assert len(close_records) == 3
    assert any("paint_message_items: hidden writer close failed" in r.message for r in close_records)
    assert any("append_formatted_rows: hidden writer close failed" in r.message for r in close_records)
    assert any("append_formatted_row: hidden writer close failed" in r.message for r in close_records)
    for record in close_records:
        assert record.exc_info is not None
        assert record.exc_info[0] is RuntimeError
