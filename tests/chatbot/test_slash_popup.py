# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for the Ask-box slash ListBox controller (no soffice)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from plugin.chatbot.slash_commands import KEY_ESCAPE, KEY_RETURN, KEY_TAB, KEY_UP
from plugin.chatbot.slash_popup import (
    SlashPopupController,
    _POPUP_CLOSED_PX,
    _POPUP_MAX_ROWS,
    _RUNTIME_LIST_NAME,
    _apply_dropdown_model,
    _ensure_listbox,
    _is_combo_box,
    _overlay_height,
    _toggle_list_popup,
    is_dropdown_overlay,
)


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

    def setDropDownLineCount(self, n: int) -> None:
        self.line_count = n


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


def test_apply_dropdown_model_is_overlay_not_in_layout_list():
    """Completion control must be a VCL dropdown overlay, not a multi-row dialog list."""
    model = SimpleNamespace(Dropdown=False, LineCount=0, Tabstop=True, Border=0, MultiSelection=True)
    _apply_dropdown_model(model)
    assert model.Dropdown is True
    assert model.LineCount == _POPUP_MAX_ROWS
    assert model.Tabstop is False
    assert model.MultiSelection is False
    assert is_dropdown_overlay(SimpleNamespace(getModel=lambda: model)) is True
    assert is_dropdown_overlay(SimpleNamespace(Dropdown=False)) is False


def test_open_slash_parks_closed_combo_not_multirow_in_gap():
    """The dialog control is one line in the transcript, not a crushed Ready/Ask list."""
    popup, box = _controller()
    with patch("plugin.chatbot.slash_popup.load_slash_lru", return_value=[]):
        popup.on_query_text("/")
    assert popup.is_open is True
    assert box.pos.Height == _POPUP_CLOSED_PX
    # Ask field is at Y=152. A multi-row in-flow list sat at ~118 in the gap.
    # The closed combo anchors above the overlay so VCL drops over the chat.
    assert box.pos.Y < 128
    assert box.pos.Y == 152 - _overlay_height(min(len(popup.visible_names), _POPUP_MAX_ROWS)) - _POPUP_CLOSED_PX - 2


def test_ensure_listbox_inserts_dropdown_overlay():
    created: dict[str, object] = {}

    class _DlgModel:
        def createInstance(self, _name: str):
            model = SimpleNamespace(
                Name="",
                Dropdown=False,
                LineCount=0,
                Tabstop=True,
                Border=0,
                PositionX=0,
                PositionY=0,
                Width=0,
                Height=0,
            )
            created["model"] = model
            return model

        def insertByName(self, name: str, model: object) -> None:
            created["inserted"] = name
            created["model"] = model

    class _Dlg:
        def __init__(self) -> None:
            self._model = _DlgModel()

        def getModel(self):
            return self._model

        def getControl(self, name: str):
            if name == _RUNTIME_LIST_NAME and created.get("inserted"):
                return SimpleNamespace(getModel=lambda: created["model"], setVisible=lambda _v: None)
            return None

    dlg = _Dlg()
    combo = SimpleNamespace(
        getModel=lambda: SimpleNamespace(
            getSupportedServiceNames=lambda: ("com.sun.star.awt.UnoControlComboBoxModel",)
        ),
        setVisible=lambda _v: None,
    )
    query = _Query()
    query.getContext = lambda: dlg  # type: ignore[method-assign]
    created_ctrl = _ensure_listbox(combo, query)
    assert created.get("inserted") == _RUNTIME_LIST_NAME
    model = created["model"]
    assert getattr(model, "Dropdown") is True
    assert getattr(model, "Height") == _POPUP_CLOSED_PX
    assert is_dropdown_overlay(created_ctrl) is True


def test_toggle_list_popup_uses_accessible_togglepopup():
    calls: list[int] = []

    class _Acc:
        def getAccessibleActionCount(self) -> int:
            return 1

        def getAccessibleActionDescription(self, idx: int) -> str:
            return "togglePopup"

        def doAccessibleAction(self, idx: int) -> bool:
            calls.append(idx)
            return True

    ctrl = SimpleNamespace(getAccessibleContext=lambda: _Acc())
    assert _toggle_list_popup(ctrl) is True
    assert calls == [0]


def test_on_query_text_opens_dropdown_overlay():
    popup, _box = _controller()
    with patch("plugin.chatbot.slash_popup.load_slash_lru", return_value=[]):
        with patch("plugin.chatbot.slash_popup._toggle_list_popup", return_value=True) as toggle:
            popup.on_query_text("/")
            assert toggle.called
    assert popup.is_open is True
    assert popup._overlay_open is True
    with patch("plugin.chatbot.slash_popup._toggle_list_popup", return_value=True) as toggle_hide:
        popup.hide()
        assert toggle_hide.called
    assert popup._overlay_open is False
