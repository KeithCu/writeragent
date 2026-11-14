# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for the Ask-box slash overlay controller (no soffice)."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from plugin.chatbot.send_state import SendEventKind
from plugin.chatbot.slash_commands import KEY_ESCAPE, KEY_RETURN, KEY_TAB, KEY_UP, SlashResult
from plugin.chatbot.slash_popup import (
    SlashPopupController,
    _POPUP_MAX_ROWS,
    _POPUP_ROW_PX,
    _above_ready_rect,
    _create_ask_peer_listbox,
    _hscroll_height,
    _list_height,
    _overlay_height,
    _printable_key_char,
    _row_index_at_y,
)


@pytest.fixture(autouse=True)
def _slash_popup_unit_test_env(monkeypatch):
    # Most tests drive the [mock] rows, like the mock-LLM sidebar harness.
    monkeypatch.setattr("plugin.chatbot.slash_commands._mock_commands_enabled", True)


class _ListBox:
    def __init__(self) -> None:
        self.items: list[str] = []
        self.selected = 0
        self.visible = False
        self.pos = SimpleNamespace(X=4, Y=80, Width=142, Height=60)
        self.disposed = False

    def getItemCount(self) -> int:
        return len(self.items)

    def removeItems(self, start: int, count: int) -> None:
        del self.items[start : start + count]

    def addItems(self, labels, _pos: int) -> None:
        self.items.extend(list(labels))

    def selectItemPos(self, idx: int, _select: bool) -> None:
        self.selected = idx

    def getSelectedItemPos(self) -> int:
        return self.selected

    def getPosSize(self):
        return self.pos

    def setPosSize(self, x, y, w, h, _flags) -> None:
        self.pos = SimpleNamespace(X=x, Y=y, Width=w, Height=h)

    def setVisible(self, visible: bool) -> None:
        self.visible = visible

    def dispose(self) -> None:
        self.disposed = True

    def getModel(self):
        return SimpleNamespace()


class _Query:
    def __init__(self) -> None:
        self.text = ""
        self.pos = SimpleNamespace(X=4, Y=152, Width=142, Height=30)

    def getPosSize(self):
        return self.pos

    def setText(self, text: str) -> None:
        self.text = text

    def getText(self) -> str:
        return self.text

    def getModel(self):
        return SimpleNamespace(Text=self.text)


_orig_create_ask_peer_listbox = _create_ask_peer_listbox


def _stub_create_ask_peer_listbox(
    query_control: Any,
    parent_control: Any = None,
    send_listener: Any = None,
) -> Any:
    if getattr(query_control, "_stub_overlay", False):
        new_box = _ListBox()
        host = _Win()
        query_control._stub_boxes.append(new_box)
        return new_box, host, (0, 0)
    return _orig_create_ask_peer_listbox(query_control, parent_control, send_listener=send_listener)


