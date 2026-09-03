# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Native UNO coverage for the Ask-peer toolkit slash overlay (no Packet G audio)."""

from __future__ import annotations

from plugin.testing_runner import native_test


@native_test
def test_slash_popup_listbox_filter_and_keys(ctx):
    """Drive SlashPopupController on a live toolkit listbox created on first ``/``."""
    from plugin.chatbot.slash_commands import KEY_ESCAPE, KEY_RETURN
    from plugin.chatbot.slash_popup import SlashPopupController, uses_toolkit_overlay

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

    dlg = smgr.createInstanceWithContext("com.sun.star.awt.UnoControlDialog", ctx)
    dlg.setModel(dlg_model)
    toolkit = smgr.createInstanceWithContext("com.sun.star.awt.Toolkit", ctx)
    dlg.createPeer(toolkit, None)
    dlg.setVisible(True)
    popup = None
    try:
        query = dlg.getControl("query")
        assert query is not None
        send = type("Host", (), {"query_control": query, "slash_popup": None, "dispatch": lambda *a, **k: None})()
        # Same parent as sidebar wiring: dialog root, not the 24px Ask peer.
        popup = SlashPopupController(None, send, query, overlay_parent=dlg)
        send.slash_popup = popup
        # Overlay is born on first `/`, not in __init__ (createWindow is deferred).
        assert uses_toolkit_overlay(popup) is False

        popup.on_query_text("/")
        assert uses_toolkit_overlay(popup) is True
        box = popup.control
        assert box is not None

        assert popup.is_open is True
        assert popup.selected_name == "help"
        assert "clear" in popup.visible_names
        assert "mock-alpha" in popup.visible_names
        assert int(box.getItemCount()) >= 5
        ps = box.getPosSize()
        assert int(ps.Height) > 20
        # SIMPLE listbox is parented to the dialog (no decorated TOP host).
        assert popup._popup_floater is None

        popup.on_query_text("/he")
        assert popup.visible_names == ["help"]
        assert popup.selected_name == "help"

        assert popup.handle_key(KEY_ESCAPE) is True
        assert popup.is_open is False
        # hide() disposes the toolkit window so Esc can recreate a clean overlay.
        assert uses_toolkit_overlay(popup) is False

        popup.on_query_text("/")
        assert popup.is_open is True
        assert popup.handle_key(KEY_RETURN, 0) is True
        assert popup.is_open is False
    finally:
        if getattr(popup, "_popup_window", None) is not None:
            try:
                popup._popup_window.dispose()
            except Exception:
                pass
        dlg.dispose()
