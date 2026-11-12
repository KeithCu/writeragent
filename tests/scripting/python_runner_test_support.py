# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Test support utilities for python_runner."""

from __future__ import annotations

import logging
from typing import Any

from plugin.scripting.editor_errors import exception_traceback
from plugin.scripting.helper_domain import rps_error_outcome
from plugin.scripting import python_runner

log = logging.getLogger("writeragent.scripting")


def execute_and_insert_result(ctx: Any, doc: Any, code: str, *, data_range: str | None = None) -> dict[str, Any]:
    """Run code in the user venv and insert the result into doc when possible (test entry)."""
    from plugin.scripting.session_manager import pin_script_document, release_script_document

    pin_token = pin_script_document(doc)
    try:
        prepared = python_runner._prepare_rps_execution(ctx, doc, code, data_range=data_range, pin_token=pin_token)
        if isinstance(prepared, python_runner.EarlyOutcome):
            return prepared.outcome
        try:
            response = python_runner._run_prepared_rps(prepared)
        except Exception as e:
            log.exception("execute_and_insert_result failed")
            return rps_error_outcome(str(e), t0=prepared.t0, traceback=exception_traceback(e))
        return python_runner._finish_rps_execution(prepared, response)
    finally:
        release_script_document(pin_token)
