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
from plugin.framework.uno_context import get_active_document, get_extension_url
from plugin.framework.uno_listeners import BaseActionListener
from plugin.chatbot.config_ui_helpers import populate_combobox_with_lru
from plugin.chatbot.dialogs import set_control_text

log = logging.getLogger(__name__)


class EvalDashboard:
    """Evaluation dashboard dialog controller."""

    _ctx: Any

    def __init__(self, ctx: Any) -> None:
        self._ctx = ctx
        self._dlg: Any = None

    def show(self) -> None:
        smgr = self._ctx.getServiceManager()
        base_url = get_extension_url()
        dp = smgr.createInstanceWithContext("com.sun.star.awt.DialogProvider", self._ctx)
        self._dlg = dp.createDialog(base_url + "/Dialogs/EvalDialog.xdl")

        try:
            self._populate()
            if self._dlg:
                self._dlg.execute()
        finally:
            if self._dlg:
                self._dlg.dispose()

    def _populate(self) -> None:
        assert self._dlg is not None
        endpoint_ctrl = self._dlg.getControl("endpoint")
        set_control_text(endpoint_ctrl, get_config_str("endpoint"))

        model_ctrl = self._dlg.getControl("models")
        current_model = str(get_text_model())
        current_endpoint = get_config_str("endpoint").strip()
        populate_combobox_with_lru(self._ctx, model_ctrl, current_model, "model_lru", current_endpoint)

        self._dlg.getControl("btn_run").addActionListener(EvalRunListener(self._ctx, self._dlg))
        self._dlg.getControl("btn_close").addActionListener(SimpleCloseListener(self._dlg))


class EvalRunListener(BaseActionListener):
    """Listener to run benchmark suite in response to Run button."""

    ctx: Any
    dialog: Any
    is_running: bool
    _job: Any

    def __init__(self, ctx: Any, dialog: Any) -> None:
        self.ctx = ctx
        self.dialog = dialog
        self.is_running = False
        self._job = None

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
            # What was wrong: Run called the suite on the dialog action thread
            # and did not return until every model call finished, so the modal
            # dialog could not paint. The suite now runs here. Each test posts
            # its line back; document edits stay on the main thread inside the
            # runner. An exception before the summary dict used to leave the
            # status on "Running...".
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
            try:
                area = self.dialog.getControl("log_area")
                current = area.getText() if hasattr(area, "getText") else ""
                area.setText(current + f"[{result.get('status')}] {result.get('name')}\n")
                from plugin.framework.uno_context import process_events_to_idle

                process_events_to_idle(self.ctx)
            except Exception:
                log.debug("eval progress paint failed", exc_info=True)

        post_to_main_thread(_paint)

    def _show_summary(self, model_name: str, summary: dict[str, Any]) -> None:
        try:
            log_text = f"Benchmarks Complete for {model_name}!\n"
            log_text += f"Passed: {summary['passed']}, Failed: {summary['failed']}\n"
            log_text += f"Total Est. Cost: ${summary['total_cost']:.4f}\n\n Details:\n"
            for res in cast("list[dict[str, Any]]", summary["results"]):
                log_text += f"[{res['status']}] {res['name']} ({res.get('latency', 0):.1f}s)\n"
            self.dialog.getControl("log_area").setText(log_text)
            self.dialog.getControl("status").setText("Finished")
        finally:
            self.is_running = False

    def _show_failure(self, exc: BaseException) -> None:
        # What was wrong: a failure before the summary dict skipped the Finished
        # status. The action listener only cleared is_running, so the dialog
        # stayed on "Running...". The status line is set here either way.
        try:
            self.dialog.getControl("log_area").setText(f"Benchmark failed:\n{exc}\n")
            self.dialog.getControl("status").setText("Finished")
        except Exception:
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
