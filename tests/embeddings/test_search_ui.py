# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for SearchDialog."""

import json
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from plugin.embeddings.embeddings_cache import CorpusMeta
from plugin.embeddings.search_ui import (
    SearchDialog,
    format_cache_age,
    show_search_dialog,
)

_UNSET = object()


def _run_inline(fn, *args, **kwargs):
    return fn(*args, **kwargs)


def _make_uno_dialog_ctx():
    """Return a mock UNO context and dialog wired through the service manager."""
    mock_ctx = MagicMock()
    mock_smgr = mock_ctx.getServiceManager.return_value
    mock_dlg_model = MagicMock()
    mock_dlg = MagicMock()
    mock_smgr.createInstanceWithContext.side_effect = lambda svc, ctx: {
        "com.sun.star.awt.UnoControlDialogModel": mock_dlg_model,
        "com.sun.star.awt.UnoControlDialog": mock_dlg,
        "com.sun.star.awt.Toolkit": MagicMock(),
    }.get(svc, MagicMock())
    return mock_ctx, mock_dlg


def _make_bare_dialog(query: str = "test query", resp: str = "7", *, wire_controls: bool = True):
    """Construct a SearchDialog without __init__ and optionally wire named control mocks."""
    mock_ctx = MagicMock()
    dialog = SearchDialog.__new__(SearchDialog)
    dialog._ctx = mock_ctx
    mock_dlg = MagicMock()
    controls: dict[str, MagicMock] = {}
    if wire_controls:
        controls = {
            "QueryEdit": MagicMock(),
            "RespEdit": MagicMock(),
            "ResultsEdit": MagicMock(),
            "BtnSearch": MagicMock(),
            "BtnRebuild": MagicMock(),
            "BtnCancel": MagicMock(),
        }
        controls["QueryEdit"].getModel().Text = query
        controls["RespEdit"].getModel().Text = resp
        mock_dlg.getControl.side_effect = controls.get
    return dialog, mock_dlg, controls


@contextmanager
def _patched_rebuild_env(
    *,
    maintain_side_effect: Any = None,
    main_call_side_effect: Any = _run_inline,
    main_post_side_effect: Any = None,
    resolve_context: Any = _UNSET,
):
    """Patch SearchDialog rebuild dependencies with inline background and main-thread defaults."""
    with ExitStack() as stack:
        stack.enter_context(patch("plugin.framework.constants.configured_folder_search_mode", return_value="llama_index"))
        maintain_mock = stack.enter_context(
            patch("plugin.embeddings.embeddings_service.maintain_folder_index", side_effect=maintain_side_effect)
        )
        clear_mock = stack.enter_context(patch("plugin.embeddings.embeddings_cache.clear_folder_cache"))
        if resolve_context is not _UNSET:
            stack.enter_context(patch("plugin.embeddings.embeddings_cache.resolve_index_context", return_value=resolve_context))
        doc_mock = stack.enter_context(patch("plugin.embeddings.search_ui.get_active_document", return_value=MagicMock()))
        desktop_mock = stack.enter_context(patch("plugin.framework.uno_context.get_desktop"))
        stack.enter_context(patch("plugin.framework.queue_executor.main.call", side_effect=main_call_side_effect))
        if main_post_side_effect is not None:
            stack.enter_context(patch("plugin.framework.queue_executor.main.post", side_effect=main_post_side_effect))
        stack.enter_context(
            patch("plugin.embeddings.search_ui.run_in_background", side_effect=lambda fn, *args, **kwargs: fn(*args))
        )
        stack.enter_context(patch("plugin.embeddings.embedding_client.get_embedding_model", return_value="fake-model"))
        yield SimpleNamespace(maintain=maintain_mock, clear=clear_mock, doc=doc_mock, desktop=desktop_mock)


