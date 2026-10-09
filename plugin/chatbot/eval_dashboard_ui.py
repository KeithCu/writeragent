# WriterAgent - Prompt Optimization & Benchmark Eval Dashboard
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Eval Dashboard UI: dialog for running prompt optimization benchmark suites from LibreOffice."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, cast

from plugin.framework.config import get_config_str
from plugin.framework.client.model_fetcher import get_text_model
from plugin.framework.errors import is_disposed_exception
from plugin.framework.uno_context import get_active_document
from plugin.framework.uno_listeners import BaseActionListener
from plugin.chatbot.config_ui_helpers import populate_combobox_with_lru
from plugin.chatbot.dialogs import load_writeragent_dialog, set_control_text

log = logging.getLogger(__name__)


class EvalDashboard:
    """Evaluation dashboard dialog controller."""

    _ctx: Any
    _closed: bool

    def __init__(self, ctx: Any) -> None:
        self._ctx = ctx
        self._dlg: Any = None
        self._closed = False

    def show(self) -> None:
        # Translates at load. A missing dialog must not execute().
        self._dlg = load_writeragent_dialog("EvalDialog", self._ctx)
        self._closed = False

        try:
            if not self._dlg:
                return
            self._populate()
            self._dlg.execute()
        finally:
            # The catalog worker can still post after execute() returns.
            self._closed = True
            if self._dlg:
                self._dlg.dispose()
                self._dlg = None

    def _populate(self) -> None:
        assert self._dlg is not None
        endpoint_ctrl = self._dlg.getControl("endpoint")
        set_control_text(endpoint_ctrl, get_config_str("endpoint"))

        model_ctrl = self._dlg.getControl("models")
        current_model = str(get_text_model())
        current_endpoint = get_config_str("endpoint").strip()
        # Same freeze as Settings: fetch_available_models before execute()
        # blocked the UI on a dead endpoint. LRU now; the worker fills the list.
        populate_combobox_with_lru(
            self._ctx, model_ctrl, current_model, "model_lru", current_endpoint,
            skip_remote_fetch=True,
        )
        self._schedule_models_fetch(current_endpoint, current_model)

        self._dlg.getControl("btn_run").addActionListener(EvalRunListener(self._ctx, self._dlg, self))
        self._dlg.getControl("btn_close").addActionListener(SimpleCloseListener(self._dlg))

    def _apply_model_list(self, endpoint: str, current_model: str, models: Any) -> None:
        """Paint the model combo from a list already in hand. No catalog GET."""
        if self._closed or self._dlg is None:
            return
        try:
            ctrl = self._dlg.getControl("models")
        except Exception:
            log.debug("Eval dashboard model combo unavailable", exc_info=True)
            return
        if ctrl is None:
            return
        populate_combobox_with_lru(
            self._ctx, ctrl, current_model, "model_lru", endpoint,
            remote_models=models if isinstance(models, list) else None,
            skip_remote_fetch=not isinstance(models, list),
        )

    def _schedule_models_fetch(self, endpoint: str, current_model: str) -> None:
        from plugin.framework.client.model_fetcher import (
            cached_text_models,
            endpoint_url_suitable_for_v1_models_fetch,
            fetch_available_models,
        )
        from plugin.framework.queue_executor import post_to_main_thread
        from plugin.framework.worker_pool import run_in_background

        if not endpoint or not endpoint_url_suitable_for_v1_models_fetch(endpoint):
            return

        # Apply a catalog this process already holds on this turn. A miss
        # still fetches on the worker, then paints once.
        cached = cached_text_models(endpoint)
        if isinstance(cached, list):
            self._apply_model_list(endpoint, current_model, cached)
            return

        def _fetch_eval_models() -> None:
            models = fetch_available_models(endpoint)

            def _apply() -> None:
                self._apply_model_list(endpoint, current_model, models)

            post_to_main_thread(_apply)

        run_in_background(_fetch_eval_models, name="eval-models")



