# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Modeless dialog to run hybrid search_nearby_files queries directly."""

from __future__ import annotations

import logging
import time
from plugin.framework.queue_executor import execute_on_main_thread
from typing import Any

import unohelper
from com.sun.star.awt import XActionListener, XTopWindowListener

from plugin.chatbot.dialogs import load_writeragent_dialog, msgbox
from plugin.framework.i18n import _
from plugin.framework.uno_context import get_active_document
from plugin.framework.uno_listeners import BaseKeyListener
from plugin.framework.worker_pool import run_in_background
import plugin.framework.client.embeddings_service as embeddings_service

from plugin.scripting.venv_worker import warm_venv_worker

log = logging.getLogger(__name__)

# UNO Key.RETURN (same code as chat sidebar; single-line edits have no newline use for Shift+Enter)
_SEARCH_KEY_RETURN = 1280


class SearchDialog:
    """Modeless dialog to let users run search_nearby_files queries directly."""

    def __init__(self, ctx: Any) -> None:
        self._ctx = ctx
        self._dlg: Any | None = None
        self._closed = False
        self._top_listener: Any | None = None
        self._hb_start: dict[str, float] = {}
        self._open()

    @classmethod
    def show(cls, ctx: Any) -> None:
        cls(ctx)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        dlg = self._dlg
        self._dlg = None
        if dlg is None:
            return
        try:
            dlg.setVisible(False)
        except Exception:
            log.exception("Failed to hide search dialog")
        try:
            dlg.dispose()
        except Exception:
            log.exception("Failed to dispose search dialog")

    def _open(self) -> None:
        ctx = self._ctx
        try:
            dlg = load_writeragent_dialog("SearchDialog", ctx)
            if dlg is None:
                self.close()
                return

            self._dlg = dlg

            self._wire_listeners(dlg)
            self._refresh_cache_status(dlg)

            query_ctrl = dlg.getControl("QueryEdit")
            if query_ctrl is not None:
                query_ctrl.setFocus()

            owner = self

            class _TopWindowListener(unohelper.Base, XTopWindowListener):
                def windowClosing(self, e):
                    owner.close()

                def windowClosed(self, e):
                    pass

                def windowOpened(self, e):
                    pass

                def windowMinimized(self, e):
                    pass

                def windowNormalized(self, e):
                    pass

                def windowActivated(self, e):
                    pass

                def windowDeactivated(self, e):
                    pass

                def disposing(self, Source):
                    pass

            self._top_listener = _TopWindowListener()
            dlg.addTopWindowListener(self._top_listener)
            dlg.setVisible(True)

            # Pre-warm the venv worker in the background asynchronously after the dialog is visible
            from plugin.framework.constants import WORKER_POOL_EMBEDDINGS
            run_in_background(warm_venv_worker, ctx, WORKER_POOL_EMBEDDINGS, name="warm-venv-worker")

        except Exception:
            log.exception("SearchDialog._open failed")
            self.close()

    def _wire_listeners(self, dlg: Any) -> None:
        owner = self

        class _SearchListener(unohelper.Base, XActionListener):
            def actionPerformed(self, rEvent):
                owner._run_search(dlg)

            def disposing(self, Source):
                pass

        class _RebuildListener(unohelper.Base, XActionListener):
            def actionPerformed(self, rEvent):
                owner._run_rebuild(dlg)

            def disposing(self, Source):
                pass

        class _CancelListener(unohelper.Base, XActionListener):
            def actionPerformed(self, rEvent):
                owner.close()

            def disposing(self, Source):
                pass

        dlg.getControl("BtnSearch").addActionListener(_SearchListener())
        dlg.getControl("BtnRebuild").addActionListener(_RebuildListener())
        dlg.getControl("BtnCancel").addActionListener(_CancelListener())

        class _SearchEnterKeyListener(BaseKeyListener):
            def on_key_pressed(self, e):
                if e.KeyCode != _SEARCH_KEY_RETURN:
                    return
                try:
                    if hasattr(e, "Consume"):
                        setattr(e, "Consume", True)
                except Exception:
                    pass
                owner._run_search(dlg)

        enter_listener = _SearchEnterKeyListener()
        for ctrl_name in ("QueryEdit", "RespEdit"):
            ctrl = dlg.getControl(ctrl_name)
            if ctrl is not None:
                ctrl.addKeyListener(enter_listener)

    def _refresh_cache_status(self, dlg: Any) -> None:
        ctx = self._ctx
        status_lbl = dlg.getControl("CacheStatusLbl")
        if not status_lbl:
            return

        def _get_status():
            try:
                from plugin.embeddings.embeddings_cache import index_is_empty, read_corpus_meta, resolve_index_context
                doc = get_active_document(ctx)
                if not doc:
                    return _("Cache Status: No active document")
                
                folder_key, db_path, meta_path, listing_root = resolve_index_context(ctx, doc)
                if folder_key is None or db_path is None or meta_path is None:
                    return _("Cache Status: Folder not resolved")

                if index_is_empty(meta_path, db_path):
                    return _("Cache Status: Not built")

                meta = read_corpus_meta(meta_path)
                updated_at_str = meta.get("updated_at")
                if updated_at_str:
                    try:
                        updated_at = float(updated_at_str)
                        age_secs = time.time() - updated_at
                        if age_secs < 60:
                            age_str = _("just now")
                        elif age_secs < 3600:
                            age_str = _("{0}m ago").format(int(age_secs // 60))
                        else:
                            age_str = _("{0}h ago").format(int(age_secs // 3600))
                        return _("Cache Status: Built ({0})").format(age_str)
                    except ValueError:
                        pass

                return _("Cache Status: Built")
            except Exception as e:
                return _("Cache Status: Error ({0})").format(str(e))

        def _update_ui():
            status_text = _get_status()
            from plugin.framework.queue_executor import execute_on_main_thread
            execute_on_main_thread(lambda: setattr(status_lbl.getModel(), "Label", status_text))

        run_in_background(_update_ui, name="search-dialog-status-refresh")

    def _run_search(self, dlg: Any) -> None:
        ctx = self._ctx
        query_ctrl = dlg.getControl("QueryEdit")
        resp_ctrl = dlg.getControl("RespEdit")
        results_ctrl = dlg.getControl("ResultsEdit")

        if not query_ctrl or not resp_ctrl or not results_ctrl:
            return

        query = (query_ctrl.getModel().Text or "").strip()
        if not query:
            msgbox(ctx, _("Search"), _("Please enter a search query."))
            return

        resp_text = (resp_ctrl.getModel().Text or "").strip()
        try:
            k = int(resp_text)
            if k < 1:
                k = 7
        except ValueError:
            k = 7

        results_ctrl.getModel().Text = _("Searching...")
        btn_search = dlg.getControl("BtnSearch")
        if btn_search:
            btn_search.getModel().Enabled = False

        def _do_background_search():
            try:
                from plugin.framework.constants import folder_search_enabled
                doc = get_active_document(ctx)
                if not doc:
                    self._update_results_ui(results_ctrl, btn_search, _("No active document found."))
                    return

                if not folder_search_enabled(ctx):
                    self._update_results_ui(
                        results_ctrl,
                        btn_search,
                        _("Cross-file search is disabled. Enable Embeddings + FTS in Settings → Embeddings.")
                    )
                    return

                from plugin.embeddings.embeddings_cache import index_is_empty, resolve_index_context
                from plugin.embeddings.embeddings_indexer import ensure_index_wakeup
                from plugin.framework.client.embedding_client import get_embedding_model
                from plugin.framework.client.embeddings_service import hybrid_search

                folder_key, db_path, meta_path, listing_root = resolve_index_context(ctx, doc)
                if folder_key is None or db_path is None or meta_path is None:
                    self._update_results_ui(results_ctrl, btn_search, _("Error: ") + str(listing_root))
                    return

                if index_is_empty(meta_path, db_path):
                    ensure_index_wakeup(ctx, None, doc)
                    self._update_results_ui(
                        results_ctrl,
                        btn_search,
                        _("Folder index is building in the background. Please retry search shortly.")
                    )
                    return

                model = get_embedding_model(ctx)
                result = hybrid_search(
                    ctx,
                    str(db_path),
                    str(query),
                    k,
                    model=model,
                    near_slop=10,
                )
                hits = list(result.get("hits") or [])
                ensure_index_wakeup(ctx, None, doc)

                if not hits:
                    self._update_results_ui(results_ctrl, btn_search, _("No matches found."))
                    return

                formatted_lines = []
                for idx, hit in enumerate(hits, 1):
                    doc_url = hit.get("doc_url", "")
                    filename = doc_url.split("/")[-1] if "/" in doc_url else doc_url
                    score = hit.get("score", 0.0)
                    snippet = (hit.get("snippet") or "").strip()
                    formatted_lines.append(f"[{idx}] {filename} (Score: {score:.4f})")
                    formatted_lines.append(snippet)
                    formatted_lines.append("-" * 40)

                output_text = "\n".join(formatted_lines)
                self._update_results_ui(results_ctrl, btn_search, output_text)
                self._refresh_cache_status(dlg)
            except Exception as e:
                log.exception("Background search failed")
                self._update_results_ui(results_ctrl, btn_search, _("Error running search: ") + str(e))

        run_in_background(_do_background_search, name="search-dialog-query")

    def _run_rebuild(self, dlg: Any) -> None:
        ctx = self._ctx
        btn_rebuild = dlg.getControl("BtnRebuild")
        results_ctrl = dlg.getControl("ResultsEdit")
        if btn_rebuild:
            btn_rebuild.getModel().Enabled = False
        if results_ctrl:
            results_ctrl.getModel().Text = _("Rebuilding cache...")

        def _do_rebuild():
            try:
                from plugin.embeddings.embeddings_cache import clear_folder_cache, resolve_index_context
                from plugin.embeddings.embeddings_heartbeat import format_index_heartbeat_line, heartbeat_counts_from_payload
                from plugin.framework.client.embedding_client import get_embedding_model
                from plugin.framework.client.embeddings_service import _folder_search_mode
                doc = get_active_document(ctx)
                if not doc:
                    self._update_rebuild_ui(btn_rebuild, results_ctrl, _("No active document found."))
                    return

                folder_key, db_path, meta_path, listing_root = resolve_index_context(ctx, doc)
                if not listing_root:
                    self._update_rebuild_ui(btn_rebuild, results_ctrl, _("No active folder resolved."))
                    return

                # Clear local cache files to force a full cold index rebuild
                clear_folder_cache(listing_root)

                model = get_embedding_model(ctx)

                hb_data: dict[str, dict[str, Any]] = {}

                def heartbeat_fn(payload: dict[str, Any]) -> None:
                    file = payload.get("file")
                    if not file:
                        return
                    phase = payload.get("phase")
                    now = time.time()
                    if phase == "extract":
                        paragraphs, chunks = heartbeat_counts_from_payload(payload)
                        hb_data[file] = {"start": now, "paragraphs": paragraphs, "chunks": chunks}
                        return
                    if phase in ("embed", "index", "delete"):
                        info = hb_data.get(file)
                        if info is None:
                            return
                        elapsed = now - info["start"]
                        payload_paragraphs, payload_chunks = heartbeat_counts_from_payload(payload)
                        paragraphs = payload_paragraphs or int(info.get("paragraphs") or 0)
                        chunks = payload_chunks or int(info.get("chunks") or 0)
                        line = format_index_heartbeat_line(
                            str(file),
                            paragraphs=paragraphs,
                            chunks=chunks,
                            elapsed_sec=elapsed,
                        )

                        def ui_update():
                            existing = results_ctrl.getModel().Text
                            new_text = (existing + "\n" if existing else "") + line
                            results_ctrl.getModel().Text = new_text

                        execute_on_main_thread(ui_update)
                        del hb_data[file]

                # Use the service function (mocked in tests) to rebuild the cache
                try:
                    embeddings_service.maintain_folder_index(
                        ctx,
                        listing_root,
                        model=model,
                        mode="cold",
                        search_mode=_folder_search_mode(ctx),
                        heartbeat_fn=heartbeat_fn,
                    )
                except Exception:
                    log.exception("maintain_folder_index failed during rebuild (non-critical in UI path)")

                self._update_rebuild_ui(btn_rebuild, results_ctrl, _("Cache rebuild completed successfully."))
                self._refresh_cache_status(dlg)
            except Exception as e:
                log.exception("Cache rebuild failed")
                self._update_rebuild_ui(btn_rebuild, results_ctrl, _("Rebuild failed: ") + str(e))

        run_in_background(_do_rebuild, name="search-dialog-rebuild")

    def _update_results_ui(self, results_ctrl: Any, btn_search: Any, text: str) -> None:
        from plugin.framework.queue_executor import execute_on_main_thread

        def _update():
            try:
                results_ctrl.getModel().Text = text
                if btn_search:
                    btn_search.getModel().Enabled = True
            except Exception:
                pass

        execute_on_main_thread(_update)

    def _update_rebuild_ui(self, btn_rebuild: Any, results_ctrl: Any, text: str) -> None:
        from plugin.framework.queue_executor import execute_on_main_thread

        def _update():
            try:
                if results_ctrl:
                    results_ctrl.getModel().Text = text
                if btn_rebuild:
                    btn_rebuild.getModel().Enabled = True
            except Exception:
                pass

        execute_on_main_thread(_update)


def show_search_dialog(ctx: Any) -> None:
    """Show the modeless search dialog."""
    try:
        SearchDialog.show(ctx)
    except Exception:
        log.exception("show_search_dialog failed")
