# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Modeless dialog to run hybrid research_search_nearby_files queries directly."""

from __future__ import annotations

import logging
import time
from plugin.framework.queue_executor import main
from typing import Any

from plugin.chatbot.dialogs import load_writeragent_dialog_detail, msgbox
from plugin.chatbot.slash_commands import KEY_RETURN
from plugin.framework.errors import suppress_disposed
from plugin.framework.i18n import _
from plugin.framework.thread_guard import main_thread_only
from plugin.framework.uno_context import get_active_document
from plugin.framework.uno_listeners import (
    BaseActionListener,
    BaseKeyListener,
    BaseTopWindowListener,
)
from plugin.framework.worker_pool import run_in_background
import plugin.embeddings.embeddings_service as embeddings_service

from plugin.scripting.venv_worker import warm_venv_worker

log = logging.getLogger(__name__)


def format_index_heartbeat_line(
    filename: str,
    *,
    paragraphs: int,
    chunks: int,
    elapsed_sec: float,
) -> str:
    """Format one per-file rebuild progress line (native paragraphs + embed chunks)."""
    para_label = "paragraph" if paragraphs == 1 else "paragraphs"
    chunk_label = "chunk" if chunks == 1 else "chunks"
    return f"{filename}: {paragraphs} {para_label}, {chunks} {chunk_label}, {elapsed_sec:.2f}s"


def heartbeat_counts_from_payload(payload: dict[str, Any]) -> tuple[int, int]:
    """Return (paragraphs, chunks) from a maintain heartbeat payload."""
    paragraphs = int(payload.get("paragraphs") or 0)
    chunks = int(payload.get("upserted") or payload.get("chunks") or paragraphs)
    return paragraphs, chunks


