# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Host-side Harper grammar entry (no venv worker / trusted RPC).

Kept out of ``plugin.scripting.client`` so LibreHarper and the grammar queue can
call Harper without importing vision / trusted_rpc.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("writeragent.grammar")


def _pump_grammar_status_ui(ctx: Any) -> None:
    """Best-effort drain of grammar status UI on the LO main thread.

    Must never block or fail the Harper check: a busy VCL / delayed AsyncCallback
    used to raise TimeoutError from execute_on_main_thread(timeout=2.0) and abort
    linting even though status painting is optional.
    """
    from plugin.framework.queue_executor import post_to_main_thread, pump_main_thread_work_queue
    from plugin.framework.uno_context import get_toolkit

    def _pump() -> None:
        pump_main_thread_work_queue(max_items=8)
        toolkit = get_toolkit(ctx)
        if toolkit is not None and hasattr(toolkit, "processEventsToIdle"):
            toolkit.processEventsToIdle()

    try:
        post_to_main_thread(_pump)
    except Exception as e:
        log.warning("[grammar] Harper status UI pump skipped: %s", e)


def run_harper_check(ctx: Any, text: str, config_dir: str, *, bcp47: str = "en-US") -> dict[str, Any]:
    """Run Harper on the host (no venv worker). Downloads harper-ls into the user profile if needed."""
    from plugin.scripting.venv.harper import run_harper_check as _run_harper_in_process
    from plugin.writer.locale.grammar_obs import emit_harper_worker_status

    emit_harper_worker_status(text, "Starting Harper…")
    _pump_grammar_status_ui(ctx)

    def _on_progress(payload: dict[str, Any]) -> None:
        message = str(payload.get("message") or "").strip()
        if message:
            emit_harper_worker_status(text, message)
            _pump_grammar_status_ui(ctx)

    return _run_harper_in_process(text, config_dir, bcp47=bcp47, heartbeat_fn=_on_progress)
