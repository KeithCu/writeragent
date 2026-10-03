# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for the Ask-box slash overlay controller (no soffice)."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from plugin.chatbot.send_state import SendEventKind
from plugin.chatbot.slash_commands import KEY_ESCAPE, KEY_RETURN, KEY_TAB, KEY_UP
from plugin.chatbot.slash_popup import (
    SlashPopupController,
    _POPUP_MAX_ROWS,
    _POPUP_ROW_PX,
    _above_ready_rect,
    _create_ask_peer_listbox,
    _is_combo_box,
    _is_parent_local,
    _overlay_height,
    _popup_bounds,
    _printable_key_char,
    _row_index_at_y,
    uses_toolkit_overlay,
)


@pytest.fixture(autouse=True)
def _enable_slash_for_unit_tests(monkeypatch):
    monkeypatch.setattr("plugin.chatbot.slash_popup.ENABLE_SLASH", True)


class _ListBox:
    def __init__(self) -> None:
        self.items: list[str] = []
        self.selected = 0
        self.visible = True
        self.pos = SimpleNamespace(X=4, Y=80, Width=142, Height=60)

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


@pytest.fixture(autouse=True)
def _inline_idle_overlay_show():
    """CI has UNO AsyncCallback so post_to_main_thread queues; unit tests need inline."""

    def _run(fn, *args, **kwargs):
        fn(*args, **kwargs)

    with patch("plugin.framework.queue_executor.post_to_main_thread", side_effect=_run):
        yield


def _controller(lru: list[str] | None = None) -> tuple[SlashPopupController, _ListBox]:
    box = _ListBox()
    query = _Query()
    send = SimpleNamespace(query_control=query, slash_popup=None)
    with patch("plugin.chatbot.slash_popup.load_slash_lru", return_value=lru or []):
        popup = SlashPopupController(box, send, query)
    send.slash_popup = popup
    return popup, box


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
    # Non-toolkit path fills in _refresh_list only. A second addItems doubled rows.
    assert len(box.items) == len(popup.visible_names)


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
    send._append_response = MagicMock()
    with patch("plugin.chatbot.slash_popup.load_slash_lru", return_value=[]):
        popup.on_query_text("/he")
    with patch("plugin.chatbot.slash_commands.record_slash_lru"):
        with patch("plugin.chatbot.dialogs.set_control_text"):
            assert popup.handle_key(KEY_RETURN, 0) is True
    send._append_response.assert_called_once()
    assert "Slash commands:" in send._append_response.call_args[0][0]
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


def test_xdl_menulist_is_treated_as_combo_not_listbox():
    combo = SimpleNamespace(getModel=lambda: SimpleNamespace(getSupportedServiceNames=lambda: ("com.sun.star.awt.UnoControlComboBoxModel",)))
    box = SimpleNamespace(getModel=lambda: SimpleNamespace(getSupportedServiceNames=lambda: ("com.sun.star.awt.UnoControlListBoxModel",)))
    assert _is_combo_box(combo) is True
    assert _is_combo_box(box) is False


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


def test_popup_bounds_are_tall_overlay_above_ask():
    """Visible menu is a multi-row overlay above Ask, not a 14px closed combo."""
    x, y, w, h = _popup_bounds(142, 6)
    assert x == 0
    assert y < 0
    assert w == 142
    assert h == _overlay_height(6)
    assert h > _POPUP_ROW_PX
    assert h >= _POPUP_MAX_ROWS * _POPUP_ROW_PX


def test_open_slash_uses_tall_list_not_closed_combo():
    popup, box = _controller()
    with patch("plugin.chatbot.slash_popup.load_slash_lru", return_value=[]):
        popup.on_query_text("/")
    assert popup.is_open is True
    rows = min(len(popup.visible_names), _POPUP_MAX_ROWS)
    assert box.pos.Height == _overlay_height(rows)
    assert box.pos.Height > 20
    # Parked above Ask (Y=152), covering Ready / lower transcript — not the gap.
    assert box.pos.Y < 128
    assert uses_toolkit_overlay(popup) is False


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
    send._append_response = MagicMock()
    send.dispatch = MagicMock()
    with patch("plugin.chatbot.slash_popup.load_slash_lru", return_value=[]):
        popup.on_query_text("/")
    with patch("plugin.chatbot.slash_commands.record_slash_lru"):
        with patch("plugin.chatbot.dialogs.set_control_text"):
            assert popup.accept_row_at_y(-1) is False
            send._append_response.assert_not_called()
            assert popup.accept_row_at_y(0) is True
    send._append_response.assert_called_once()
    assert "Slash commands:" in send._append_response.call_args[0][0]

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
        assert popup._on_document_key(0, 0, "x") is False
        assert query.text == "/"
        assert popup._on_list_key(0, 0, "h") is True
    assert query.text == "/h"
    assert popup.visible_names == ["help"]


