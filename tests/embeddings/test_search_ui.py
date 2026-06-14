# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for SearchDialog."""

from unittest.mock import MagicMock, patch
import pytest

from plugin.embeddings.search_ui import SearchDialog, show_search_dialog


class TestSearchDialog:
    @patch("plugin.embeddings.search_ui.get_desktop")
    @patch("plugin.embeddings.search_ui.msgbox")
    @patch("plugin.embeddings.search_ui.warm_venv_worker")
    def test_search_dialog_open_and_close(self, mock_warm, mock_msgbox, mock_get_desktop):
        # Setup mock components for UNO
        mock_ctx = MagicMock()
        mock_smgr = mock_ctx.getServiceManager.return_value
        
        mock_dlg_model = MagicMock()
        mock_smgr.createInstanceWithContext.side_effect = lambda svc, ctx: {
            "com.sun.star.awt.UnoControlDialogModel": mock_dlg_model,
            "com.sun.star.awt.UnoControlDialog": MagicMock(),
            "com.sun.star.awt.Toolkit": MagicMock()
        }.get(svc, MagicMock())

        mock_frame = MagicMock()
        mock_get_desktop.return_value.getCurrentFrame.return_value = mock_frame
        mock_parent_window = mock_frame.getContainerWindow.return_value

        # Open and show
        dialog = SearchDialog(mock_ctx)
        
        assert mock_get_desktop.called
        assert mock_frame.getContainerWindow.called
        assert mock_smgr.createInstanceWithContext.called
        
        # Verify close disposes
        dialog.close()
        assert dialog._closed

    @patch("plugin.embeddings.search_ui.get_desktop")
    @patch("plugin.embeddings.search_ui.warm_venv_worker")
    def test_show_search_dialog_safely(self, mock_warm, mock_get_desktop):
        mock_ctx = MagicMock()
        mock_frame = MagicMock()
        mock_get_desktop.return_value.getCurrentFrame.return_value = mock_frame
        mock_parent_window = mock_frame.getContainerWindow.return_value

        show_search_dialog(mock_ctx)
        assert mock_get_desktop.called

    @patch("plugin.embeddings.search_ui.get_desktop")
    @patch("plugin.embeddings.search_ui.get_active_document")
    @patch("plugin.embeddings.embeddings_cache.resolve_index_context")
    @patch("plugin.embeddings.embeddings_cache.clear_folder_cache")
    @patch("plugin.framework.client.embeddings_service.maintain_folder_index")
    @patch("plugin.framework.client.embeddings_service._folder_search_mode", return_value="llama_index")
    def test_rebuild_action_triggered(self, mock_search_mode, mock_maintain, mock_clear, mock_resolve, mock_doc, mock_get_desktop):
        mock_ctx = MagicMock()
        mock_smgr = mock_ctx.getServiceManager.return_value
        
        mock_dlg_model = MagicMock()
        mock_dlg = MagicMock()
        mock_smgr.createInstanceWithContext.side_effect = lambda svc, ctx: {
            "com.sun.star.awt.UnoControlDialogModel": mock_dlg_model,
            "com.sun.star.awt.UnoControlDialog": mock_dlg,
            "com.sun.star.awt.Toolkit": MagicMock()
        }.get(svc, MagicMock())

        mock_frame = MagicMock()
        mock_get_desktop.return_value.getCurrentFrame.return_value = mock_frame
        mock_parent_window = mock_frame.getContainerWindow.return_value

        dialog = SearchDialog(mock_ctx)

        mock_resolve.return_value = ("folder_key", "db_path", "meta_path", "/path/to/listing")
        mock_doc.return_value = MagicMock()

        # Call Rebuild
        dialog._run_rebuild(mock_dlg)

        # Let background threads run or inspect execution
        import time
        time.sleep(0.1)

        assert mock_clear.called
        assert mock_maintain.called
        assert mock_maintain.call_args.kwargs["search_mode"] == "llama_index"