class _StubBoxProxy:
    def __init__(self, query: _Query) -> None:
        self._default = _ListBox()
        self._query = query

    @property
    def _target(self) -> _ListBox:
        boxes = getattr(self._query, "_stub_boxes", None)
        if boxes:
            return boxes[-1]
        return self._default

    def __getattr__(self, name: str) -> Any:
        return getattr(self._target, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in ("_default", "_query"):
            super().__setattr__(name, value)
        else:
            setattr(self._target, name, value)


@pytest.fixture(autouse=True)
def _inline_idle_overlay_show(monkeypatch):
    """CI has UNO AsyncCallback so post queues; unit tests need inline."""

    def _run(fn, *args, **kwargs):
        fn(*args, **kwargs)

    monkeypatch.setattr("plugin.chatbot.slash_popup._create_ask_peer_listbox", _stub_create_ask_peer_listbox)
    with patch("plugin.framework.queue_executor.main.post", side_effect=_run):
        yield


def _controller(lru: list[str] | None = None) -> tuple[SlashPopupController, Any]:
    placeholder = _ListBox()
    query = _Query()
    query._stub_overlay = True
    query._stub_boxes = []
    send = SimpleNamespace(
        query_control=query,
        slash_popup=None,
        sync_has_text_from_query=MagicMock(),
        cached_doc_type=None,
    )
    with patch("plugin.chatbot.slash_popup.load_slash_lru", return_value=lru or []):
        popup = SlashPopupController(placeholder, send, query)
    send.slash_popup = popup
    return popup, _StubBoxProxy(query)


def test_slash_opens_popup_with_full_list():
    popup, box = _controller()
    with patch("plugin.chatbot.slash_popup.load_slash_lru", return_value=[]):
        popup.on_query_text("/")
    assert popup.is_open is True
    assert box.visible is True
    assert popup.visible_names[0] == "help"
    assert "clear" in popup.visible_names
    assert "mock-alpha" in popup.visible_names
    assert popup.selected_name == "help"
    # Non-toolkit path fills in _fill_visible_list only. A second addItems doubled rows.
    assert len(box.items) == len(popup.visible_names)


def test_product_popup_lists_no_mock_commands(monkeypatch):
    # Keith 2026-10-29: /mock-bravo [mock] showed in a real dev build.
    monkeypatch.setattr("plugin.chatbot.slash_commands._mock_commands_enabled", False)
    popup, _box = _controller(lru=["mock-bravo"])
    with patch("plugin.chatbot.slash_popup.load_slash_lru", return_value=["mock-bravo"]):
        popup.on_query_text("/")
    assert "mock-bravo" not in popup.visible_names
    assert popup.visible_names[:3] == ["help", "clear", "stop"]
    assert "summarize" in popup.visible_names


def test_he_leaves_help_selected():
    popup, _box = _controller()
    with patch("plugin.chatbot.slash_popup.load_slash_lru", return_value=[]):
        popup.on_query_text("/he")
    assert popup.visible_names == ["help"]
    assert popup.selected_name == "help"


def test_enter_accepts_help_without_send():
    popup, _box = _controller()
    send = popup.send_listener
    send.dispatch = MagicMock()
    send.append_output = MagicMock()
    send.clear_ask = MagicMock()
    send.hide_popup = MagicMock(side_effect=popup.hide)
    with patch("plugin.chatbot.slash_popup.load_slash_lru", return_value=[]):
        popup.on_query_text("/he")
    with patch("plugin.chatbot.slash_commands.record_slash_lru"):
        with patch("plugin.chatbot.dialogs.set_control_text"):
            assert popup.handle_key(KEY_RETURN, 0) is True
    send.append_output.assert_called_once()
    assert "Slash commands:" in send.append_output.call_args[0][0]
    send.on_action_performed = MagicMock()
    # Controller consumed Enter; send path is not invoked from handle_key.
    send.on_action_performed.assert_not_called()


def test_esc_dismisses():
    popup, box = _controller()
    with patch("plugin.chatbot.slash_popup.load_slash_lru", return_value=[]):
        popup.on_query_text("/")
    assert popup.handle_key(KEY_ESCAPE) is True
    assert popup.is_open is False
    assert box.visible is False


def test_tab_completes_selected_name():
    popup, _box = _controller()
    query = popup.query_control
    with patch("plugin.chatbot.slash_popup.load_slash_lru", return_value=[]):
        popup.on_query_text("/he")
        assert popup.handle_key(KEY_TAB) is True
    assert query.text == "/help"


def test_up_down_move_selection():
    popup, box = _controller()
    with patch("plugin.chatbot.slash_popup.load_slash_lru", return_value=[]):
        popup.on_query_text("/")
    first = popup.selected_name
    popup.handle_key(KEY_UP)  # wrap to last
    assert popup.selected_name != first
    assert box.selected == len(popup.visible_names) - 1


def test_lru_ranks_mock_higher_next_time():
    popup, _box = _controller(lru=["mock-bravo"])
    with patch("plugin.chatbot.slash_popup.load_slash_lru", return_value=["mock-bravo"]):
        popup.on_query_text("/")
    assert popup.selected_name == "mock-bravo"
    assert popup.visible_names[0] == "mock-bravo"


def test_closed_popup_does_not_steal_enter():
    popup, _box = _controller()
    assert popup.is_open is False
    assert popup.handle_key(KEY_RETURN, 0) is False


def test_chat_panel_xdl_uses_menulist_for_slash_popup():
    """LibreOffice dialog.dtd has dlg:menulist only; dlg:listbox breaks the sidebar."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    # Repo: extension/Dialogs/; make release bundle: Dialogs/ at OXT root.
    candidates = (
        root / "extension" / "Dialogs" / "ChatPanelDialog.xdl",
        root / "Dialogs" / "ChatPanelDialog.xdl",
    )
    xdl = next((p for p in candidates if p.is_file()), None)
    if xdl is None:
        pytest.skip("ChatPanelDialog.xdl not in this tree")
    text = xdl.read_text(encoding="utf-8")
    assert "dlg:listbox" not in text
    assert 'dlg:id="slash_popup"' in text
    assert "dlg:menulist" in text


def test_create_ask_peer_listbox_none_without_peer():
    assert _create_ask_peer_listbox(_Query()) is None
    assert _create_ask_peer_listbox(None) is None


def test_combo_placeholder_is_hidden_and_not_used_as_menu():
    combo = SimpleNamespace(
        getModel=lambda: SimpleNamespace(getSupportedServiceNames=lambda: ("com.sun.star.awt.UnoControlComboBoxModel",)),
        visible=True,
    )
    combo.setVisible = lambda v: setattr(combo, "visible", v)
    query = _Query()
    send = SimpleNamespace(query_control=query, slash_popup=None)
    popup = SlashPopupController(combo, send, query)
    assert combo.visible is False
    assert popup.control is not combo
    with patch("plugin.chatbot.slash_popup.load_slash_lru", return_value=[]):
        popup.on_query_text("/")
    assert combo.visible is False
    assert popup.is_open is False


def test_row_index_at_y_ignores_chrome():
    assert _row_index_at_y(0, 6) == 0
    assert _row_index_at_y(_POPUP_ROW_PX, 6) == 1
    assert _row_index_at_y(-1, 6) is None
    assert _row_index_at_y(400, 6) is None


def test_click_row_accepts_command_chrome_does_not_dump_help():
    popup, _box = _controller()
    send = popup.send_listener
    send.append_output = MagicMock()
    send.clear_ask = MagicMock()
    send.hide_popup = MagicMock(side_effect=popup.hide)
    send.dispatch = MagicMock()
    with patch("plugin.chatbot.slash_popup.load_slash_lru", return_value=[]):
        popup.on_query_text("/")
    with patch("plugin.chatbot.slash_commands.record_slash_lru"):
        with patch("plugin.chatbot.dialogs.set_control_text"):
            assert popup.accept_row_at_y(-1) is False
            send.append_output.assert_not_called()
            assert popup.accept_row_at_y(0) is True
    send.append_output.assert_called_once()
    assert "Slash commands:" in send.append_output.call_args[0][0]


def test_enter_on_exact_explain_runs_not_completes_draft():
    from plugin.chatbot.slash_commands import filter_slash_commands

    popup, _box = _controller()
    send = popup.send_listener
    query = popup.query_control
    query.text = "/explain"
    send.dispatch = MagicMock()
    send.sync_has_text_from_query = MagicMock()
    send.append_output = MagicMock()
    send.clear_ask = MagicMock()
    send.hide_popup = MagicMock(side_effect=popup.hide)
    popup._open = True
    popup._matches = filter_slash_commands("/explain")
    popup._selected = 0
    with patch("plugin.chatbot.slash_commands.record_slash_lru"):
        with patch("plugin.chatbot.slash_popup._defer_to_next_turn", side_effect=lambda fn, *a: fn()):
            with patch("plugin.chatbot.slash_popup.run_slash_command", return_value=SlashResult.CONTINUE_SEND) as run_cmd:
                with patch("plugin.chatbot.dialogs.set_control_text") as set_text:
                    popup.accept_selected()
                    assert run_cmd.call_args.args[0].name == "explain"
                    run_cmd.assert_called_once_with(run_cmd.call_args.args[0], send, "", from_popup=False)
                    set_text.assert_not_called()
                    send.sync_has_text_from_query.assert_called_once()
                    send.dispatch.assert_called_once()
                    from plugin.chatbot.send_state import SendEventKind

                    assert send.dispatch.call_args[0][0].kind == SendEventKind.SEND_CLICKED


def test_enter_exact_continue_send_starts_send_when_has_text_was_false():
    """QA F1: one Enter after exact /preset must sync Ask then SEND_CLICKED."""
    from plugin.chatbot.send_state import SendButtonState, SendEvent, SendEventKind, SendMode, next_state

    popup, _box = _controller()
    send = popup.send_listener
    query = popup.query_control
    query.text = "/summarize"
    send.sidebar_state = SimpleNamespace(
        send=SendButtonState(SendMode.IDLE, has_text=False, has_audio=False, audio_supported=True)
    )

    def sync_has_text() -> None:
        send.sidebar_state = SimpleNamespace(
            send=next_state(
                send.sidebar_state.send,
                SendEvent(SendEventKind.TEXT_UPDATED, has_text=True),
            ).state
        )

    send.sync_has_text_from_query = sync_has_text
    events: list[SendEvent] = []

    def capture(event: SendEvent) -> None:
        events.append(event)
        if event.kind == SendEventKind.SEND_CLICKED:
            tr = next_state(send.sidebar_state.send, event)
            send.sidebar_state = SimpleNamespace(send=tr.state)

    send.dispatch = capture
    send.append_output = MagicMock()
    send.clear_ask = MagicMock()
    send.hide_popup = MagicMock(side_effect=popup.hide)
    popup._open = True
    from plugin.chatbot.slash_commands import filter_slash_commands

    popup._matches = filter_slash_commands("/summarize")
    popup._selected = 0
    with patch("plugin.chatbot.slash_commands.record_slash_lru"):
        with patch("plugin.chatbot.slash_popup._defer_to_next_turn", side_effect=lambda fn, *a: fn()):
            with patch("plugin.chatbot.slash_popup.run_slash_command", return_value=SlashResult.CONTINUE_SEND):
                popup.accept_selected()

    assert [event.kind for event in events] == [SendEventKind.SEND_CLICKED]
    assert send.sidebar_state.send.is_busy is True


def test_enter_on_exact_translate_runs_with_empty_arg():
    from plugin.chatbot.slash_commands import filter_slash_commands

    popup, _box = _controller()
    send = popup.send_listener
    query = popup.query_control
    query.text = "/translate"
    send.dispatch = MagicMock()
    send.append_output = MagicMock()
    send.clear_ask = MagicMock()
    send.hide_popup = MagicMock(side_effect=popup.hide)
    popup._open = True
    popup._matches = filter_slash_commands("/translate")
    popup._selected = 0
    with patch("plugin.chatbot.slash_commands.record_slash_lru"):
        with patch("plugin.chatbot.slash_popup._defer_to_next_turn", side_effect=lambda fn, *a: fn()):
            with patch("plugin.chatbot.slash_popup.run_slash_command", return_value=SlashResult.CONTINUE_SEND) as run_cmd:
                with patch("plugin.chatbot.dialogs.set_control_text") as set_text:
                    popup.accept_selected()
                    assert run_cmd.call_args.args[0].name == "translate"
                    run_cmd.assert_called_once_with(run_cmd.call_args.args[0], send, "", from_popup=False)
                    set_text.assert_not_called()


def test_enter_on_summarize_completes_ask_draft_not_rewrite():
    popup, _box = _controller()
    send = popup.send_listener
    query = popup.query_control
    send.dispatch = MagicMock()
    send.append_output = MagicMock()
    send.clear_ask = MagicMock()
    send.hide_popup = MagicMock(side_effect=popup.hide)
    send.slash_run_rewrite = MagicMock()
    with patch("plugin.chatbot.slash_popup.load_slash_lru", return_value=[]):
        popup.on_query_text("/su")
    with patch("plugin.chatbot.slash_commands.record_slash_lru"):
        with patch("plugin.chatbot.dialogs.set_control_text") as set_text:
            assert popup.handle_key(KEY_RETURN, 0) is True
            set_text.assert_called_once_with(query, "/summarize ")
    send.slash_run_rewrite.assert_not_called()
    send.append_output.assert_not_called()


def test_tone_with_argument_closes_popup_and_does_not_consume_enter():
    popup, _box = _controller()
    query = popup.query_control
    query.text = "/tone very formal"
    send = popup.send_listener
    send.dispatch = MagicMock()
    with patch("plugin.chatbot.slash_popup.load_slash_lru", return_value=[]):
        assert popup.on_query_text("/tone very formal") is False
    assert popup.is_open is False
    assert popup.handle_key(KEY_RETURN, 0) is False
    send.dispatch.assert_called_once()
    event = send.dispatch.call_args[0][0]
    assert event.kind is SendEventKind.TEXT_UPDATED


def test_popup_list_labels_use_short_popup_formatter():
    popup, box = _controller()
    with patch("plugin.chatbot.slash_popup.load_slash_lru", return_value=[]):
        popup.on_query_text("/t")
    tone_row = next(label for label in box.items if label.startswith("/tone"))
    assert "<" not in tone_row
    assert " — " in tone_row


def test_printable_key_char_skips_controls():
    assert _printable_key_char("h") == "h"
    assert _printable_key_char("\n") is None
    assert _printable_key_char(None) is None


def test_overlay_printable_feeds_ask_and_filters_to_help():
    popup, _box = _controller()
    query = popup.query_control
    query.text = "/"
    with patch("plugin.chatbot.slash_popup.load_slash_lru", return_value=[]):
        popup.on_query_text("/")
        assert popup.handle_key(0, 0, "h", from_overlay=True) is True
        assert popup.handle_key(0, 0, "e", from_overlay=True) is True
    assert query.text == "/he"
    assert popup.visible_names == ["help"]


def test_ask_path_printable_does_not_insert():
    popup, _box = _controller()
    query = popup.query_control
    query.text = "/"
    with patch("plugin.chatbot.slash_popup.load_slash_lru", return_value=[]):
        popup.on_query_text("/")
        assert popup.handle_key(0, 0, "h") is False
    assert query.text == "/"


def test_document_key_handler_does_not_steal_printable():
    """Frame and toolkit handlers are nav-only. The listbox listener inserts."""
    popup, _box = _controller()
    query = popup.query_control
    query.text = "/"
    with patch("plugin.chatbot.slash_popup.load_slash_lru", return_value=[]):
        popup.on_query_text("/")
        assert popup.handle_key(0, 0, "x", from_overlay=False) is False
        assert query.text == "/"
        assert popup.handle_key(0, 0, "h", from_overlay=True) is True
    assert query.text == "/h"
    assert popup.visible_names == ["help"]


def test_slash_key_handler_key_pressed_behaviour() -> None:
    from plugin.chatbot.slash_popup import _make_slash_key_handler

    calls: list[tuple[int, int, str | None, bool]] = []

    class FakeHost:
        def handle_key(self, code: int, mods: int, ch: str | None, *, from_overlay: bool) -> int:
            calls.append((code, mods, ch, from_overlay))
            return 1

    host = FakeHost()
    handler = _make_slash_key_handler(host, "frame")
    ev = SimpleNamespace(KeyCode=1280, Modifiers=0, KeyChar="\x1b")

    result = handler.keyPressed(ev)
    assert result is True
    assert type(result) is bool
    assert calls == [(1280, 0, "\x1b", False)]

    def _failing_handle_key(*args: Any, **kwargs: Any) -> int:
        raise ValueError("broken key handler")

    host.handle_key = _failing_handle_key  # type: ignore[method-assign]
    assert handler.keyPressed(ev) is False


def test_show_matches_repositions_only_before_show():
    popup, box = _controller()
    seen_visible: list[bool] = []
    orig = popup.reposition

    def _wrapped() -> None:
        seen_visible.append(box.visible)
        orig()

    popup.reposition = _wrapped  # type: ignore[method-assign]
    with patch("plugin.chatbot.slash_popup.load_slash_lru", return_value=[]):
        popup.on_query_text("/")
    assert seen_visible == [False]
    assert box.visible is True


def test_reposition_does_not_probe_screen_bounds(monkeypatch):
    calls = {"n": 0}

    def _probe(*_args, **_kwargs):
        calls["n"] += 1
        return None

    monkeypatch.setattr("plugin.chatbot.slash_popup._overlay_offset", _probe)
    popup, _box = _controller()
    popup.reposition()
    with patch("plugin.chatbot.slash_popup.load_slash_lru", return_value=[]):
        popup.on_query_text("/")
    assert calls["n"] == 0


def _capture_posts():
    posted: list[tuple] = []

    def _capture(fn, *args, **kwargs):
        posted.append((fn, args, kwargs))

    return posted, _capture


def test_esc_defers_hide_and_restores_send_for_slash_draft():
    popup, _box = _controller()
    query = popup.query_control
    query.text = "/he"
    send = popup.send_listener
    send.dispatch = MagicMock()
    win = MagicMock()
    floater = MagicMock()
    with patch("plugin.chatbot.slash_popup.load_slash_lru", return_value=[]):
        popup.on_query_text("/he")
    popup.control = win
    popup._popup_floater = floater
    send.dispatch.reset_mock()
    posted, capture = _capture_posts()
    with patch("plugin.framework.queue_executor.main.post", side_effect=capture):
        assert popup.handle_key(KEY_ESCAPE) is True
    win.dispose.assert_not_called()
    floater.dispose.assert_not_called()
    assert popup.is_open is True
    assert len(posted) == 1
    fn, args, kwargs = posted[0]
    assert fn.__name__ == "hide"
    fn(*args, **kwargs)
    win.dispose.assert_called_once()
    floater.dispose.assert_called_once()
    assert popup.is_open is False
    assert query.text == "/he"
    event = send.dispatch.call_args[0][0]
    assert event.kind is SendEventKind.TEXT_UPDATED
    assert event.has_text is True


def test_accept_defers_command_so_dispose_is_not_inside_callback():
    popup, _box = _controller()
    send = popup.send_listener
    send.dispatch = MagicMock()
    send.append_output = MagicMock()
    send.clear_ask = MagicMock()
    send.hide_popup = MagicMock(side_effect=popup.hide)
    win = MagicMock()
    floater = MagicMock()
    with patch("plugin.chatbot.slash_popup.load_slash_lru", return_value=[]):
        popup.on_query_text("/")
    popup.control = win
    popup._popup_floater = floater
    posted, capture = _capture_posts()
    with patch("plugin.framework.queue_executor.main.post", side_effect=capture):
        assert popup.handle_key(KEY_RETURN, 0) is True
    win.dispose.assert_not_called()
    assert popup.is_open is True
    send.append_output.assert_not_called()
    assert len(posted) == 1
    fn, args, kwargs = posted[0]
    assert callable(fn)
    with patch("plugin.chatbot.slash_commands.record_slash_lru"):
        with patch("plugin.chatbot.dialogs.set_control_text"):
            fn()
    win.dispose.assert_called_once()
    floater.dispose.assert_called_once()
    assert popup.is_open is False
    send.append_output.assert_called_once()
    assert "Slash commands:" in send.append_output.call_args[0][0]


def test_query_text_post_timeout_does_not_run_inline(caplog):
    """TimeoutError from post must not hide or show on this stack.

    Ask textChanged used to catch the timeout and call hide / _show_matches
    inline, which deadlocks VCL. _defer_to_next_turn only logs.
    """
    popup, box = _controller()
    popup.hide = MagicMock()
    popup._show_matches = MagicMock()

    def _timeout(*_args, **_kwargs):
        raise TimeoutError("marshal post timed out")

    with caplog.at_level(logging.WARNING, logger="plugin.chatbot.slash_popup"):
        with patch(
            "plugin.framework.queue_executor.main.post",
            side_effect=_timeout,
        ):
            popup.on_query_text("hello")  # prefix is None → defer hide
            popup.on_query_text("/zzz-not-a-command")  # no command starts with zzz → defer hide
            popup.on_query_text("/")  # matches → defer _show_matches
    popup.hide.assert_not_called()
    popup._show_matches.assert_not_called()
    assert popup.is_open is False
    assert box.visible is False
    deferred = [r for r in caplog.records if "could not defer main-thread turn" in r.message]
    assert len(deferred) == 3
    assert all(r.levelno == logging.WARNING for r in deferred)


def test_recreate_hide_does_not_dispatch_has_text():
    popup, _box = _controller()
    send = popup.send_listener
    send.dispatch = MagicMock()
    win = MagicMock()
    popup.control = win
    popup._popup_floater = MagicMock()
    popup._open = True
    popup.hide(restore_send=False)
    send.dispatch.assert_not_called()
    win.dispose.assert_called_once()


def test_fill_failure_logs_warning(caplog):
    popup, box = _controller()
    popup.control = box
    popup._popup_floater = object()
    from plugin.chatbot.slash_commands import SLASH_COMMANDS

    popup._matches = [SLASH_COMMANDS[0]]

    def _boom(*_args, **_kwargs):
        raise RuntimeError("addItems")

    box.addItems = _boom
    with caplog.at_level(logging.WARNING, logger="plugin.chatbot.slash_popup"):
        popup._fill_visible_list()
    assert any("fill addItems failed" in r.message and r.levelno == logging.WARNING for r in caplog.records)


def test_frame_key_attach_failure_logs_warning(caplog):
    import com.sun.star.awt as awt

    class XKeyHandler:
        pass

    awt.XKeyHandler = XKeyHandler
    try:
        popup, _box = _controller()
        controller = MagicMock()
        controller.addKeyHandler.side_effect = RuntimeError("attach")
        popup.send_listener.frame = SimpleNamespace(getController=lambda: controller)
        with caplog.at_level(logging.WARNING, logger="plugin.chatbot.slash_popup"):
            popup._attach_frame_keys()
        assert any(
            "frame key handler attach failed" in r.message and r.levelno == logging.WARNING
            for r in caplog.records
        )
    finally:
        if hasattr(awt, "XKeyHandler"):
            delattr(awt, "XKeyHandler")


def test_slash_disabled_when_flag_false(monkeypatch):
    monkeypatch.setattr("plugin.chatbot.slash_popup.ENABLE_SLASH", False)
    popup, _box = _controller()
    popup.on_query_text("/")
    assert popup.is_open is False


class _R:
    def __init__(self, x, y, w, h):
        self.X, self.Y, self.Width, self.Height = x, y, w, h


def test_no_status_falls_back_above_ask():
    qr = _R(28, 1853, 1001, 206)
    x, y, w, h = _above_ready_rect(qr, 6, status_top=None)
    assert (x, w) == (28, 1001)
    assert h == _overlay_height(6)
    assert y == 1853 - h - 2


def test_above_ready_uses_status_top():
    qr = _R(28, 1853, 1001, 206)
    x, y, w, h = _above_ready_rect(qr, 6, status_top=1678)
    assert y == 1678 - _overlay_height(6) - 2
    assert y + h <= 1678


class _Acc:
    def __init__(self, xy):
        self._xy = xy

    def getLocationOnScreen(self):
        return SimpleNamespace(X=self._xy[0], Y=self._xy[1])


class _Win:
    """AWT window/peer stand-in with an accessible screen location."""

    def __init__(self, xy=None, *, parent=None):
        self.xy = xy
        self.parent = parent
        self.toolkit: Any = None
        self.min_row_px: int | None = None
        self.origin: Any = None
        self.pos: list[tuple] = []
        self.disposed = False
        self.visible = False

    def setVisible(self, visible: bool) -> None:
        self.visible = visible

    def getAccessibleContext(self):
        return _Acc(self.xy) if self.xy is not None else None

    def getToolkit(self):
        return self.toolkit

    def getMinimumSize(self, _cols, rows):
        px = self.min_row_px
        return SimpleNamespace(Width=0, Height=2 + px * rows) if px else None

    def setPosSize(self, x, y, w, h, flags):
        self.pos.append((x, y, w, h, flags))

    def dispose(self):
        self.disposed = True


class _Toolkit:
    def __init__(self, host_xy, list_xy=(0, 0)):
        self.descs: list = []
        self.host_xy = host_xy
        self.list_xy = list_xy

    def createWindow(self, desc):
        self.descs.append(desc)
        if desc.WindowServiceName == "listbox":
            return _Win(self.list_xy)
        return _Win(self.host_xy)


class _Ctl:
    def __init__(self, peer, rect):
        self._peer = peer
        self._rect = rect

    def getPeer(self):
        return self._peer

    def getPosSize(self):
        return self._rect

    def getAccessibleContext(self):
        # UnoControl keeps XAccessible on LibreOffice 26.2; peers do not.
        return self._peer.getAccessibleContext()


def _overlay_parts(monkeypatch, host_xy, ask_xy=(1010, 951)):
    monkeypatch.setattr(
        "plugin.chatbot.slash_popup._awt_window_constants",
        lambda: (_R, SimpleNamespace, 0, 3, 16),
    )
    tk = _Toolkit(host_xy)
    dialog_peer = _Win((1000, 100))
    dialog_peer.toolkit = tk
    ask = _Ctl(_Win(ask_xy), _R(10, 851, 400, 60))
    dialog = _Ctl(dialog_peer, _R(0, 0, 420, 950))
    return tk, ask, dialog


def test_overlay_host_is_borderless_floatingwindow(monkeypatch):
    # Keith 2026-10-29: "window"+TOP was a decorated soffice.bin WM window
    # (kf6/qt6 ignore NODECORATION for WorkWindow). floatingwindow with no
    # attributes is a VCL overlap window: no system frame, no title bar.
    tk, ask, dialog = _overlay_parts(monkeypatch, host_xy=(10, 51))
    created = _create_ask_peer_listbox(ask, dialog)
    assert created is not None
    host_desc, list_desc = tk.descs
    assert host_desc.WindowServiceName == "floatingwindow"
    assert host_desc.WindowAttributes == 0
    assert (host_desc.Bounds.X, host_desc.Bounds.Y) == (0, 0)
    assert host_desc.Parent is dialog.getPeer()
    assert list_desc.WindowServiceName == "listbox"
    assert list_desc.Parent is created[1]
    # Ask sits at screen (1010, 951) = dialog-local (10, 851) + offset + host origin.
    assert created[2] == (1010 - 10 - 10, 951 - 51 - 851)


def test_overlay_without_position_is_disposed(monkeypatch):
    tk, ask, dialog = _overlay_parts(monkeypatch, host_xy=None)
    assert _create_ask_peer_listbox(ask, dialog) is None
    assert len(tk.descs) == 2


def test_reposition_applies_overlay_offset_and_measured_height():
    popup, box = _controller()
    with patch("plugin.chatbot.slash_popup.load_slash_lru", return_value=[]):
        popup.on_query_text("/")
    rows = min(len(popup._matches), _POPUP_MAX_ROWS)
    host = _Win()
    lst = _Win()
    lst.min_row_px = 18
    popup.control = lst
    popup._popup_floater = host
    popup._ask_origin = (990, -850)
    qr = popup.query_control.getPosSize()
    popup.reposition()
    h = 2 + 18 * rows
    x, y, w, _h = _above_ready_rect(qr, rows, status_top=None, height=h)
    assert host.pos[-1][:4] == (x + 990, y - 850, w, h)
    assert lst.pos[-1][:4] == (0, 0, w, h)
    assert popup._row_px == 18
    assert _row_index_at_y(19, rows, popup._row_px) == 1


def test_list_height_measures_rows_and_falls_back():
    lst = SimpleNamespace(getMinimumSize=lambda _c, n: SimpleNamespace(Height=2 + 18 * n))
    assert _list_height(lst, 6) == 110  # qt6 box: 18px rows; old estimate 90 clipped
    assert _list_height(lst, 99) == 110
    assert _list_height(SimpleNamespace(), 6) == _overlay_height(6)
    bad = SimpleNamespace(getMinimumSize=lambda _c, _n: SimpleNamespace(Height=0))
    assert _list_height(bad, 2) == _overlay_height(2)


def test_toolkit_key_handler_registered_at_panel_creation():
    """Esc must reach us before the sidebar FocusManager, which also listens
    app-wide and moves focus to the panel title. App key listeners run in
    registration order, so ours is added once at panel creation and kept
    until dispose; hide must not drop it."""
    toolkit = MagicMock()
    peer = SimpleNamespace(getToolkit=lambda: toolkit)
    query = SimpleNamespace(getPeer=lambda: peer, getText=lambda: "")
    popup = SlashPopupController(_ListBox(), MagicMock(), query)
    toolkit.addKeyHandler.assert_called_once()
    handler = toolkit.addKeyHandler.call_args.args[0]
    popup.hide(restore_send=False)
    toolkit.removeKeyHandler.assert_not_called()
    popup.dispose()
    toolkit.removeKeyHandler.assert_called_once_with(handler)


def test_toolkit_key_handler_not_registered_when_slash_disabled(monkeypatch):
    monkeypatch.setattr("plugin.chatbot.slash_popup.ENABLE_SLASH", False)
    toolkit = MagicMock()
    peer = SimpleNamespace(getToolkit=lambda: toolkit)
    query = SimpleNamespace(getPeer=lambda: peer, getText=lambda: "")
    SlashPopupController(_ListBox(), MagicMock(), query)
    toolkit.addKeyHandler.assert_not_called()


class _ProbeCtl:
    """UnoControlFixedText stand-in: accessible at its peer parent's origin."""

    def __init__(self, made):
        self.made = made
        self.parent = None
        self.disposed = False
        made.append(self)

    def setModel(self, _model):
        pass

    def createPeer(self, _toolkit, parent):
        self.parent = parent

    def setPosSize(self, *_args):
        pass

    def getAccessibleContext(self):
        assert self.parent is not None
        return _Acc(self.parent.origin)

    def dispose(self):
        self.disposed = True


def test_overlay_offset_without_peer_accessibility(monkeypatch):
    # CI 40978901270 (LibreOffice 26.2): toolkit peers have no
    # getAccessibleContext, so the host origin came back None and the popup
    # never opened. A UnoControl inside the host still reports it.
    tk, ask, dialog = _overlay_parts(monkeypatch, host_xy=None)
    made: list = []

    def create(name, _ctx):
        return _ProbeCtl(made) if name.endswith("FixedText") else object()

    ctx = SimpleNamespace(getServiceManager=lambda: SimpleNamespace(createInstanceWithContext=create))
    host = _Win(None)
    host.origin = (10, 51)
    host.toolkit = tk
    from plugin.chatbot.slash_popup import _overlay_offset

    assert _overlay_offset(host, ask, _R(10, 851, 400, 60), ctx) == (1010 - 10 - 10, 951 - 51 - 851)
    assert len(made) == 1 and made[0].parent is host and made[0].disposed


class _VclList:
    """VCLXListBox.getMinimumSize stand-in: ListBox::CalcBlockSize semantics."""

    def __init__(self, row_px: int, text_w: int, bar: int, border: int = 2):
        self.row_px, self.text_w, self.bar, self.border = row_px, text_w, bar, border

    def getMinimumSize(self, cols, rows):
        h = rows * self.row_px
        w = cols * 8 if cols else self.text_w
        if w < self.text_w:
            h += self.bar
        return SimpleNamespace(Width=w + 2 * self.border, Height=h + 2 * self.border)


def test_hscroll_height_only_when_rows_wider_than_list():
    # Keith 2026-11-03: "/tr" -> one /translate row, half hidden by the HScroll.
    wide = _VclList(row_px=36, text_w=900, bar=24)  # HiDPI-ish
    assert _hscroll_height(wide, 1, 1, 700) == 24
    narrow = _VclList(row_px=18, text_w=300, bar=12)
    assert _hscroll_height(narrow, 1, 1, 400) == 0
    # 7 matches in 6 rows: the VScroll eats width, so 395px text overflows 400.
    assert _hscroll_height(narrow, 6, 7, 310) == 12
    assert _hscroll_height(SimpleNamespace(), 1, 1, 400) == 0


def test_single_wide_match_list_is_tall_enough_for_row_and_hscroll():
    popup, _box = _controller()
    with patch("plugin.chatbot.slash_popup.load_slash_lru", return_value=[]):
        popup.on_query_text("/")
    popup._matches = popup._matches[:1]
    host = _Win()
    lst = _VclList(row_px=18, text_w=5000, bar=14)
    popup.control = lst
    popup._popup_floater = host
    popup._ask_origin = (0, 0)
    popup.reposition()
    assert host.pos[-1][3] == 18 + 4 + 14
    assert popup._row_px == 18 + 4


def test_show_matches_fills_before_reposition():
    popup, _box = _controller()
    order: list[str] = []
    fill, repos = popup._fill_visible_list, popup.reposition
    popup._fill_visible_list = lambda: (order.append("fill"), fill())[1]  # type: ignore[method-assign]
    popup.reposition = lambda: (order.append("reposition"), repos())[1]  # type: ignore[method-assign]
    with patch("plugin.chatbot.slash_popup.load_slash_lru", return_value=[]):
        popup.on_query_text("/")
    assert order[:2] == ["fill", "reposition"]
