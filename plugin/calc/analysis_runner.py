# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared trusted analysis execution for Calc tools and Run Python Script."""

from __future__ import annotations

from typing import Any

from plugin.calc.address_utils import format_range_address, qualify_a1
from plugin.calc.bridge import CalcBridge
from plugin.doc.doc_type import is_calc
from plugin.scripting.client import run_analysis
from plugin.framework.errors import uno_read
from plugin.scripting.analysis import HELPER_NAMES


def calc_selection_to_a1(doc: Any) -> str | None:
    """Format the current Calc selection as a sheet-qualified A1 address."""
    if doc is None or not is_calc(doc):
        return None

    def _get() -> str | None:
        controller = doc.getCurrentController()
        if controller is None:
            return None
        selection = controller.getSelection()
        if selection is None or not hasattr(selection, "getRangeAddress"):
            return None
        addr = selection.getRangeAddress()
        bridge = CalcBridge(doc)
        sheet = bridge.get_active_sheet()
        sheet_name = str(sheet.getName())
        cell_part = format_range_address(addr, collapse_single=True)
        return qualify_a1(sheet_name, cell_part)

    return uno_read(_get, None)


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
