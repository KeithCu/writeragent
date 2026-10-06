# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Authoritative caller document resolution for Calc =PY() / =PYTHON()."""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


def resolve_formula_document(ctx: Any, code: str, caller_doc: Any | None = None) -> Any | None:
    """Resolve the target Calc document for =PY() execution.

    - caller_doc is not None: authoritative caller passed via SC_ADDINARG_CALLER.
      No cross-doc fallback.
    - caller_doc is None and on the main thread: uses locate_formula_cell_in_open_docs
      semantics (preferred = _calc_document; zero hits there -> unique origin
      across open Calc docs; otherwise None).
    - Off-main with no caller: returns None (caller must rely on cached/unambiguous rules).
    """
    if caller_doc is not None:
        return caller_doc

    from plugin.framework.thread_guard import on_main_thread

    if not on_main_thread():
        return None

    try:
        from plugin.scripting.session_manager import _calc_document, record_active_calc_document
        from plugin.calc.python.formula_locator_cache import locate_formula_cell_in_open_docs

        preferred = _calc_document(ctx)
        if not code:
            if preferred is not None:
                record_active_calc_document(preferred)
            return preferred

        located = locate_formula_cell_in_open_docs(ctx, preferred, code)
        if located is not None:
            doc = located[0]
            if doc is not None:
                record_active_calc_document(doc)
            return doc
    except Exception:
        log.debug("resolve_formula_document fallback lookup failed", exc_info=True)

    return None
