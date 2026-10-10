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
import time
import weakref
from typing import Any

from plugin.framework.thread_guard import _unwrap_uno
from plugin.framework.uno_listeners import _HAVE_UNO as _HAVE_UNO_DOC_EVENTS
from plugin.framework.uno_listeners import BaseDocumentEventListener
from plugin.scripting.session_manager import calc_workbook_base_session_id
from plugin.scripting.venv_worker import reset_python_session

log = logging.getLogger(__name__)

# Re-entrant: ensure_* holds this lock while calling note_*, and note_* /
# _teardown take it too. A plain Lock deadlocks that same-thread re-entry.
# note_calc_identity can also run off the main thread during unload.
_LOCK = threading.RLock()
_LISTENERS: dict[str, "_CalcPythonUnloadListener"] = {}
# Filled only when ``_lifecycle_key`` runs (UI thread). Off-main spill timers
# look this up and must not call getPropertyValue / getURL on the cached model.
# Weak keys so a collected document cannot leave an id that a new object reuses.
_LIFECYCLE_KEYS: weakref.WeakKeyDictionary[Any, str] = weakref.WeakKeyDictionary()
_LIFECYCLE_REFS_BY_KEY: dict[str, list[weakref.ReferenceType[Any]]] = {}
# PyUNO objects often reject weakref. Those ids are stored with a strong reference
# to the object so Python cannot reuse the id for a different document before unload.
_LIFECYCLE_KEY_BY_DOC_ID: dict[int, tuple[str, Any]] = {}
_DOC_IDS_BY_LIFECYCLE_KEY: dict[str, set[int]] = {}


def _doc_objects(doc: Any) -> list[Any]:
    """*doc* and its unwrapped UNO target, without calling UNO methods."""
    objects = [doc]
    try:
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
                # Store (key, obj) so obj stays alive. When a PyUNO object
                # rejects weakref, id(obj) alone can be reused for a
                # different document and cancel the wrong spill timer.
                entry = _LIFECYCLE_KEY_BY_DOC_ID.get(doc_id)
                previous = entry[0] if entry is not None else None
                if previous and previous != key:
                    old_ids = _DOC_IDS_BY_LIFECYCLE_KEY.get(previous)
                    if old_ids is not None:
                        old_ids.discard(doc_id)
                        if not old_ids:
                            _DOC_IDS_BY_LIFECYCLE_KEY.pop(previous, None)
                _LIFECYCLE_KEY_BY_DOC_ID[doc_id] = (key, obj)
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
            entry = _LIFECYCLE_KEY_BY_DOC_ID.get(doc_id)
            if entry is not None and entry[0] == key:
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
                entry = _LIFECYCLE_KEY_BY_DOC_ID.get(id(obj))
                found = entry[0] if entry is not None else None
            if found:
                return found
    return ""


def _runtime_uid(doc: Any) -> str:
    """Read RuntimeUID from doc, or empty string on failure."""
    try:
        if hasattr(doc, "getPropertyValue"):
            uid = doc.getPropertyValue("RuntimeUID")
            if uid:
                return str(uid)
    except Exception:
        log.debug("python_workbook_lifecycle: RuntimeUID read failed", exc_info=True)
    return ""


