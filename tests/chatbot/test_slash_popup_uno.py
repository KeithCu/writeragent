# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Native UNO coverage for the Ask-box slash ListBox (no Packet G audio)."""

from __future__ import annotations

from plugin.testing_runner import native_test


@native_test
def test_slash_popup_listbox_filter_and_keys(ctx):
    """Drive SlashPopupController on a live UnoControlListBox (throwaway dialog)."""
    from plugin.chatbot.slash_commands import KEY_ESCAPE, KEY_RETURN
    from plugin.chatbot.slash_popup import SlashPopupController

    smgr = ctx.getServiceManager()
    dlg_model = smgr.createInstanceWithContext("com.sun.star.awt.UnoControlDialogModel", ctx)
    dlg_model.PositionX = 20
    dlg_model.PositionY = 20
    dlg_model.Width = 160
    dlg_model.Height = 80
    dlg_model.Title = "slash-popup-test"

    query_model = dlg_model.createInstance("com.sun.star.awt.UnoControlEditModel")
    query_model.Name = "query"
    query_model.PositionX = 4
    query_model.PositionY = 50
    query_model.Width = 150
    query_model.Height = 24
    dlg_model.insertByName("query", query_model)

    list_model = dlg_model.createInstance("com.sun.star.awt.UnoControlListBoxModel")
    list_model.Name = "slash_popup"
    list_model.PositionX = 4
    list_model.PositionY = 4
    list_model.Width = 150
    list_model.Height = 40
    list_model.Dropdown = False
    dlg_model.insertByName("slash_popup", list_model)

    dlg = smgr.createInstanceWithContext("com.sun.star.awt.UnoControlDialog", ctx)
    dlg.setModel(dlg_model)
    toolkit = smgr.createInstanceWithContext("com.sun.star.awt.Toolkit", ctx)
    dlg.createPeer(toolkit, None)
    dlg.setVisible(True)
    try:
        query = dlg.getControl("query")
        box = dlg.getControl("slash_popup")
        assert query is not None and box is not None
        send = type("Host", (), {"query_control": query, "slash_popup": None, "dispatch": lambda *a, **k: None})()
        popup = SlashPopupController(box, send, query)
        send.slash_popup = popup

        popup.on_query_text("/")
        assert popup.is_open is True
        assert popup.selected_name == "help"
        assert "mock-alpha" in popup.visible_names
        assert int(box.getItemCount()) >= 5

        popup.on_query_text("/he")
        assert popup.visible_names == ["help"]
        assert popup.selected_name == "help"

        assert popup.handle_key(KEY_ESCAPE) is True
        assert popup.is_open is False

        popup.on_query_text("/")
        assert popup.handle_key(KEY_RETURN, 0) is True
        assert popup.is_open is False
    finally:
        dlg.dispose()