@contextmanager
def _patched_search_env(
    *,
    folder_search: bool = True,
    main_call_side_effect: Any = _run_inline,
    get_doc_side_effect: Any = None,
    index_context: tuple[Any, ...] = ("folder_key", Path("/db"), Path("/meta"), "/listing"),
    search_mode: str | None = None,
    embedding_model: str | None = None,
    patch_rpc: bool = False,
):
    """Patch SearchDialog search dependencies with inline background and main-thread defaults."""
    with ExitStack() as stack:
        stack.enter_context(patch("plugin.framework.constants.folder_search_enabled", return_value=folder_search))
        stack.enter_context(patch("plugin.framework.queue_executor.main.call", side_effect=main_call_side_effect))
        doc_kwargs = {"side_effect": get_doc_side_effect} if get_doc_side_effect is not None else {"return_value": MagicMock()}
        stack.enter_context(patch("plugin.embeddings.search_ui.get_active_document", **doc_kwargs))
        stack.enter_context(patch("plugin.embeddings.embeddings_cache.resolve_index_context", return_value=index_context))
        stack.enter_context(
            patch("plugin.embeddings.search_ui.run_in_background", side_effect=lambda fn, *args, **kwargs: fn(*args))
        )
        if search_mode is not None:
            stack.enter_context(patch("plugin.framework.constants.configured_folder_search_mode", return_value=search_mode))
        if embedding_model is not None:
            stack.enter_context(patch("plugin.embeddings.embedding_client.get_embedding_model", return_value=embedding_model))
        hybrid_mock = stack.enter_context(patch("plugin.embeddings.embeddings_service.hybrid_search")) if patch_rpc else None
        wakeup_mock = stack.enter_context(patch("plugin.embeddings.embeddings_indexer.ensure_index_wakeup")) if patch_rpc else None
        yield SimpleNamespace(hybrid=hybrid_mock, wakeup=wakeup_mock)