def format_cache_age(age_secs: float) -> str:
    """Format relative cache age in seconds to human-readable localized string.

    Displays 'just now', minutes ('{0}m ago'), hours ('{0}h ago'), or days ('{0}d ago')
    when age is 24 hours or greater.
    """
    if age_secs < 60:
        return _("just now")
    if age_secs < 3600:
        return _("{0}m ago").format(int(age_secs // 60))
    if age_secs < 86400:
        return _("{0}h ago").format(int(age_secs // 3600))
    return _("{0}d ago").format(int(age_secs // 86400))


class SearchDialog:
    """Modeless dialog to let users run research_search_nearby_files queries directly."""

    _ctx: Any
    _closed: bool

    def __init__(self, ctx: Any) -> None:
        self._ctx = ctx
        self._dlg: Any | None = None
        self._closed = False
        self._top_listener: Any | None = None
        self._hb_start: dict[str, float] = {}
        self._open()

    @classmethod
    @main_thread_only
    def show(cls, ctx: Any) -> None:
        cls(ctx)

    def close(self, *, toolkit_teardown: bool = False) -> None:
        if self._closed:
            return
        self._closed = True
        dlg = self._dlg
        self._dlg = None
        if dlg is None:
            return
        if toolkit_teardown:
            with suppress_disposed("search: hide after windowClosing", logger=log):
                dlg.setVisible(False)
            return
        with suppress_disposed("search: hide before dispose", logger=log):
            dlg.setVisible(False)
        with suppress_disposed("search: dispose", logger=log):
            dlg.dispose()

    def _open(self) -> None:
        ctx = self._ctx
        try:
            dlg, load_detail = load_writeragent_dialog_detail("SearchDialog", ctx)
            if dlg is None:
                fail_text = _("Failed to open search dialog.") + (f"\n\n{load_detail}" if load_detail else "")
                msgbox(ctx, _("Search Nearby Files"), fail_text, box_type=3)
                self.close()
                return

            self._dlg = dlg

            self._wire_listeners(dlg)
            self._refresh_cache_status(dlg)

            query_ctrl = dlg.getControl("QueryEdit")
            if query_ctrl is not None:
                query_ctrl.setFocus()

            owner = self

            class _TopWindowListener(BaseTopWindowListener):
                def on_window_closing(self, rEvent: Any) -> None:
                    owner.close(toolkit_teardown=True)

            self._top_listener = _TopWindowListener()
            dlg.addTopWindowListener(self._top_listener)
            dlg.setVisible(True)

            # Pre-warm the venv worker in the background asynchronously after the dialog is visible
            from plugin.framework.constants import WORKER_POOL_EMBEDDINGS
            run_in_background(warm_venv_worker, WORKER_POOL_EMBEDDINGS, name="warm-venv-worker", dedicated=True)

        except Exception:
            log.exception("SearchDialog._open failed")
            self.close()

    def _wire_listeners(self, dlg: Any) -> None:
        owner = self

        class _SearchListener(BaseActionListener):
            def actionPerformed(self, rEvent: Any) -> None:
                owner._run_search(dlg)

        class _RebuildListener(BaseActionListener):
            def actionPerformed(self, rEvent: Any) -> None:
                owner._run_rebuild(dlg)

        class _CancelListener(BaseActionListener):
            def actionPerformed(self, rEvent: Any) -> None:
                owner.close()

        dlg.getControl("BtnSearch").addActionListener(_SearchListener())
        dlg.getControl("BtnRebuild").addActionListener(_RebuildListener())
        dlg.getControl("BtnCancel").addActionListener(_CancelListener())

        class _SearchEnterKeyListener(BaseKeyListener):
            def on_key_pressed(self, e: Any) -> None:
                # Single-line edits have no newline use for Shift+Enter.
                if e.KeyCode != KEY_RETURN:
                    return
                btn_search = dlg.getControl("BtnSearch")
                if btn_search and not btn_search.getModel().Enabled:
                    # Search is already in progress
                    return
                owner._run_search(dlg)

        enter_listener = _SearchEnterKeyListener()
        for ctrl_name in ("QueryEdit", "RespEdit"):
            ctrl = dlg.getControl(ctrl_name)
            if ctrl is not None:
                ctrl.addKeyListener(enter_listener)

    def _resolve_doc_index_context(self) -> tuple[Any, tuple[Any, Any, Any, str]] | None:
        """Return (doc, resolve_index_context tuple) on the main thread, or None if no doc."""
        ctx = self._ctx
        from plugin.embeddings.embeddings_cache import resolve_index_context

        doc = get_active_document(ctx)
        if not doc:
            return None
        return doc, resolve_index_context(ctx, doc)

    def _refresh_cache_status(self, dlg: Any) -> None:
        ctx = self._ctx
        status_lbl = dlg.getControl("CacheStatusLbl")
        if not status_lbl:
            return

        def _apply_cache_status() -> None:
            def _get_status() -> str:
                try:
                    from plugin.embeddings.embeddings_cache import (
                        folder_store,
                        resolve_index_context,
                    )

                    doc = get_active_document(ctx)
                    if not doc:
                        return _("Cache Status: No active document")

                    folder_key, db_path, meta_path, listing_root = resolve_index_context(ctx, doc)
                    if folder_key is None or db_path is None or meta_path is None:
                        return _("Cache Status: Folder not resolved")

                    store = folder_store(listing_root, meta_path, db_path)
                    if store.is_empty:
                        if store.mode == "zvec":
                            return _("Cache Status: Not built (zvec)")
                        if store.mode == "lancedb":
                            return _("Cache Status: Not built (lancedb)")
                        return _("Cache Status: Not built")

                    updated_at_str = store.meta.data.get("updated_at")
                    if updated_at_str:
                        try:
                            updated_at = float(updated_at_str)
                            age_secs = time.time() - updated_at
                            age_str = format_cache_age(age_secs)
                            return _("Cache Status: Built ({0})").format(age_str)
                        except ValueError:
                            pass

                    return _("Cache Status: Built")
                except Exception as e:
                    return _("Cache Status: Error ({0})").format(str(e))

            status_lbl.getModel().Label = _get_status()

        main.call(_apply_cache_status)

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

        def _do_background_search() -> None:
            try:
                from plugin.framework.constants import folder_search_enabled
                from plugin.embeddings.embeddings_cache import folder_store
                from plugin.embeddings.embeddings_indexer import ensure_index_wakeup
                from plugin.embeddings.embedding_client import get_embedding_model
                from plugin.embeddings.embeddings_service import hybrid_search

                resolved = main.call(self._resolve_doc_index_context)
                if resolved is None:
                    self._finish(_("No active document found."), "BtnSearch")
                    return
                doc, (folder_key, db_path, meta_path, listing_root) = resolved

                if not folder_search_enabled():
                    self._finish(
                        _("Cross-file search is disabled. Enable Embeddings + FTS in Settings → Embeddings."),
                        "BtnSearch",
                    )
                    return

                if folder_key is None or db_path is None or meta_path is None:
                    self._finish(_("Error: ") + str(listing_root), "BtnSearch")
                    return

                store = folder_store(listing_root, meta_path, db_path)
                search_path = store.search_path
                model = ""
                if search_path is not None:
                    model = get_embedding_model()
                    # Do not query a vec store built for a different model.
                    if store.mode != "fts" and store.meta.query_blocked_for_model(model):
                        search_path = None
                if search_path is None:
                    main.call(ensure_index_wakeup, ctx, doc)
                    self._finish(
                        _("Folder index is building in the background. Please retry search shortly."),
                        "BtnSearch",
                    )
                    return

                result = hybrid_search(
                    ctx,
                    search_path,
                    str(query),
                    k,
                    model=model,
                    near_slop=10,
                    doc_url_filter=None,
                )
                hits = list(result.get("hits") or [])
                main.call(ensure_index_wakeup, ctx, doc)

                if not hits:
                    self._finish(_("No matches found."), "BtnSearch")
                    return

                formatted_lines = []
                for idx, hit in enumerate(hits, 1):
                    doc_url = hit.get("doc_url", "")
                    filename = doc_url.split("/")[-1]
                    score = hit.get("score", 0.0)
                    snippet = (hit.get("snippet") or "").strip()
                    formatted_lines.append(f"[{idx}] {filename} (Score: {score:.4f})")
                    formatted_lines.append(snippet)
                    formatted_lines.append("-" * 40)

                output_text = "\n".join(formatted_lines)
                self._finish(output_text, "BtnSearch")
                main.call(self._refresh_status_if_open)
            except Exception as e:
                log.exception("Background search failed")
                self._finish(_("Error running search: ") + str(e), "BtnSearch")

        run_in_background(_do_background_search, name="search-dialog-query", dedicated=True)

    def _run_rebuild(self, dlg: Any) -> None:
        ctx = self._ctx
        btn_rebuild = dlg.getControl("BtnRebuild")
        results_ctrl = dlg.getControl("ResultsEdit")
        if btn_rebuild:
            btn_rebuild.getModel().Enabled = False
        if results_ctrl:
            results_ctrl.getModel().Text = _("Rebuilding cache...")

        def _do_rebuild() -> None:
            try:
                from plugin.embeddings.embeddings_cache import clear_folder_cache
                from plugin.framework.constants import configured_folder_search_mode
                from plugin.embeddings.embedding_client import get_embedding_model

                resolved = main.call(self._resolve_doc_index_context)
                if resolved is None:
                    self._finish(_("No active document found."), "BtnRebuild")
                    return
                _doc, (_folder_key, _db_path, _meta_path, listing_root) = resolved
                if not listing_root:
                    self._finish(_("No active folder resolved."), "BtnRebuild")
                    return

                # Clear local cache files to force a full cold index rebuild
                clear_folder_cache(listing_root)

                model = get_embedding_model()

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
                        paragraphs = payload_paragraphs or info["paragraphs"]
                        chunks = payload_chunks or info["chunks"]
                        line = format_index_heartbeat_line(
                            str(file),
                            paragraphs=paragraphs,
                            chunks=chunks,
                            elapsed_sec=elapsed,
                        )

                        def ui_update() -> None:
                            try:
                                d = self._dlg
                                if not d:
                                    return
                                ctrl = d.getControl("ResultsEdit")
                                if ctrl:
                                    existing = ctrl.getModel().Text
                                    new_text = (existing + "\n" if existing else "") + line
                                    ctrl.getModel().Text = new_text
                            except Exception as exc:
                                log.debug("search rebuild heartbeat UI update failed (%s)", type(exc).__name__)

                        # The return value is unused, and call blocks this pipe holder
                        # for up to 30s when the UI thread is not pumping;
                        # bound_scope=None because `run_in_background` in `plugin/framework/worker_pool.py`
                        # (`caller_ctx = contextvars.copy_context()`) copies the caller context and a chat SendCancellation
                        # must not drop the progress paint (is_cancelled(None) is false;
                        # cancel_pending_work(scope) keeps a different scope).
                        main.post(ui_update, bound_scope=None)
                        del hb_data[file]

                # Use the service function (mocked in tests) to rebuild the cache
                try:
                    embeddings_service.maintain_folder_index(
                        ctx,
                        listing_root,
                        model=model,
                        mode="cold",
                        search_mode=configured_folder_search_mode(),
                        heartbeat_fn=heartbeat_fn,
                    )
                    self._finish(_("Cache rebuild completed successfully."), "BtnRebuild")
                except Exception as exc:
                    log.exception("maintain_folder_index failed during rebuild")
                    self._finish(_("Rebuild failed: ") + str(exc), "BtnRebuild")

                main.call(self._refresh_status_if_open)
            except Exception as e:
                log.exception("Cache rebuild failed")
                self._finish(_("Rebuild failed: ") + str(e), "BtnRebuild")

        run_in_background(_do_rebuild, name="search-dialog-rebuild", dedicated=True)

    def _refresh_status_if_open(self) -> None:
        dlg = self._dlg
        if dlg:
            self._refresh_cache_status(dlg)

    def _finish(self, text: str, button: str) -> None:
        from plugin.framework.queue_executor import main

        def _update() -> None:
            try:
                dlg = self._dlg
                if not dlg:
                    return
                results_ctrl = dlg.getControl("ResultsEdit")
                if results_ctrl:
                    results_ctrl.getModel().Text = text
                btn = dlg.getControl(button)
                if btn:
                    btn.getModel().Enabled = True
            except Exception as exc:
                log.debug("search UI update failed (%s)", type(exc).__name__)

        main.call(_update)


def show_search_dialog(ctx: Any) -> None:
    """Show the modeless search dialog."""
    try:
        SearchDialog.show(ctx)
    except Exception:
        log.exception("show_search_dialog failed")
