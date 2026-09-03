# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for the Ask-box slash ListBox controller (no soffice)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from plugin.chatbot.slash_commands import KEY_ESCAPE, KEY_RETURN, KEY_TAB, KEY_UP
from plugin.chatbot.slash_popup import SlashPopupController, _ensure_listbox, _is_combo_box


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
    assert "mock-alpha" in popup.visible_names
    assert popup.selected_name == "help"


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
    assert _ensure_listbox(box, None) is box


def test_chat_panel_xdl_uses_menulist_for_slash_popup():
    """LibreOffice dialog.dtd has dlg:menulist only; dlg:listbox breaks the sidebar."""
    from pathlib import Path

    xdl = Path(__file__).resolve().parents[2] / "extension" / "Dialogs" / "ChatPanelDialog.xdl"
    text = xdl.read_text(encoding="utf-8")
    assert "dlg:listbox" not in text
    assert 'dlg:id="slash_popup"' in text
    assert "dlg:menulist" in text