def _lifecycle_key(doc: Any) -> str:
    key = _runtime_uid(doc) or calc_workbook_base_session_id(doc)
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
        with _LOCK:
            if not session_id or session_id == self._workbook_session_id:
                return
            self._extra_session_ids.add(session_id)

    def note_calc_identity(self, session_id: str, doc_url: str = "") -> None:
        """Remember a session id this workbook grew after Save.

        An unsaved file's worker id is ``calc:{uuid}``. After Save it
        becomes ``calc:{file URL}``. Keeping only the first id resets the
        uuid session on close and leaves the file-URL kernel warm.

        The session id and URL sets are also read from ``_teardown``, which
        can run on the main thread while this runs off-main. Both sides take
        ``_LOCK`` so a Save cannot mutate the set while unload iterates it.
        """
        with _LOCK:
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

    def _retry_reset(self, sid: str) -> None:
        """Retry resetting a worker session that returned WORKER_REENTRY."""
        # do_retry sleeps up to 5s, so run it on a dedicated background
        # worker. On the shared pool that sleep starves other tasks.
        last_res: Any = None
        for _ in range(10):
            time.sleep(0.5)
            try:
                last_res = reset_python_session(self._ctx, sid)
                if isinstance(last_res, dict) and last_res.get("status") == "error" and last_res.get("code") == "WORKER_REENTRY":
                    continue
                break
            except Exception:
                log.debug("python_workbook_lifecycle: retry reset raised", exc_info=True)
                break
        if isinstance(last_res, dict) and last_res.get("status") == "error" and last_res.get("code") == "WORKER_REENTRY":
            log.warning("python_workbook_lifecycle: session %s still WORKER_REENTRY after retry timeout", sid)

    def _reset_sessions(self, session_ids: tuple[str, ...]) -> None:
        for sid in session_ids:
            if not sid:
                continue
            try:
                res = reset_python_session(self._ctx, sid)
                if isinstance(res, dict) and res.get("status") == "error" and res.get("code") == "WORKER_REENTRY":
                    from plugin.framework.worker_pool import run_in_background

                    # A sleeping background job uses dedicated=True (AGENTS.md).
                    run_in_background(
                        lambda s=sid: self._retry_reset(s),
                        name="reset_python_session_retry",
                        dedicated=True,
                    )
                elif isinstance(res, dict) and res.get("status") != "ok":
                    log.debug("python_workbook_lifecycle: reset on unload failed for %s: %s", sid, res.get("message"))
            except Exception:
                log.debug("python_workbook_lifecycle: reset on unload raised", exc_info=True)

    def _release_calc_state(self, session_ids: tuple[str, ...], doc_urls: tuple[str, ...], lifecycle_key: str, *, reset_sessions: bool) -> None:
        """Drop in-memory Calc state and worker kernels. Caller does not hold ``_LOCK``."""
        if self._calc_cleanup:
            try:
                from plugin.calc.python.formula_locator_cache import FORMULA_LOCATION_CACHE

                FORMULA_LOCATION_CACHE.clear_document(lifecycle_key)
            except Exception:
                log.debug("python_workbook_lifecycle: formula cache clear failed", exc_info=True)
            try:
                from plugin.calc.python.function import clear_in_memory_spill_state

                # Deduplicate doc URLs and skip empty strings. Unsaved workbooks
                # have doc_url="", so clear using lifecycle_key when no URLs exist.
                # Note: clear_in_memory_spill_state already ends with clear_python_addin_cache().
                clean_urls = {u for u in doc_urls if u}
                if clean_urls:
                    for url in clean_urls:
                        clear_in_memory_spill_state(doc_url=url, lifecycle_key=lifecycle_key)
                elif lifecycle_key:
                    clear_in_memory_spill_state(doc_url="", lifecycle_key=lifecycle_key)
            except Exception:
                log.debug("python_workbook_lifecycle: spill state clear failed", exc_info=True)
            try:
                from plugin.calc.python.geometric_recalc import clear_in_memory_geometric_state

                for sid in session_ids:
                    clear_in_memory_geometric_state(workbook_key=sid)
            except Exception:
                log.debug("python_workbook_lifecycle: geometric state clear failed", exc_info=True)
        if reset_sessions:
            self._reset_sessions(session_ids)

    def _teardown(self) -> None:
        # Take the lock. _teardown_done, the session id, and the extra
        # URL and session sets are written by note_calc_identity off the
        # main thread. tuple(set) can raise, and a Save during unload
        # can leave the new kernel out of the reset list.
        with _LOCK:
            if self._teardown_done:
                return
            self._teardown_done = True
            _LISTENERS.pop(self._lifecycle_key, None)
            session_ids = (self._workbook_session_id, *tuple(self._extra_session_ids))
            doc_urls = (self._doc_url, *tuple(self._extra_doc_urls))
            lifecycle_key = self._lifecycle_key
        # Drop the id cache in the same unload that cancels spill timers.
        # ``_forget_doc_lifecycle_key`` takes ``_LOCK``; do that after the snapshot.
        _forget_doc_lifecycle_key(lifecycle_key)
        self._release_calc_state(session_ids, doc_urls, lifecycle_key, reset_sessions=True)


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
    uid = _runtime_uid(doc)
    return f"py:{uid or session_id}"


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
