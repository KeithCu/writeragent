# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Drop in-memory Python worker sessions when a Calc workbook closes.

Init scripts run once per workbook *open* in the warm worker. Without ``OnUnload``,
closing and reopening the same file (same URL / session key) would reuse the cached
``calc:…:init`` executor. Clearing on unload matches the expectation that init runs
again when the spreadsheet is opened later.

Wired from ``get_python_init_kwargs`` on the first ``=PY()`` for a workbook.
``reset_sandbox_session`` on the ``calc:…`` id also drops the companion ``:init``
session. Init-script *edits* still invalidate via hash + ``reset_python_session``
on save.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from plugin.framework.uno_listeners import BaseDocumentEventListener
from plugin.scripting.session_manager import calc_workbook_base_session_id
from plugin.scripting.venv_worker import reset_python_session

log = logging.getLogger(__name__)

_HAVE_UNO_DOC_EVENTS = False
try:
    import unohelper as _unohelper_impl  # noqa: F401  # pyright: ignore[reportUnusedImport]
    from com.sun.star.document import XDocumentEventListener as _XDocumentEventListener_impl  # noqa: F401  # pyright: ignore[reportUnusedImport]

    _HAVE_UNO_DOC_EVENTS = True
except ImportError:
    pass

_LOCK = threading.Lock()
_LISTENERS: dict[str, "_CalcPythonUnloadListener"] = {}


def _lifecycle_key(doc: Any) -> str:
    try:
        if hasattr(doc, "getPropertyValue"):
            uid = doc.getPropertyValue("RuntimeUID")
            if uid:
                return str(uid)
    except Exception:
        log.debug("python_workbook_lifecycle: RuntimeUID read failed", exc_info=True)
    return calc_workbook_base_session_id(doc)


class _CalcPythonUnloadListener(BaseDocumentEventListener):
    def __init__(
        self,
        ctx: Any,
        workbook_session_id: str,
        lifecycle_key: str,
        *,
        doc_url: str = "",
    ) -> None:
        super().__init__()
        self._ctx = ctx
        self._workbook_session_id = workbook_session_id
        self._lifecycle_key = lifecycle_key
        self._doc_url = doc_url
        self._teardown_done = False

    def on_document_event(self, Event: Any) -> None:
        try:
            name = getattr(Event, "EventName", "") or ""
        except Exception:
            return
        if name == "OnUnload":
            self._teardown()

    def on_disposing(self, Source: Any) -> None:
        self._teardown()

    def _teardown(self) -> None:
        if self._teardown_done:
            return
        self._teardown_done = True
        with _LOCK:
            _LISTENERS.pop(self._lifecycle_key, None)
        try:
            from plugin.calc.python.formula_locator_cache import FORMULA_LOCATION_CACHE

            FORMULA_LOCATION_CACHE.clear_document(self._lifecycle_key)
        except Exception:
            log.debug("python_workbook_lifecycle: formula cache clear failed", exc_info=True)
        try:
            from plugin.calc.python.function import clear_in_memory_spill_state

            clear_in_memory_spill_state(doc_url=self._doc_url, lifecycle_key=self._lifecycle_key)
        except Exception:
            log.debug("python_workbook_lifecycle: spill state clear failed", exc_info=True)
        try:
            from plugin.calc.python.geometric_recalc import clear_in_memory_geometric_state

            clear_in_memory_geometric_state(workbook_key=self._workbook_session_id)
        except Exception:
            log.debug("python_workbook_lifecycle: geometric state clear failed", exc_info=True)
        try:
            from plugin.scripting.session_manager import clear_active_calc_session

            clear_active_calc_session(self._workbook_session_id)
        except Exception:
            log.debug("python_workbook_lifecycle: active session clear failed", exc_info=True)
        try:
            res = reset_python_session(self._ctx, self._workbook_session_id)
            if res.get("status") != "ok":
                log.debug(
                    "python_workbook_lifecycle: reset on unload failed for %s: %s",
                    self._workbook_session_id,
                    res.get("message"),
                )
        except Exception:
            log.debug("python_workbook_lifecycle: reset on unload raised", exc_info=True)


def ensure_calc_workbook_unload_resets_python(ctx: Any, doc: Any) -> None:
    """Register a one-time listener so closing *doc* clears worker init/cell sessions."""
    if not _HAVE_UNO_DOC_EVENTS or doc is None:
        return
    key = _lifecycle_key(doc)
    session_id = calc_workbook_base_session_id(doc)
    doc_url = ""
    try:
        doc_url = getattr(doc, "getURL", lambda: "")() or ""
    except Exception:
        doc_url = ""
    with _LOCK:
        if key in _LISTENERS:
            return
        listener = _CalcPythonUnloadListener(ctx, session_id, key, doc_url=doc_url)
        _LISTENERS[key] = listener
    try:
        if hasattr(doc, "addDocumentEventListener"):
            doc.addDocumentEventListener(listener)
    except Exception:
        with _LOCK:
            _LISTENERS.pop(key, None)
        log.warning("python_workbook_lifecycle: addDocumentEventListener failed", exc_info=True)
