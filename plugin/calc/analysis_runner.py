# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared trusted analysis execution for Calc tools and Run Python Script."""

from __future__ import annotations

from typing import Any

from plugin.scripting.client import run_analysis
from plugin.scripting.analysis import HELPER_NAMES


def run_trusted_analysis(
    uno_ctx: Any,
    doc: Any,
    *,
    helper: str,
    **kwargs: Any,
) -> dict[str, Any]:
    """Fetch Calc data and run a trusted helper in the user venv."""
    from plugin.scripting.helper_domain import run_trusted_calc_data_helper

    # What was wrong: analysis_runner used run_trusted_calc_data_helper's on_main_thread()
    # short-circuit, so an untagged PyUNO Dummy-N thread ran the sheet read inline.
    # How it happened: run_trusted_analysis was refactored to share the helper without
    # preserving the original execute_on_main_thread force-enqueue behavior.
    # Why this change: pass force_queue=True so Dummy-N worker threads enqueue to the main thread.
    return run_trusted_calc_data_helper(
        uno_ctx,
        doc,
        helper=helper,
        helper_names=HELPER_NAMES,
        error_code="ANALYSIS_ERROR",
        empty_data_message="No data to analyze",
        client_run=run_analysis,
        force_queue=True,
        **kwargs,
    )
