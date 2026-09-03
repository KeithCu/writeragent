# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the hamburger popup menu."""

from unittest.mock import MagicMock, patch

from plugin.chatbot.hamburger_menu import show_hamburger_menu


class TestHamburgerMenu:
    def test_show_hamburger_menu_none_ctx(self):
        # Should cleanly do nothing when ctx is None
        show_hamburger_menu(None, None, MagicMock())

    def test_show_hamburger_menu_executes_selected_action(self):
        ctx = MagicMock()
        smgr = MagicMock()
        popup = MagicMock()
        ctx.getServiceManager.return_value = smgr
        smgr.createInstanceWithContext.return_value = popup

        # Simulate user selecting item ID 1 (Extend Selection)
        popup.execute.return_value = 1

        button_ctrl = MagicMock()
        pos_size = MagicMock(X=76, Y=2, Width=16, Height=12)
        button_ctrl.getPosSize.return_value = pos_size
        button_ctrl.getPeer.return_value = MagicMock()

        frame = MagicMock()
        mock_handler = MagicMock()

        with patch("plugin.chatbot.hamburger_menu.get_action_handler", return_value=mock_handler) as mock_get_handler:
            with patch("plugin.chatbot.hamburger_menu.is_writer", return_value=True), patch("plugin.chatbot.hamburger_menu.is_calc", return_value=False), patch("plugin.chatbot.hamburger_menu.is_draw", return_value=False):
                show_hamburger_menu(ctx, frame, button_ctrl)
                mock_get_handler.assert_called_with("chatbot.extend_selection")
                mock_handler.assert_called_once_with(frame)

    def test_writer_hamburger_loads_jupyter_icon(self):
        ctx = MagicMock()
        smgr = MagicMock()
        popup = MagicMock()
        ctx.getServiceManager.return_value = smgr
        smgr.createInstanceWithContext.return_value = popup
        popup.execute.return_value = 0
        button_ctrl = MagicMock()
        button_ctrl.getPosSize.return_value = MagicMock(X=76, Y=2, Width=16, Height=12)
        button_ctrl.getPeer.return_value = MagicMock()
        graphic = MagicMock()

        with (
            patch("plugin.chatbot.hamburger_menu.is_writer", return_value=True),
            patch("plugin.chatbot.hamburger_menu.is_calc", return_value=False),
            patch("plugin.chatbot.hamburger_menu.is_draw", return_value=False),
            patch("plugin.chatbot.hamburger_menu._load_graphic", return_value=graphic) as load_g,
        ):
            show_hamburger_menu(ctx, MagicMock(), button_ctrl)

        assert any(c.args[-1] == "gear_32.png" for c in load_g.call_args_list)
        labels = [c.args[1] for c in popup.insertItem.call_args_list]
        assert not any("Jupyter" in str(label) for label in labels)
        # Plain Writer (no WriterAgentNotebookJson): specialized notebook chrome stays off.
        assert "Run All" not in labels
        assert "Run From Here" not in labels
        assert "Stop" not in labels
        assert "Reset Python Session" in labels

    def test_writer_hamburger_with_notebook_registry_shows_run_actions(self):
        ctx = MagicMock()
        smgr = MagicMock()
        popup = MagicMock()
        ctx.getServiceManager.return_value = smgr
        smgr.createInstanceWithContext.return_value = popup
        popup.execute.return_value = 0
        button_ctrl = MagicMock()
        button_ctrl.getPosSize.return_value = MagicMock(X=76, Y=2, Width=16, Height=12)
        button_ctrl.getPeer.return_value = MagicMock()

        with (
            patch("plugin.chatbot.hamburger_menu.is_writer", return_value=True),
            patch("plugin.chatbot.hamburger_menu.is_calc", return_value=False),
            patch("plugin.chatbot.hamburger_menu.is_draw", return_value=False),
            patch("plugin.notebook.cell_registry.load_registry", return_value=object()),
        ):
            show_hamburger_menu(ctx, MagicMock(), button_ctrl)

        labels = [c.args[1] for c in popup.insertItem.call_args_list]
        assert "Run All" in labels
        assert "Run From Here" in labels
        assert "Stop" in labels
        assert labels.index("Reset Python Session") < labels.index("Run All")

    def test_calc_hamburger_omits_notebook_run_actions(self):
        ctx = MagicMock()
        smgr = MagicMock()
        popup = MagicMock()
        ctx.getServiceManager.return_value = smgr
        smgr.createInstanceWithContext.return_value = popup
        popup.execute.return_value = 0
        button_ctrl = MagicMock()
        button_ctrl.getPosSize.return_value = MagicMock(X=76, Y=2, Width=16, Height=12)
        button_ctrl.getPeer.return_value = MagicMock()

        with (
            patch("plugin.chatbot.hamburger_menu.is_writer", return_value=False),
            patch("plugin.chatbot.hamburger_menu.is_calc", return_value=True),
            patch("plugin.chatbot.hamburger_menu.is_draw", return_value=False),
            patch("plugin.notebook.cell_registry.load_registry", return_value=object()),
        ):
            show_hamburger_menu(ctx, MagicMock(), button_ctrl)

        labels = [c.args[1] for c in popup.insertItem.call_args_list]
        assert "Run All" not in labels
        assert "Run From Here" not in labels
        assert "Stop" not in labels

    def test_show_hamburger_menu_calc_includes_calc_items(self):
        ctx = MagicMock()
        smgr = MagicMock()
        popup = MagicMock()
        ctx.getServiceManager.return_value = smgr
        smgr.createInstanceWithContext.return_value = popup
        popup.execute.return_value = 0  # Cancelled

        button_ctrl = MagicMock()
        button_ctrl.getPosSize.return_value = MagicMock(X=76, Y=2, Width=16, Height=12)
        button_ctrl.getPeer.return_value = MagicMock()

        frame = MagicMock()

        with patch("plugin.chatbot.hamburger_menu.is_writer", return_value=False), patch("plugin.chatbot.hamburger_menu.is_calc", return_value=True), patch("plugin.chatbot.hamburger_menu.is_draw", return_value=False):
            show_hamburger_menu(ctx, frame, button_ctrl)
            # Verify insertItem was called on popup
            assert popup.insertItem.call_count > 5

