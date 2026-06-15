# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for SearchDialog."""

from unittest.mock import MagicMock, patch
from pathlib import Path
import pytest

from plugin.embeddings.search_ui import SearchDialog, show_search_dialog


class TestSearchDialog:
    @patch("plugin.embeddings.search_ui.msgbox")
    @patch("plugin.embeddings.search_ui.warm_venv_worker")
    def test_search_dialog_open_and_close(self, mock_warm, mock_msgbox):
        # Setup mock components for UNO
        mock_ctx = MagicMock()
        mock_smgr = mock_ctx.getServiceManager.return_value
        
        mock_dlg_model = MagicMock()
        mock_smgr.createInstanceWithContext.side_effect = lambda svc, ctx: {
            "com.sun.star.awt.UnoControlDialogModel": mock_dlg_model,
            "com.sun.star.awt.UnoControlDialog": MagicMock(),
            "com.sun.star.awt.Toolkit": MagicMock()
        }.get(svc, MagicMock())

        # Open and show
        dialog = SearchDialog(mock_ctx)
        
        assert mock_smgr.createInstanceWithContext.called
        
        # Verify close disposes
        dialog.close()
        assert dialog._closed

    @patch("plugin.embeddings.search_ui.warm_venv_worker")
    def test_show_search_dialog_safely(self, mock_warm):
        mock_ctx = MagicMock()
        mock_smgr = mock_ctx.getServiceManager.return_value
        mock_smgr.createInstanceWithContext.return_value = MagicMock()

        show_search_dialog(mock_ctx)

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

    def test_query_edit_enter_triggers_search(self):
        mock_ctx = MagicMock()
        dialog = SearchDialog.__new__(SearchDialog)
        dialog._ctx = mock_ctx

        mock_dlg = MagicMock()
        query_ctrl = MagicMock()
        resp_ctrl = MagicMock()
        mock_dlg.getControl.side_effect = lambda name: {
            "QueryEdit": query_ctrl,
            "RespEdit": resp_ctrl,
            "BtnSearch": MagicMock(),
            "BtnRebuild": MagicMock(),
            "BtnCancel": MagicMock(),
        }.get(name)

        dialog._wire_listeners(mock_dlg)
        assert query_ctrl.addKeyListener.called
        assert resp_ctrl.addKeyListener.called
        listener = query_ctrl.addKeyListener.call_args[0][0]

        dialog._run_search = MagicMock()
        enter_event = MagicMock(KeyCode=1280, Modifiers=0)
        listener.keyPressed(enter_event)
        dialog._run_search.assert_called_once_with(mock_dlg)

        shift_enter_event = MagicMock(KeyCode=1280, Modifiers=1)
        listener.keyPressed(shift_enter_event)
        dialog._run_search.assert_called_with(mock_dlg)
        assert dialog._run_search.call_count == 2

    @patch("plugin.embeddings.search_ui.get_desktop")
    @patch("plugin.embeddings.search_ui.get_active_document")
    @patch("plugin.embeddings.embeddings_cache.clear_folder_cache")
    @patch("plugin.framework.client.embeddings_service.maintain_folder_index")
    @patch("plugin.framework.client.embeddings_service._folder_search_mode", return_value="llama_index")
    def test_rebuild_untitled_doc_uses_my_documents_listing(
        self,
        mock_search_mode,
        mock_maintain,
        mock_clear,
        mock_doc,
        mock_get_desktop,
        tmp_path,
    ):
        mock_ctx = MagicMock()
        mock_smgr = mock_ctx.getServiceManager.return_value

        mock_dlg_model = MagicMock()
        mock_dlg = MagicMock()
        mock_smgr.createInstanceWithContext.side_effect = lambda svc, ctx: {
            "com.sun.star.awt.UnoControlDialogModel": mock_dlg_model,
            "com.sun.star.awt.UnoControlDialog": mock_dlg,
            "com.sun.star.awt.Toolkit": MagicMock(),
        }.get(svc, MagicMock())

        mock_frame = MagicMock()
        mock_get_desktop.return_value.getCurrentFrame.return_value = mock_frame
        mock_frame.getContainerWindow.return_value = MagicMock()

        my_docs = str(tmp_path / "Documents")
        Path(my_docs).mkdir()

        dialog = SearchDialog(mock_ctx)
        mock_doc.return_value = MagicMock()

        with patch("plugin.doc.document_helpers.get_document_path", return_value=None):
            with patch("plugin.doc.document_research.get_work_directory", return_value=my_docs):
                dialog._run_rebuild(mock_dlg)
                import time
                time.sleep(0.2)

        assert mock_clear.called
        assert mock_clear.call_args.args[0] == my_docs
        assert mock_maintain.called
        assert mock_maintain.call_args.args[1] == my_docs