def test_frame_and_toolkit_sources_are_nav_only():
    import inspect

    frame = inspect.getsource(SlashPopupController._attach_frame_keys)
    toolkit = inspect.getsource(SlashPopupController._attach_toolkit_keys)
    listed = inspect.getsource(SlashPopupController._attach_keys)
    assert "_on_document_key(" in frame
    assert "_on_document_key(" in toolkit
    assert "from_overlay=True" not in frame
    assert "from_overlay=True" not in toolkit
    assert "_on_list_key(" in listed
    assert "from_overlay=False" in inspect.getsource(SlashPopupController._on_document_key)
    assert "from_overlay=True" in inspect.getsource(SlashPopupController._on_list_key)


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

    monkeypatch.setattr("plugin.chatbot.slash_popup._screen_bounds_above_ready", _probe)
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
    popup._popup_window = win
    popup._popup_floater = floater
    send.dispatch.reset_mock()
    posted, capture = _capture_posts()
    with patch("plugin.framework.queue_executor.post_to_main_thread", side_effect=capture):
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
    assert event.data == {"has_text": True}


def test_accept_defers_command_so_dispose_is_not_inside_callback():
    popup, _box = _controller()
    send = popup.send_listener
    send.dispatch = MagicMock()
    send._append_response = MagicMock()
    win = MagicMock()
    floater = MagicMock()
    with patch("plugin.chatbot.slash_popup.load_slash_lru", return_value=[]):
        popup.on_query_text("/")
    popup._popup_window = win
    popup._popup_floater = floater
    popup.control = win
    posted, capture = _capture_posts()
    with patch("plugin.framework.queue_executor.post_to_main_thread", side_effect=capture):
        assert popup.handle_key(KEY_RETURN, 0) is True
    win.dispose.assert_not_called()
    assert popup.is_open is True
    send._append_response.assert_not_called()
    assert len(posted) == 1
    fn, args, kwargs = posted[0]
    assert getattr(fn, "__name__", "") == "run_slash_command"
    with patch("plugin.chatbot.slash_commands.record_slash_lru"):
        with patch("plugin.chatbot.dialogs.set_control_text"):
            fn(*args, **kwargs)
    win.dispose.assert_called_once()
    floater.dispose.assert_called_once()
    assert popup.is_open is False
    send._append_response.assert_called_once()


def test_recreate_hide_does_not_dispatch_has_text():
    popup, _box = _controller()
    send = popup.send_listener
    send.dispatch = MagicMock()
    win = MagicMock()
    popup._popup_window = win
    popup._popup_floater = MagicMock()
    popup._open = True
    popup.hide(restore_send=False)
    send.dispatch.assert_not_called()
    win.dispose.assert_called_once()


def test_fill_failure_logs_warning(caplog):
    popup, box = _controller()
    popup._popup_window = object()
    from plugin.chatbot.slash_commands import SLASH_COMMANDS

    popup._matches = [SLASH_COMMANDS[0]]

    def _boom(*_args, **_kwargs):
        raise RuntimeError("addItems")

    box.addItems = _boom
    with caplog.at_level(logging.WARNING, logger="writeragent.slash_popup"):
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
        with caplog.at_level(logging.WARNING, logger="writeragent.slash_popup"):
            popup._attach_frame_keys()
        assert any(
            "frame key handler attach failed" in r.message and r.levelno == logging.WARNING
            for r in caplog.records
        )
    finally:
        if hasattr(awt, "XKeyHandler"):
            delattr(awt, "XKeyHandler")


def test_slash_disabled_when_flag_false(monkeypatch, caplog):
    monkeypatch.setattr("plugin.chatbot.slash_popup.ENABLE_SLASH", False)
    monkeypatch.setattr("plugin.chatbot.slash_popup.SLASH_OV_VERBOSE_DEBUG", False)
    popup, _box = _controller()
    with caplog.at_level(logging.DEBUG, logger="writeragent.slash_popup"):
        popup.on_query_text("/")
    assert popup.is_open is False
    assert not any("[SLASH-OV]" in r.message for r in caplog.records)


def test_ovlog_silent_when_verbose_off(caplog, monkeypatch):
    from plugin.chatbot.slash_popup import _ovdiag, _ovlog

    monkeypatch.setattr("plugin.chatbot.slash_popup.SLASH_OV_VERBOSE_DEBUG", False)
    probe = MagicMock()
    with caplog.at_level(logging.DEBUG, logger="writeragent.slash_popup"):
        _ovlog("query_text entered raw=%r", "/he")
        _ovdiag(probe, "status")
    assert not any("[SLASH-OV]" in r.message for r in caplog.records)
    probe.getPosSize.assert_not_called()
    probe.getImplementationName.assert_not_called()


def test_ovlog_emits_debug_when_verbose(caplog, monkeypatch):
    import plugin.chatbot.slash_popup as slash_popup
    from tests.harness.strip_bundle import module_source_contains

    monkeypatch.setattr(slash_popup, "SLASH_OV_VERBOSE_DEBUG", True)
    with caplog.at_level(logging.DEBUG, logger="writeragent.slash_popup"):
        slash_popup._ovlog("query_text entered raw=%r", "/he")
    if not module_source_contains(slash_popup, "log.debug"):
        return
    matches = [r for r in caplog.records if "[SLASH-OV]" in r.message]
    assert matches
    assert all(r.levelno == logging.DEBUG for r in matches)
    assert any("query_text entered" in r.message for r in matches)
    assert not any(r.levelno >= logging.INFO and "[SLASH-OV]" in r.message for r in caplog.records)


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


def test_parent_local_echo_detected():
    # Box bug: Ask accessible returned (8,360) while PosSize was ~9,361.
    assert _is_parent_local((8, 360), 9, 361, 307) is True
    assert _is_parent_local((940, 400), 9, 361, 307) is False
