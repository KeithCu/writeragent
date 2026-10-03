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
import weakref
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
# Filled only when ``_lifecycle_key`` runs (UI thread). Off-main spill timers
# look this up and must not call getPropertyValue / getURL on the cached model.
# Weak keys so a collected document cannot leave an id that a new object reuses.
_LIFECYCLE_KEYS: weakref.WeakKeyDictionary[Any, str] = weakref.WeakKeyDictionary()
_LIFECYCLE_REFS_BY_KEY: dict[str, list[weakref.ReferenceType[Any]]] = {}
# PyUNO objects often reject weakref. Those ids are dropped on unload.
_LIFECYCLE_KEY_BY_DOC_ID: dict[int, str] = {}
_DOC_IDS_BY_LIFECYCLE_KEY: dict[str, set[int]] = {}


def _doc_objects(doc: Any) -> list[Any]:
    """*doc* and its unwrapped UNO target, without calling UNO methods."""
    objects = [doc]
    try:
        from plugin.framework.thread_guard import _unwrap_uno

        raw = _unwrap_uno(doc)
    except Exception:
        raw = doc
    if raw is not None and raw is not doc:
        objects.append(raw)
    return objects


def _remember_doc_lifecycle_key(doc: Any, key: str) -> None:
    """Remember *key* for later off-main lookup. Call only after UNO already ran."""
    if doc is None or not key:
        return
    objects = _doc_objects(doc)
    with _LOCK:
        for obj in objects:
            try:
                _LIFECYCLE_KEYS[obj] = key
            except TypeError:
                doc_id = id(obj)
                previous = _LIFECYCLE_KEY_BY_DOC_ID.get(doc_id)
                if previous and previous != key:
                    old_ids = _DOC_IDS_BY_LIFECYCLE_KEY.get(previous)
                    if old_ids is not None:
                        old_ids.discard(doc_id)
                        if not old_ids:
                            _DOC_IDS_BY_LIFECYCLE_KEY.pop(previous, None)
                _LIFECYCLE_KEY_BY_DOC_ID[doc_id] = key
                _DOC_IDS_BY_LIFECYCLE_KEY.setdefault(key, set()).add(doc_id)
                continue
            refs = _LIFECYCLE_REFS_BY_KEY.setdefault(key, [])
            if not any(existing() is obj for existing in refs):
                refs.append(weakref.ref(obj))


def _forget_doc_lifecycle_key(key: str) -> None:
    """Drop the cached key for an unloaded workbook."""
    if not key:
        return
    with _LOCK:
        for ref in _LIFECYCLE_REFS_BY_KEY.pop(key, ()):
            obj = ref()
            if obj is not None and _LIFECYCLE_KEYS.get(obj) == key:
                _LIFECYCLE_KEYS.pop(obj, None)
        for doc_id in _DOC_IDS_BY_LIFECYCLE_KEY.pop(key, ()):
            if _LIFECYCLE_KEY_BY_DOC_ID.get(doc_id) == key:
                _LIFECYCLE_KEY_BY_DOC_ID.pop(doc_id, None)


def lifecycle_key_if_known(doc: Any | None) -> str:
    """Lifecycle key recorded on the UI thread.

    No UNO. Off-main ``=PY()`` may hold a cached model only to post it back to
    the UI thread; reading RuntimeUID here would trip the thread guard.
    A missing document has no key: guessing the only open workbook would
    cancel the wrong timer after an id is reused.
    """
    if doc is None:
        return ""
    objects = _doc_objects(doc)
    with _LOCK:
        for obj in objects:
            try:
                found = _LIFECYCLE_KEYS.get(obj)
            except TypeError:
                found = _LIFECYCLE_KEY_BY_DOC_ID.get(id(obj))
            if found:
                return found
    return ""


def _lifecycle_key(doc: Any) -> str:
    key = ""
    try:
        if hasattr(doc, "getPropertyValue"):
            uid = doc.getPropertyValue("RuntimeUID")
            if uid:
                key = str(uid)
    except Exception:
        log.debug("python_workbook_lifecycle: RuntimeUID read failed", exc_info=True)
    if not key:
        key = calc_workbook_base_session_id(doc)
    _remember_doc_lifecycle_key(doc, key)
    return key


