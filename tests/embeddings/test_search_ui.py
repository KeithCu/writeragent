# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for SearchDialog."""

import json
from unittest.mock import MagicMock, patch
from pathlib import Path

import pytest

from plugin.embeddings.search_ui import (
    SearchDialog, format_cache_age, show_search_dialog, _ready_search_path,
)


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

    @patch("plugin.embeddings.search_ui.run_in_background", side_effect=lambda fn, *args, **kwargs: fn(*args))
    @patch("plugin.embeddings.search_ui.execute_on_main_thread", side_effect=lambda fn, *args, **kwargs: fn(*args, **kwargs))
    @patch("plugin.framework.uno_context.get_desktop")
    @patch("plugin.embeddings.search_ui.get_active_document")
    @patch("plugin.embeddings.embeddings_cache.resolve_index_context")
    @patch("plugin.embeddings.embeddings_cache.clear_folder_cache")
    @patch("plugin.embeddings.embeddings_service.maintain_folder_index")
    @patch("plugin.embeddings.embeddings_service._folder_search_mode", return_value="llama_index")
    def test_rebuild_action_triggered(self, mock_search_mode, mock_maintain, mock_clear, mock_resolve, mock_doc, mock_get_desktop, _mock_execute, _mock_bg):
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

        dialog = SearchDialog(mock_ctx)

        mock_resolve.return_value = ("folder_key", "db_path", "meta_path", "/path/to/listing")
        mock_doc.return_value = MagicMock()

        # Call Rebuild
        dialog._run_rebuild(mock_dlg)

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

    @patch("plugin.embeddings.search_ui.run_in_background", side_effect=lambda fn, *args, **kwargs: fn(*args))
    @patch("plugin.embeddings.search_ui.execute_on_main_thread", side_effect=lambda fn, *args, **kwargs: fn(*args, **kwargs))
    @patch("plugin.framework.uno_context.get_desktop")
    @patch("plugin.embeddings.search_ui.get_active_document")
    @patch("plugin.embeddings.embeddings_cache.clear_folder_cache")
    @patch("plugin.embeddings.embeddings_service.maintain_folder_index")
    @patch("plugin.embeddings.embeddings_service._folder_search_mode", return_value="llama_index")
    def test_rebuild_untitled_doc_uses_my_documents_listing(
        self,
        mock_search_mode,
        mock_maintain,
        mock_clear,
        mock_doc,
        mock_get_desktop,
        _mock_execute,
        _mock_bg,
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

        with patch("plugin.doc.text_helpers.get_document_path", return_value=None):
            with patch("plugin.doc.document_research.get_work_directory", return_value=my_docs):
                dialog._run_rebuild(mock_dlg)

        assert mock_clear.called
        assert mock_clear.call_args.args[0] == my_docs
        assert mock_maintain.called
        assert mock_maintain.call_args.args[1] == my_docs

    def test_refresh_cache_status_marshals_uno_to_main_thread(self):
        mock_ctx = MagicMock()
        dialog = SearchDialog.__new__(SearchDialog)
        dialog._ctx = mock_ctx
        mock_dlg = MagicMock()
        status_lbl = MagicMock()
        mock_dlg.getControl.return_value = status_lbl

        with patch("plugin.embeddings.search_ui.run_in_background") as mock_bg:
            with patch(
                "plugin.embeddings.search_ui.execute_on_main_thread",
                side_effect=lambda fn, *args, **kwargs: fn(*args, **kwargs),
            ):
                with patch("plugin.embeddings.search_ui.get_active_document") as mock_doc:
                    mock_doc.return_value = None
                    dialog._refresh_cache_status(mock_dlg)

        mock_bg.assert_not_called()
        mock_doc.assert_called_once_with(mock_ctx)
        assert status_lbl.getModel().Label

    @patch("plugin.framework.constants.folder_search_enabled", return_value=False)
    def test_search_marshals_doc_resolution_before_background_work(self, _mock_folder_search):
        mock_ctx = MagicMock()
        dialog = SearchDialog.__new__(SearchDialog)
        dialog._ctx = mock_ctx

        mock_dlg = MagicMock()
        query_ctrl = MagicMock()
        resp_ctrl = MagicMock()
        results_ctrl = MagicMock()
        btn_search = MagicMock()
        mock_dlg.getControl.side_effect = lambda name: {
            "QueryEdit": query_ctrl,
            "RespEdit": resp_ctrl,
            "ResultsEdit": results_ctrl,
            "BtnSearch": btn_search,
        }.get(name)
        query_ctrl.getModel().Text = "test query"
        resp_ctrl.getModel().Text = "7"

        marshal_depth = [0]
        doc_calls_during_marshal: list[bool] = []

        def fake_execute(fn, *args, **kwargs):
            marshal_depth[0] += 1
            try:
                return fn(*args, **kwargs)
            finally:
                marshal_depth[0] -= 1

        def tracking_get_active_document(ctx):
            doc_calls_during_marshal.append(marshal_depth[0] > 0)
            return MagicMock()

        with patch("plugin.embeddings.search_ui.execute_on_main_thread", side_effect=fake_execute):
            with patch("plugin.embeddings.search_ui.get_active_document", side_effect=tracking_get_active_document):
                with patch(
                    "plugin.embeddings.embeddings_cache.resolve_index_context",
                    return_value=("folder_key", Path("/db"), Path("/meta"), "/listing"),
                ):
                    with patch(
                        "plugin.embeddings.search_ui.run_in_background",
                        side_effect=lambda fn, *args, **kwargs: fn(*args),
                    ):
                        dialog._run_search(mock_dlg)

        assert doc_calls_during_marshal, "get_active_document should run during search"
        assert all(doc_calls_during_marshal), "get_active_document must be marshaled to main thread"

    def test_search_refuses_wrong_model_index(self, tmp_path):
        meta = tmp_path / "corpus_meta.json"
        db = tmp_path / "corpus.db"
        db.write_text("sqlite", encoding="utf-8")
        meta.write_text(
            json.dumps(
                {
                    "schema_version": "6",
                    "embedding_model": "old-model",
                    "chunk_count": "2",
                    "dim": "384",
                }
            ),
            encoding="utf-8",
        )
        mock_ctx = MagicMock()
        dialog = SearchDialog.__new__(SearchDialog)
        dialog._ctx = mock_ctx
        mock_dlg = MagicMock()
        query_ctrl = MagicMock()
        resp_ctrl = MagicMock()
        results_ctrl = MagicMock()
        btn_search = MagicMock()
        mock_dlg.getControl.side_effect = lambda name: {
            "QueryEdit": query_ctrl,
            "RespEdit": resp_ctrl,
            "ResultsEdit": results_ctrl,
            "BtnSearch": btn_search,
        }.get(name)
        query_ctrl.getModel().Text = "test query"
        resp_ctrl.getModel().Text = "7"

        def _call(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        with patch("plugin.framework.constants.folder_search_enabled", return_value=True):
            with patch("plugin.embeddings.search_ui.execute_on_main_thread", side_effect=_call):
                with patch("plugin.framework.queue_executor.execute_on_main_thread", side_effect=_call):
                    with patch("plugin.embeddings.search_ui.get_active_document", return_value=MagicMock()):
                        with patch(
                            "plugin.embeddings.embeddings_cache.resolve_index_context",
                            return_value=("folder_key", db, meta, str(tmp_path)),
                        ):
                            with patch(
                                "plugin.embeddings.search_ui.run_in_background",
                                side_effect=lambda fn, *args, **kwargs: fn(*args),
                            ):
                                with patch(
                                    "plugin.embeddings.embeddings_service._folder_search_mode",
                                    return_value="hybrid",
                                ):
                                    with patch(
                                        "plugin.embeddings.embedding_client.get_embedding_model",
                                        return_value="new-model",
                                    ):
                                        with patch(
                                            "plugin.embeddings.embeddings_service.hybrid_search"
                                        ) as hybrid_mock:
                                            with patch(
                                                "plugin.embeddings.embeddings_indexer.ensure_index_wakeup"
                                            ) as wakeup_mock:
                                                dialog._run_search(mock_dlg)

        hybrid_mock.assert_not_called()
        wakeup_mock.assert_called_once()

    @pytest.mark.parametrize(
        ("mode", "listing_root", "populated", "empty_index", "expected", "create_parent_calls"),
        [
            ("zvec", None, False, False, None, []),
            ("zvec", "/root", False, False, None, [False]),
            ("zvec", "/root", True, False, "/ready", [False, True]),
            ("lancedb", None, False, False, None, []),
            ("lancedb", "/root", False, False, None, [False]),
            ("lancedb", "/root", True, False, "/ready", [False, True]),
            ("sqlite", "/root", False, True, None, []),
            ("sqlite", "/root", False, False, "/db", []),
        ],
    )
    def test_ready_search_path(self, mode, listing_root, populated, empty_index, expected, create_parent_calls):
        """Unbuilt stores return None and do not create the parent directory."""
        path_name = "zvec_collection_path" if mode == "zvec" else "lancedb_collection_path"
        populated_name = (
            "zvec_collection_looks_populated" if mode == "zvec" else "lancedb_collection_looks_populated"
        )

        def _path(root, *, create_parent):
            return Path("/probe") if not create_parent else Path("/ready")

        with patch(f"plugin.embeddings.embeddings_cache.{path_name}", side_effect=_path) as path:
            with patch(f"plugin.embeddings.embeddings_cache.{populated_name}", return_value=populated):
                with patch("plugin.embeddings.embeddings_cache.index_is_empty", return_value=empty_index):
                    got = _ready_search_path(mode, listing_root, Path("/meta"), Path("/db"))

        if expected is None:
            assert got is None
        else:
            # str(Path("/ready")) is "\\ready" on Windows. The product returns
            # the native filesystem string; compare path objects so both forms match.
            assert got is not None
            assert Path(got) == Path(expected)
        assert [call.kwargs["create_parent"] for call in path.call_args_list] == create_parent_calls


    def test_format_cache_age_thresholds(self):
        # < 60 seconds -> "just now"
        assert format_cache_age(0) == "just now"
        assert format_cache_age(30) == "just now"
        assert format_cache_age(59.9) == "just now"
        assert format_cache_age(-5) == "just now"

        # 60s <= age < 3600s -> minutes
        assert format_cache_age(60) == "1m ago"
        assert format_cache_age(120) == "2m ago"
        assert format_cache_age(3599) == "59m ago"

        # 3600s <= age < 86400s -> hours
        assert format_cache_age(3600) == "1h ago"
        assert format_cache_age(7200) == "2h ago"
        assert format_cache_age(86399) == "23h ago"

        # >= 86400s (24h) -> days
        assert format_cache_age(86400) == "1d ago"
        assert format_cache_age(86400 * 1.5) == "1d ago"
        assert format_cache_age(86400 * 2) == "2d ago"
        assert format_cache_age(86400 * 7) == "7d ago"

    def test_refresh_cache_status_over_24h_shows_days(self):
        import time

        mock_ctx = MagicMock()
        dialog = SearchDialog.__new__(SearchDialog)
        dialog._ctx = mock_ctx
        mock_dlg = MagicMock()
        status_lbl = MagicMock()
        mock_dlg.getControl.return_value = status_lbl

        two_days_ago = time.time() - (86400 * 2 + 100)

        with patch("plugin.embeddings.search_ui.execute_on_main_thread", side_effect=lambda fn, *args, **kwargs: fn(*args, **kwargs)):
            with patch("plugin.embeddings.search_ui.get_active_document", return_value=MagicMock()):
                with patch("plugin.embeddings.embeddings_cache.resolve_index_context", return_value=("key", Path("/db"), Path("/meta"), "/root")):
                    with patch("plugin.embeddings.embeddings_service._folder_search_mode", return_value="sqlite"):
                        with patch("plugin.embeddings.embeddings_cache.index_is_empty", return_value=False):
                            with patch("plugin.embeddings.embeddings_cache.read_corpus_meta", return_value={"updated_at": str(two_days_ago)}):
                                dialog._refresh_cache_status(mock_dlg)

        assert status_lbl.getModel().Label == "Cache Status: Built (2d ago)"

