# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Unit tests for plugin.chatbot.rich_text_control."""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from plugin.tests.testing_utils import setup_uno_mocks

setup_uno_mocks()

from plugin.chatbot.rich_text_control import (
    _is_automatic_char_color,
    _nudge_rich_view_to_end_inner,
    _set_model_property,
    append_text_chunk,
    clear_control,
    nudge_rich_control_view_to_end,
    truncate_control_from,
)


class TestRichControlHelpers:
    def test_automatic_char_color(self):
        assert _is_automatic_char_color(None)
        assert _is_automatic_char_color(-1)
        assert _is_automatic_char_color(0xFFFFFFFF)
        assert not _is_automatic_char_color(0x2A6099)

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

    def test_content_bounds_placeholder_rect_is_authoritative(self):
        """Layout-provided rect is the sole geometry source; Clear width must not widen it."""
        from types import SimpleNamespace

        from plugin.chatbot.rich_text_control import RICH_CONTROL_EDGE_INSET, _content_bounds_for_rich_control

        ps = MagicMock()
        ps.getPosSize.return_value = SimpleNamespace(X=4, Y=16, Width=142, Height=110)
        root = MagicMock()
        root.getPosSize.return_value = SimpleNamespace(Width=900, Height=500)
        clear = MagicMock()
        clear.getPosSize.return_value = SimpleNamespace(X=108, Y=186, Width=50, Height=15)
        root.getControl.return_value = clear
        bx, by, bw, bh = _content_bounds_for_rich_control(
            root, ps, placeholder_rect=(4, 16, 142, 350),
        )
        assert bx == 4 + RICH_CONTROL_EDGE_INSET
        assert by == 16 + RICH_CONTROL_EDGE_INSET
        assert bw == 142 - 2 * RICH_CONTROL_EDGE_INSET
        assert bh == 350 - 2 * RICH_CONTROL_EDGE_INSET
        assert bw < 900

    def test_apply_rich_control_geometry_updates_dialog_model(self):
        from types import SimpleNamespace

        from plugin.chatbot.rich_text_control import _apply_rich_control_geometry

        model = MagicMock()
        model.PositionX = 36
        model.PositionY = 118
        model.Width = 1043
        model.Height = 94
        rich = MagicMock()
        rich.getModel.return_value = model
        rich.getPosSize.return_value = SimpleNamespace(X=36, Y=118, Width=1043, Height=94)

        changed = _apply_rich_control_geometry(rich, 36, 118, 1043, 300, update_dialog_model=True)

        assert changed
        assert model.Height == 300
        rich.setPosSize.assert_called_once_with(36, 118, 1043, 300, 15)

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


@contextmanager
def _immediate_focus(_ctx):
    yield


class TestAppendTextChunk:
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
             patch("plugin.chatbot.rich_text_control.focus_preserved", _immediate_focus):
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
             patch("plugin.chatbot.rich_text_control.process_events_to_idle") as mock_idle, \
             patch("plugin.chatbot.rich_text_control.focus_preserved", _immediate_focus):
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
             patch("plugin.chatbot.rich_text_control.process_events_to_idle") as mock_idle:
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


class TestStripLegacyAiLabel:
    def test_strips_ai_prefix(self):
        from plugin.chatbot.rich_text import strip_legacy_ai_label

        assert strip_legacy_ai_label("AI: Hello") == "Hello"
        assert strip_legacy_ai_label("  ai:  Hello") == "Hello"

    def test_leaves_user_text(self):
        from plugin.chatbot.rich_text import strip_legacy_ai_label

        assert strip_legacy_ai_label("You: hi") == "You: hi"


class TestSkipLegacyStreamChunk:
    def test_skips_ai_prefix(self):
        from plugin.chatbot.rich_text_control import skip_legacy_assistant_stream_chunk

        assert skip_legacy_assistant_stream_chunk("\nAI: ")
        assert skip_legacy_assistant_stream_chunk("AI:")
        assert skip_legacy_assistant_stream_chunk("\n[Using chat model.]\n")

    def test_allows_real_content(self):
        from plugin.chatbot.rich_text_control import skip_legacy_assistant_stream_chunk

        assert not skip_legacy_assistant_stream_chunk("Here is a real answer with tools.")


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
            widget = MagicMock()
            send.rich_text_widget = widget
            send.session = MagicMock()
            send.session.messages = [{"role": "assistant", "content": "<p>Hi</p>"}]
            send._assistant_stream_start_len = 100

            send.rerender_rich_text_session()

            widget.rerender_last_assistant_if_html.assert_called_once_with(
                send.session,
                100,
            )