class TestSearchDialog:
    @patch("plugin.embeddings.search_ui.msgbox")
    @patch("plugin.embeddings.search_ui.warm_venv_worker")
    def test_search_dialog_open_and_close(self, _mock_warm, mock_msgbox):
        mock_ctx, _mock_dlg = _make_uno_dialog_ctx()
        dialog = SearchDialog(mock_ctx)

        dlg = dialog._dlg
        assert dlg is not None
        mock_msgbox.assert_not_called()
        dlg.setVisible.assert_any_call(True)

        # Verify close disposes once and is idempotent
        dialog.close()
        assert dialog._closed
        assert dialog._dlg is None
        dlg.dispose.assert_called_once_with()

        dialog.close()
        dlg.dispose.assert_called_once_with()

    @pytest.mark.parametrize("side_effect", [None, RuntimeError("boom")])
    def test_show_search_dialog_opens_dialog_and_swallows_errors(self, side_effect):
        mock_ctx = MagicMock()
        with patch.object(SearchDialog, "show", side_effect=side_effect) as mock_show:
            show_search_dialog(mock_ctx)

        mock_show.assert_called_once_with(mock_ctx)

    def test_rebuild_action_triggered(self):
        hb_capture = []

        def _mock_maintain(*args, **kwargs):
            if "heartbeat_fn" in kwargs:
                hb_capture.append(kwargs["heartbeat_fn"])

        mock_ctx, mock_dlg = _make_uno_dialog_ctx()
        with _patched_rebuild_env(
            maintain_side_effect=_mock_maintain,
            resolve_context=("folder_key", "db_path", "meta_path", "/path/to/listing"),
        ) as env:
            env.desktop.return_value.getCurrentFrame.return_value = MagicMock()
            dialog = SearchDialog(mock_ctx)
            dialog._run_rebuild(mock_dlg)

        assert env.clear.called
        assert env.maintain.called
        assert env.maintain.call_args.kwargs["search_mode"] == "llama_index"

        # Test heartbeat callback exception handling
        assert hb_capture
        hb = hb_capture[0]
        hb({"file": "test.txt", "phase": "extract", "paragraphs": 5, "chunks": 10})

        mock_results_ctrl = MagicMock()
        mock_dlg.getControl.side_effect = lambda name: mock_results_ctrl if name == "ResultsEdit" else MagicMock()
        mock_results_ctrl.getModel.side_effect = Exception("UI Disposed")

        try:
            hb({"file": "test.txt", "phase": "index", "paragraphs": 5, "chunks": 10})
        except Exception as e:
            pytest.fail(f"Heartbeat callback raised exception: {e}")

    def test_rebuild_heartbeat_posts_instead_of_call(self):
        in_heartbeat = False
        heartbeat_call_count = 0

        def _mock_call(fn, *args, **kwargs):
            if in_heartbeat:
                nonlocal heartbeat_call_count
                heartbeat_call_count += 1
                raise RuntimeError("main.call called during heartbeat callback")
            return fn(*args, **kwargs)

        recorded_posts = []

        def _mock_post(fn, *args, **kwargs):
            recorded_posts.append(kwargs.get("bound_scope"))

        def _mock_maintain(*args, **kwargs):
            nonlocal in_heartbeat
            hb = kwargs.get("heartbeat_fn")
            assert hb is not None
            in_heartbeat = True
            try:
                hb({"file": "a.txt", "phase": "extract", "paragraphs": 1, "chunks": 2})
                hb({"file": "a.txt", "phase": "embed", "paragraphs": 1, "chunks": 2})
                hb({"file": "b.txt", "phase": "extract", "paragraphs": 3, "chunks": 4})
                hb({"file": "b.txt", "phase": "index", "paragraphs": 3, "chunks": 4})
            finally:
                in_heartbeat = False

        mock_ctx, mock_dlg = _make_uno_dialog_ctx()
        with _patched_rebuild_env(
            maintain_side_effect=_mock_maintain,
            main_call_side_effect=_mock_call,
            main_post_side_effect=_mock_post,
            resolve_context=("folder_key", "db_path", "meta_path", "/path/to/listing"),
        ) as env:
            env.desktop.return_value.getCurrentFrame.return_value = MagicMock()
            dialog = SearchDialog(mock_ctx)
            dialog._run_rebuild(mock_dlg)

        assert env.clear.called
        assert env.maintain.called
        assert recorded_posts == [None, None]
        assert heartbeat_call_count == 0

    def test_query_edit_enter_triggers_search(self):
        dialog, mock_dlg, controls = _make_bare_dialog()
        query_ctrl = controls["QueryEdit"]
        resp_ctrl = controls["RespEdit"]

        dialog._wire_listeners(mock_dlg)
        assert query_ctrl.addKeyListener.called
        assert resp_ctrl.addKeyListener.called
        listener = query_ctrl.addKeyListener.call_args[0][0]

        dialog._run_search = MagicMock()
        listener.keyPressed(MagicMock(KeyCode=1280, Modifiers=0))
        dialog._run_search.assert_called_once_with(mock_dlg)

        listener.keyPressed(MagicMock(KeyCode=1280, Modifiers=1))
        dialog._run_search.assert_called_with(mock_dlg)
        assert dialog._run_search.call_count == 2

    def test_rebuild_untitled_doc_uses_my_documents_listing(self, tmp_path):
        mock_ctx, mock_dlg = _make_uno_dialog_ctx()
        my_docs = str(tmp_path / "Documents")
        Path(my_docs).mkdir()

        with _patched_rebuild_env() as env:
            mock_frame = MagicMock()
            env.desktop.return_value.getCurrentFrame.return_value = mock_frame
            mock_frame.getContainerWindow.return_value = MagicMock()
            dialog = SearchDialog(mock_ctx)

            with (
                patch("plugin.doc.text_helpers.get_document_path", return_value=None),
                patch("plugin.doc.document_research.get_work_directory", return_value=my_docs),
            ):
                dialog._run_rebuild(mock_dlg)

        assert env.clear.called
        assert env.clear.call_args.args[0] == my_docs
        assert env.maintain.called
        assert env.maintain.call_args.args[1] == my_docs

    def test_refresh_cache_status_marshals_uno_to_main_thread(self):
        dialog, mock_dlg, _controls = _make_bare_dialog(wire_controls=False)
        status_lbl = MagicMock()
        mock_dlg.getControl.return_value = status_lbl

        with (
            patch("plugin.embeddings.search_ui.run_in_background") as mock_bg,
            patch("plugin.framework.queue_executor.main.call", side_effect=_run_inline),
            patch("plugin.embeddings.search_ui.get_active_document", return_value=None) as mock_doc,
        ):
            dialog._refresh_cache_status(mock_dlg)

        mock_bg.assert_not_called()
        mock_doc.assert_called_once_with(dialog._ctx)
        assert status_lbl.getModel().Label

    def test_search_marshals_doc_resolution_before_background_work(self):
        dialog, mock_dlg, _controls = _make_bare_dialog()
        marshal_depth = [0]
        doc_calls_during_marshal: list[bool] = []

        def fake_execute(fn, *args, **kwargs):
            marshal_depth[0] += 1
            try:
                return fn(*args, **kwargs)
            finally:
                marshal_depth[0] -= 1

        def tracking_get_active_document(_ctx):
            doc_calls_during_marshal.append(marshal_depth[0] > 0)
            return MagicMock()

        with _patched_search_env(
            folder_search=False,
            main_call_side_effect=fake_execute,
            get_doc_side_effect=tracking_get_active_document,
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
        dialog, mock_dlg, _controls = _make_bare_dialog()

        with _patched_search_env(
            index_context=("folder_key", db, meta, str(tmp_path)),
            search_mode="hybrid",
            embedding_model="new-model",
            patch_rpc=True,
        ) as env:
            dialog._run_search(mock_dlg)

        env.hybrid.assert_not_called()
        env.wakeup.assert_called_once()

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
        dialog, mock_dlg, _controls = _make_bare_dialog(wire_controls=False)
        status_lbl = MagicMock()
        mock_dlg.getControl.return_value = status_lbl

        two_days_ago = time.time() - (86400 * 2 + 100)

        with (
            patch("plugin.framework.queue_executor.main.call", side_effect=_run_inline),
            patch("plugin.embeddings.search_ui.get_active_document", return_value=MagicMock()),
            patch("plugin.embeddings.embeddings_cache.resolve_index_context", return_value=("key", Path("/db"), Path("/meta"), "/root")),
            patch("plugin.framework.constants.configured_folder_search_mode", return_value="sqlite"),
            patch("plugin.embeddings.embeddings_cache.index_is_empty", return_value=False),
            patch(
                "plugin.embeddings.embeddings_cache.CorpusMeta.load",
                return_value=CorpusMeta(data={"updated_at": str(two_days_ago)}, is_valid=True),
            ),
        ):
            dialog._refresh_cache_status(mock_dlg)

        assert status_lbl.getModel().Label == "Cache Status: Built (2d ago)"

    def test_search_and_rebuild_use_dedicated_threads(self):
        # Search and rebuild are long blocking jobs; dedicated=True prevents starving the shared 2-worker pool.
        dialog, mock_dlg, _controls = _make_bare_dialog(query="q", resp="7")
        recorded_kwargs: list[dict[str, Any]] = []

        def fake_run_in_background(_fn, *args, **kwargs):
            recorded_kwargs.append(kwargs)

        with patch("plugin.embeddings.search_ui.run_in_background", side_effect=fake_run_in_background):
            dialog._run_search(mock_dlg)
            dialog._run_rebuild(mock_dlg)

        search_call = next(c for c in recorded_kwargs if c.get("name") == "search-dialog-query")
        rebuild_call = next(c for c in recorded_kwargs if c.get("name") == "search-dialog-rebuild")
        assert search_call.get("dedicated") is True
        assert rebuild_call.get("dedicated") is True