class EvalRunListener(BaseActionListener):
    """Listener to run benchmark suite in response to Run button."""

    ctx: Any
    dialog: Any
    is_running: bool
    _job: Any
    _owner: EvalDashboard | None

    def __init__(self, ctx: Any, dialog: Any, owner: EvalDashboard | None = None) -> None:
        self.ctx = ctx
        self.dialog = dialog
        self._owner = owner
        self.is_running = False
        self._job = None

    def _dialog_closed(self) -> bool:
        """True after show() disposes the dialog, or when the dialog is gone."""
        owner = self._owner
        if owner is not None and owner._closed:
            return True
        return self.dialog is None

    def on_action_performed(self, rEvent: Any) -> None:
        if self.is_running:
            return
        self.is_running = True
        self.run_suite()

    def run_suite(self) -> None:
        # TYPE_CHECKING is true for Pyright: do not follow tests.eval_runner → plugin.main.
        # Runtime TYPE_CHECKING is false: import stays lazy until Run.
        if TYPE_CHECKING:
            def run_benchmark_suite(*args: Any, **kwargs: Any) -> dict[str, Any]: ...
        else:
            try:
                from tests.eval_runner import run_benchmark_suite
            except ImportError:
                # Release OXTs pass --no-tests, so tests/ is not on the extension
                # path, and the Debug menu that opens this dialog is stripped.
                # make build still ships tests/eval_runner.py. Without this catch
                # the Run button raised ImportError inside the listener.
                self.dialog.getControl("log_area").setText(
                    "Evaluation benchmarks are not in this extension build.\n"
                    "Run scripts/prompt_optimization/run_eval.py, or use a dev\n"
                    "build (make build) that includes tests/eval_runner.py.\n"
                )
                self.dialog.getControl("status").setText("Unavailable")
                self.is_running = False
                return
        from plugin.framework.queue_executor import post_to_main_thread
        from plugin.framework.uno_context import process_events_to_idle
        from plugin.framework.worker_pool import run_in_background

        try:
            model_name = self.dialog.getControl("models").getText()
            categories = []
            for cat in ("writer", "calc", "draw", "multimodal"):
                if self.dialog.getControl(f"cat_{cat}").getState():
                    categories.append(cat.capitalize())

            self.dialog.getControl("log_area").setText(f"Starting benchmark for {model_name}...\n")
            self.dialog.getControl("status").setText("Running...")
            # One paint before the suite, so "Running..." is visible when Run returns.
            process_events_to_idle(self.ctx)
            doc = get_active_document(self.ctx)
        except Exception as exc:
            self._show_failure(exc)
            return

        def _job() -> None:
            # The suite runs here, off the dialog action thread, so the modal
            # can paint. Each test posts its line back; document edits stay on
            # the main thread inside the runner. An exception before the
            # summary dict still has to leave "Running...".
            try:
                summary = run_benchmark_suite(self.ctx, doc, model_name, categories, on_test_finished=self._after_test)
            except Exception as exc:
                post_to_main_thread(self._show_failure, exc)
                return
            post_to_main_thread(self._show_summary, model_name, summary)

        # dedicated: a full suite holds the worker for minutes and must not take a pool slot.
        self._job = run_in_background(_job, name="eval-benchmark", dedicated=True)

    def _after_test(self, result: dict[str, Any]) -> None:
        """Posted once per test so the dialog paints without waiting for the suite."""
        from plugin.framework.queue_executor import post_to_main_thread

        def _paint() -> None:
            # The suite worker outlives the dialog. Close returns from
            # execute() and show() disposes it, so a late post must not
            # call getControl. A closed window has nothing to paint.
            if self._dialog_closed():
                return
            try:
                area = self.dialog.getControl("log_area")
                current = area.getText() if hasattr(area, "getText") else ""
                area.setText(current + f"[{result.get('status')}] {result.get('name')}\n")
                from plugin.framework.uno_context import process_events_to_idle

                process_events_to_idle(self.ctx)
            except Exception as exc:
                if is_disposed_exception(exc):
                    log.debug("eval progress skipped; dialog disposed", exc_info=True)
                    return
                log.debug("eval progress paint failed", exc_info=True)

        post_to_main_thread(_paint)

    def _show_summary(self, model_name: str, summary: dict[str, Any]) -> None:
        try:
            # Same close race as _after_test. is_running still clears so a
            # later Run is not stuck after the dialog is already gone.
            if self._dialog_closed():
                return
            try:
                log_text = f"Benchmarks Complete for {model_name}!\n"
                log_text += f"Passed: {summary['passed']}, Failed: {summary['failed']}\n"
                log_text += f"Total Est. Cost: ${summary['total_cost']:.4f}\n\n Details:\n"
                for res in cast("list[dict[str, Any]]", summary["results"]):
                    log_text += f"[{res['status']}] {res['name']} ({res.get('latency', 0):.1f}s)\n"
                self.dialog.getControl("log_area").setText(log_text)
                self.dialog.getControl("status").setText("Finished")
            except Exception as exc:
                if not is_disposed_exception(exc):
                    raise
                log.debug("eval summary skipped; dialog disposed", exc_info=True)
        finally:
            self.is_running = False

    def _show_failure(self, exc: BaseException) -> None:
        # A failure before the summary dict still sets the status line.
        # The action listener only clears is_running, so skipping this leaves
        # the dialog on "Running...". A close during the suite disposes the
        # dialog before this post runs: skip the paint, still clear is_running.
        try:
            if self._dialog_closed():
                return
            self.dialog.getControl("log_area").setText(f"Benchmark failed:\n{exc}\n")
            self.dialog.getControl("status").setText("Finished")
        except Exception as paint_exc:
            if is_disposed_exception(paint_exc):
                log.debug("eval failure skipped; dialog disposed", exc_info=True)
            else:
                log.debug("eval failure status failed", exc_info=True)
        finally:
            self.is_running = False


class SimpleCloseListener(BaseActionListener):
    """Closes dialog on button click."""

    dialog: Any

    def __init__(self, dialog: Any) -> None:
        self.dialog = dialog

    def on_action_performed(self, rEvent: Any) -> None:
        self.dialog.endDialog(0)


def show_eval_dashboard(ctx: Any) -> None:
    """Show the evaluation dashboard dialog."""
    EvalDashboard(ctx).show()