class TestRichTextChatWidget:
    def test_widget_delegates_correctly(self):
        from plugin.chatbot.rich_text_control import RichTextChatWidget

        ctx = MagicMock()
        control = MagicMock()
        model = MagicMock()
        control.getModel.return_value = model
        widget = RichTextChatWidget(ctx, control, style_window=None)

        assert widget.model == model

        with patch("plugin.chatbot.rich_text_control.get_control_text_length", return_value=12) as mock_len:
            assert widget.get_text_length() == 12
            mock_len.assert_called_once_with(control)

        with patch("plugin.chatbot.rich_text_control.clear_control") as mock_clear:
            widget.clear()
            mock_clear.assert_called_once_with(control)

        with patch("plugin.chatbot.rich_text_control.truncate_control_from") as mock_trunc:
            widget.truncate(5)
            mock_trunc.assert_called_once_with(control, 5)

        with patch("plugin.chatbot.rich_text_control.nudge_rich_control_view_to_end") as mock_nudge:
            widget.nudge_view_to_end()
            mock_nudge.assert_called_once_with(control, ctx=ctx, style_window=None)

        with patch("plugin.chatbot.rich_text_control.append_text_chunk") as mock_chunk:
            widget.append_chunk("hello", auto_scroll=True)
            mock_chunk.assert_called_once_with(control, "hello", auto_scroll=True, style_window=None, ctx=ctx)

        with patch("plugin.chatbot.rich_text_paste.append_rich_text_via_clipboard") as mock_rich:
            widget.append_rich_message("<b>hi</b>", role="user")
            mock_rich.assert_called_once_with(ctx, control, "<b>hi</b>", role="user", style_window=None, auto_scroll=True, on_after_insert=None)

        with patch("plugin.chatbot.rich_text_paste.append_rich_messages_via_clipboard") as mock_batch:
            items = [("user", "hi")]
            widget.append_rich_messages_batch(items)
            mock_batch.assert_called_once_with(ctx, control, items, style_window=None, batch_chars=16384)

        with patch("plugin.chatbot.rich_text_control._apply_rich_control_style_defaults") as mock_style:
            widget.apply_style_defaults()
            mock_style.assert_called_once_with(control, style_window=None)

    def test_rerender_last_assistant_if_html(self):
        from plugin.chatbot.rich_text_control import RichTextChatWidget

        ctx = MagicMock()
        control = MagicMock()
        widget = RichTextChatWidget(ctx, control, style_window=None)
        session = MagicMock()
        session.messages = [{"role": "assistant", "content": "<p>Hi</p>"}]

        with patch.object(widget, "truncate") as mock_trunc, \
             patch.object(widget, "nudge_view_to_end") as mock_nudge, \
             patch.object(widget, "append_rich_message") as mock_append:
            widget.rerender_last_assistant_if_html(session, 42)

        mock_trunc.assert_called_once_with(42)
        mock_nudge.assert_called_once()
        mock_append.assert_called_once_with("<p>Hi</p>", role="assistant")

    def test_rerender_truncates_from_final_answer_offset(self):
        """stream_start_len must be after search steps (e.g. 500), not after user message (e.g. 100)."""
        from plugin.chatbot.rich_text_control import RichTextChatWidget

        widget = RichTextChatWidget(MagicMock(), MagicMock())
        session = MagicMock()
        session.messages = [{"role": "assistant", "content": "<p>Report</p>"}]

        with patch.object(widget, "truncate") as mock_trunc, \
             patch.object(widget, "nudge_view_to_end"), \
             patch.object(widget, "append_rich_message"):
            widget.rerender_last_assistant_if_html(session, 500)

        mock_trunc.assert_called_once_with(500)

    def test_rerender_skips_plain_assistant_message(self):
        from plugin.chatbot.rich_text_control import RichTextChatWidget

        widget = RichTextChatWidget(MagicMock(), MagicMock())
        session = MagicMock()
        session.messages = [{"role": "assistant", "content": "plain text"}]

        with patch.object(widget, "truncate") as mock_trunc:
            widget.rerender_last_assistant_if_html(session, 10)

        mock_trunc.assert_not_called()

    def test_append_assistant_stream_chunk_skips_legacy_ai(self):
        from plugin.chatbot.rich_text_control import RichTextChatWidget

        widget = RichTextChatWidget(MagicMock(), MagicMock())
        with patch.object(widget, "append_chunk") as mock_chunk:
            assert widget.append_assistant_stream_chunk("AI:") is False
            mock_chunk.assert_not_called()

    def test_render_session_history(self):
        from plugin.chatbot.rich_text_control import RichTextChatWidget

        widget = RichTextChatWidget(MagicMock(), MagicMock())
        session = MagicMock()
        with patch("plugin.chatbot.rich_text_paste.session_history_items", return_value=[("user", "hi")]), \
             patch.object(widget, "clear") as mock_clear, \
             patch.object(widget, "append_rich_messages_batch") as mock_batch:
            widget.render_session_history(session, greeting="Hello")
        mock_clear.assert_called_once()
        mock_batch.assert_called_once_with([("user", "hi")])


