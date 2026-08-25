# WriterAgent - Prompt Optimization & Benchmark Eval Dashboard
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Eval Dashboard UI: dialog for running prompt optimization benchmark suites from LibreOffice."""

from __future__ import annotations

from typing import Any, cast

from plugin.framework.config import get_config_str
from plugin.framework.client.model_fetcher import get_text_model
from plugin.framework.uno_context import get_active_document, get_extension_url
from plugin.framework.uno_listeners import BaseActionListener
from plugin.chatbot.config_ui_helpers import populate_combobox_with_lru
from plugin.chatbot.dialogs import set_control_text


class EvalDashboard:
    """Evaluation dashboard dialog controller."""

    def __init__(self, ctx: Any):
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

    def __init__(self, ctx: Any, dialog: Any):
        self.ctx = ctx
        self.dialog = dialog
        self.is_running = False

    def on_action_performed(self, rEvent: Any) -> None:
        if self.is_running:
            return
        self.is_running = True
        try:
            self.run_suite()
        finally:
            self.is_running = False

    def run_suite(self) -> None:
        from tests.eval_runner import run_benchmark_suite
        from plugin.framework.uno_context import process_events_to_idle

        model_name = self.dialog.getControl("models").getText()
        categories = []
        for cat in ("writer", "calc", "draw", "multimodal"):
            if self.dialog.getControl(f"cat_{cat}").getState():
                categories.append(cat.capitalize())

        self.dialog.getControl("log_area").setText(f"Starting benchmark for {model_name}...\n")
        self.dialog.getControl("status").setText("Running...")
        process_events_to_idle(self.ctx)

        doc = get_active_document(self.ctx)
        summary = cast("dict[str, Any]", run_benchmark_suite(self.ctx, doc, model_name, categories))

        log_text = f"Benchmarks Complete for {model_name}!\n"
        log_text += f"Passed: {summary['passed']}, Failed: {summary['failed']}\n"
        log_text += f"Total Est. Cost: ${summary['total_cost']:.4f}\n\n Details:\n"
        for res in cast("list[dict[str, Any]]", summary["results"]):
            log_text += f"[{res['status']}] {res['name']} ({res.get('latency', 0):.1f}s)\n"

        self.dialog.getControl("log_area").setText(log_text)
        self.dialog.getControl("status").setText("Finished")


class SimpleCloseListener(BaseActionListener):
    """Closes dialog on button click."""

    def __init__(self, dialog: Any):
        self.dialog = dialog

    def on_action_performed(self, rEvent: Any) -> None:
        self.dialog.endDialog(0)


def show_eval_dashboard(ctx: Any) -> None:
    """Show the evaluation dashboard dialog."""
    EvalDashboard(ctx).show()