class _CalcPythonUnloadListener(BaseDocumentEventListener):
    _ctx: Any
    _workbook_session_id: str
    _lifecycle_key: str
    _doc_url: str
    _teardown_done: bool
    _calc_cleanup: bool
    _extra_session_ids: set[str]
    _extra_doc_urls: set[str]

    def __init__(
        self,
        ctx: Any,
        workbook_session_id: str,
        lifecycle_key: str,
        *,
        doc_url: str = "",
        calc_cleanup: bool = True,
    ) -> None:
        super().__init__()
        self._ctx = ctx
        self._workbook_session_id = workbook_session_id
        self._lifecycle_key = lifecycle_key
        self._doc_url = doc_url
        self._teardown_done = False
        self._calc_cleanup = calc_cleanup
        self._extra_session_ids = set()
        self._extra_doc_urls = set()

    def note_session(self, session_id: str) -> None:
        """Remember another worker session on this same document (rps + notebook)."""
        if session_id and session_id != self._workbook_session_id:
            self._extra_session_ids.add(session_id)

    def note_calc_identity(self, session_id: str, doc_url: str = "") -> None:
        """Remember a session id this workbook grew after Save.

        Bugfix: an unsaved file's worker id is ``calc:{uuid}``. After Save it
        becomes ``calc:{file URL}``. The listener kept only the first id, so
        close reset the uuid session and left the file-URL kernel warm.
        """
        if session_id and session_id != self._workbook_session_id:
            self._extra_session_ids.add(self._workbook_session_id)
            self._workbook_session_id = session_id
        if doc_url and doc_url != self._doc_url:
            if self._doc_url:
                self._extra_doc_urls.add(self._doc_url)
            self._doc_url = doc_url

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
        # Drop the id cache in the same unload that cancels spill timers.
        _forget_doc_lifecycle_key(self._lifecycle_key)
        if self._calc_cleanup:
            try:
                from plugin.calc.python.formula_locator_cache import FORMULA_LOCATION_CACHE

                FORMULA_LOCATION_CACHE.clear_document(self._lifecycle_key)
            except Exception:
                log.debug("python_workbook_lifecycle: formula cache clear failed", exc_info=True)
            session_ids = (self._workbook_session_id, *tuple(self._extra_session_ids))
            try:
                from plugin.calc.python.function import clear_in_memory_spill_state

                for url in (self._doc_url, *tuple(self._extra_doc_urls)):
                    clear_in_memory_spill_state(doc_url=url, lifecycle_key=self._lifecycle_key)
            except Exception:
                log.debug("python_workbook_lifecycle: spill state clear failed", exc_info=True)
            try:
                from plugin.calc.python.geometric_recalc import clear_in_memory_geometric_state

                for sid in session_ids:
                    clear_in_memory_geometric_state(workbook_key=sid)
            except Exception:
                log.debug("python_workbook_lifecycle: geometric state clear failed", exc_info=True)
            try:
                from plugin.scripting.session_manager import clear_active_calc_session

                for sid in session_ids:
                    if isinstance(sid, str) and sid.startswith("calc:"):
                        clear_active_calc_session(sid)
            except Exception:
                log.debug("python_workbook_lifecycle: active session clear failed", exc_info=True)
        for sid in (self._workbook_session_id, *tuple(self._extra_session_ids)):
            try:
                res = reset_python_session(self._ctx, sid)
                if res.get("status") != "ok":
                    log.debug("python_workbook_lifecycle: reset on unload failed for %s: %s", sid, res.get("message"))
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
        existing = _LISTENERS.get(key)
        if existing is not None:
            existing.note_calc_identity(session_id, doc_url)
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


def _script_lifecycle_key(doc: Any, session_id: str) -> str:
    """Stable listener key that does not record a Calc session for Writer/Draw."""
    try:
        if hasattr(doc, "getPropertyValue"):
            uid = doc.getPropertyValue("RuntimeUID")
            if uid:
                return f"py:{uid}"
    except Exception:
        log.debug("python_workbook_lifecycle: RuntimeUID read failed", exc_info=True)
    return f"py:{session_id}"


def ensure_python_session_cleared_on_unload(ctx: Any, doc: Any, session_id: str | None) -> None:
    """Drop *session_id* when *doc* closes.

    Calc ``calc:…`` reuses the =PY() listener (formula cache, spill, init).
    Writer/Draw ``rps:…`` and ``notebook:…`` share one listener per document.
    """
    if not session_id or doc is None or not _HAVE_UNO_DOC_EVENTS:
        return
    if session_id.startswith("calc:"):
        ensure_calc_workbook_unload_resets_python(ctx, doc)
        return
    key = _script_lifecycle_key(doc, session_id)
    with _LOCK:
        existing = _LISTENERS.get(key)
        if existing is not None:
            existing.note_session(session_id)
            return
        listener = _CalcPythonUnloadListener(ctx, session_id, key, calc_cleanup=False)
        _LISTENERS[key] = listener
    try:
        if hasattr(doc, "addDocumentEventListener"):
            doc.addDocumentEventListener(listener)
    except Exception:
        with _LOCK:
            _LISTENERS.pop(key, None)
        log.warning("python_workbook_lifecycle: addDocumentEventListener failed", exc_info=True)