class TestLogRichControlContext:
    def setup_method(self):
        import plugin.chatbot.rich_text_control as rtc

        rtc._ENV_SNAPSHOT_LOGGED = False

    def test_includes_phase_and_env_snapshot_once(self):
        import plugin.chatbot.rich_text_control as rtc

        with patch.dict("os.environ", {"XDG_SESSION_DESKTOP": "gnome"}, clear=False), \
             patch.object(rtc.log, "info") as mock_info:
            rtc.log_rich_control_context(MagicMock(), "window_shown", peer=0)
            rtc.log_rich_control_context(MagicMock(), "eager_init", peer=1)
        assert mock_info.call_count == 2
        first = mock_info.call_args_list[0][0][0]
        second = mock_info.call_args_list[1][0][0]
        assert "phase=window_shown" in first
        assert "peer=0" in first
        assert "xdg_session_desktop=gnome" in first
        assert "env=" in first
        assert "xdg_session_desktop=gnome" not in second


class TestRichControlListenerInit:
    def setup_method(self):
        import plugin.chatbot.rich_text_control as rtc

        rtc._CONTROL_INIT_STARTED.clear()
        rtc._ENV_SNAPSHOT_LOGGED = True

    def test_window_shown_no_peer_does_not_init(self):
        from plugin.chatbot.rich_text_control import RichTextControlListener

        root = MagicMock()
        root.getPeer.return_value = None
        listener = RichTextControlListener(MagicMock(), root, MagicMock(), MagicMock())
        with patch("plugin.framework.queue_executor.post_to_main_thread") as mock_post, \
             patch.object(listener, "_begin_deferred_init") as mock_begin:
            listener.on_window_shown(MagicMock())
        mock_post.assert_not_called()
        mock_begin.assert_not_called()

    def test_window_shown_with_peer_begins_init(self):
        from plugin.chatbot.rich_text_control import RichTextControlListener

        root = MagicMock()
        root.getPeer.return_value = MagicMock()
        listener = RichTextControlListener(MagicMock(), root, MagicMock(), MagicMock())
        with patch.object(listener, "_begin_deferred_init") as mock_begin:
            listener.on_window_shown(MagicMock())
        mock_begin.assert_called_once()

    def test_duplicate_init_started_skips_window_shown(self):
        import plugin.chatbot.rich_text_control as rtc
        from plugin.chatbot.rich_text_control import RichTextControlListener

        root = MagicMock()
        root.getPeer.return_value = MagicMock()
        rtc._CONTROL_INIT_STARTED.add(id(root))
        listener = RichTextControlListener(MagicMock(), root, MagicMock(), MagicMock())
        with patch("plugin.framework.queue_executor.post_to_main_thread") as mock_post, \
             patch.object(rtc.log, "warning") as mock_warn:
            listener.on_window_shown(MagicMock())
        mock_post.assert_not_called()
        mock_warn.assert_called_once()

    def test_eager_init_with_peer_begins_deferred_init(self):
        from plugin.chatbot.rich_text_control import RichTextControlListener

        root = MagicMock()
        root.getPeer.return_value = MagicMock()
        listener = RichTextControlListener(MagicMock(), root, MagicMock(), MagicMock())
        with patch.object(listener, "_begin_deferred_init") as mock_begin:
            listener.try_eager_init()
        mock_begin.assert_called_once()
