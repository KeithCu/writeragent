# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Drop in-memory Python worker sessions when a Calc workbook closes.

Init scripts run once per workbook *open* in the warm worker. Without ``OnUnload``,
closing and reopening the same file (same URL / session key) would reuse the cached
``calc:…:init`` executor. Clearing on unload matches the expectation that init runs
again when the spreadsheet is opened later.

Wired from ``WorkbookState.for_doc`` on create and on later UI-thread hits.
``reset_sandbox_session`` on the ``calc:…`` id also drops the companion ``:init``
session. Init-script *edits* still invalidate via hash + ``reset_python_session``
on save.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from plugin.calc.python.workbook_state import WorkbookState
from plugin.framework.errors import best_effort
from plugin.framework.uno_listeners import _HAVE_UNO as _HAVE_UNO_DOC_EVENTS
from plugin.framework.uno_listeners import BaseDocumentEventListener
from plugin.scripting.session_manager import calc_workbook_base_session_id
from plugin.scripting.venv_worker import reset_python_session

log = logging.getLogger(__name__)

__all__ = [
    "ensure_calc_workbook_unload_resets_python",
    "calc_workbook_base_session_id",
    "rekey_listener",
]

# Re-entrant: ensure_* holds this lock while calling note_*, and note_* /
# _teardown take it too. A plain Lock deadlocks that same-thread re-entry.
# note_calc_identity can also run off the main thread during unload.
_LOCK = threading.RLock()
_LISTENERS: dict[str, "_CalcPythonUnloadListener"] = {}


def rekey_listener(old: str, new: str) -> None:
    """Move the unload listener when a document's key is upgraded.

    Caller holds ``_LOCK``. Without the move the old key kept a listener, the
    new key installed a second one, and unload forgot only the new alias.
    """
    with _LOCK:
        listener = _LISTENERS.pop(old, None)
        if listener is not None:
            _LISTENERS[new] = listener


def _is_reentry(res: Any) -> bool:
    return isinstance(res, dict) and res.get("status") == "error" and res.get("code") == "WORKER_REENTRY"


class _CalcPythonUnloadListener(BaseDocumentEventListener):
    _ctx: Any
    _workbook_session_id: str
    _state: WorkbookState
    _teardown_done: bool
    _calc_cleanup: bool
    _extra_session_ids: set[str]

    def __init__(
        self,
        ctx: Any,
        workbook_session_id: str,
        state: WorkbookState,
        *,
        calc_cleanup: bool = True,
    ) -> None:
        super().__init__()
        self._ctx = ctx
        self._workbook_session_id = workbook_session_id
        self._state = state
        self._teardown_done = False
        self._calc_cleanup = calc_cleanup
        self._extra_session_ids = set()

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

    def on_document_event(self, Event: Any) -> None:
        name = getattr(Event, "EventName", "") or ""
        if name == "OnUnload":
            self._teardown()

    def on_disposing(self, Source: Any) -> None:
        self._teardown()

    def _session_has_new_listener(self, sid: str) -> bool:
        """True when another live listener owns *sid*: the workbook was reopened."""
        with _LOCK:
            return any(
                listener is not self
                and (listener._workbook_session_id == sid or sid in listener._extra_session_ids)
                for listener in _LISTENERS.values()
            )

    def _retry_reset(self, sid: str) -> None:
        """Retry resetting a worker session that returned WORKER_REENTRY."""
        # Every caller now waits for the pipe lock; this retry runs only after
        # a tool-RPC refusal or the 600 s backstop.
        # do_retry sleeps up to 5s, so run it on a dedicated background
        # worker. On the shared pool that sleep starves other tasks.
        res: Any = None
        for _attempt in range(10):
            time.sleep(0.5)
            # Check after the sleep: a reopen can register during it.
            if self._session_has_new_listener(sid):
                log.debug("python_workbook_lifecycle: %s reopened; dropping reset retry", sid)
                return
            try:
                res = reset_python_session(sid)
            except Exception:
                log.debug("python_workbook_lifecycle: retry reset raised", exc_info=True)
                break
            if not _is_reentry(res):
                return
        if _is_reentry(res):
            log.warning("python_workbook_lifecycle: session %s still WORKER_REENTRY after retry timeout", sid)

    def _reset_one(self, sid: str) -> None:
        res = reset_python_session(sid)
        if _is_reentry(res):
            from plugin.framework.worker_pool import run_in_background

            # A sleeping background job uses dedicated=True (AGENTS.md).
            # The retry aborts if a new listener appears for the same session (reopen).
            run_in_background(
                lambda: self._retry_reset(sid),
                name="reset_python_session_retry",
                dedicated=True,
            )
        elif isinstance(res, dict) and res.get("status") != "ok":
            log.debug("python_workbook_lifecycle: reset on unload failed for %s: %s", sid, res.get("message"))

    def _reset_sessions(self, session_ids: tuple[str, ...]) -> None:
        # OnUnload must finish a live reset before it returns. A background
        # reset races a reopen of the same calc: URL and wipes the new kernel.
        # reset_python_session returns without spawning when no child is alive,
        # so this thread waits only for in-place IPC on a worker that already
        # exists, and for any request already holding the pipe (bounded by
        # that request's budget).
        for sid in session_ids:
            if sid:
                best_effort(lambda: self._reset_one(sid), "python_workbook_lifecycle: reset on unload")

    def _release_calc_state(self, session_ids: tuple[str, ...], *args: Any) -> None:
        """Drop in-memory Calc state and worker kernels. Caller does not hold ``_LOCK``."""
        if self._calc_cleanup:
            # state.clear() drops this workbook's matrix; global clear_python_addin_cache
            # wiped every open workbook's =PY() matrix cache on one unload.
            best_effort(self._state.clear, "python_workbook_lifecycle: spill state clear")

            from plugin.calc.python.geometric_recalc import clear_in_memory_geometric_state

            def _clear_geo() -> None:
                for sid in session_ids:
                    clear_in_memory_geometric_state(workbook_key=sid)

            best_effort(_clear_geo, "python_workbook_lifecycle: geometric state clear")
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
            _LISTENERS.pop(self._state.key, None)
            session_ids = (self._workbook_session_id, *tuple(self._extra_session_ids))
        WorkbookState.forget_key(self._state.key)
        self._release_calc_state(session_ids)


def _register(doc: Any, key: str, factory: Any, on_existing: Any) -> None:
    """Both ensure_* paths share this lock, rollback, and warning."""
    with _LOCK:
        existing = _LISTENERS.get(key)
        if existing is not None:
            on_existing(existing)
            return
        listener = factory()
        _LISTENERS[key] = listener
    try:
        if hasattr(doc, "addDocumentEventListener"):
            doc.addDocumentEventListener(listener)
    except Exception:
        with _LOCK:
            _LISTENERS.pop(key, None)
        log.warning("python_workbook_lifecycle: addDocumentEventListener failed", exc_info=True)


def ensure_calc_workbook_unload_resets_python(ctx: Any, doc: Any, *, state: WorkbookState) -> None:
    """Register a one-time listener so closing *doc* clears worker init/cell sessions."""
    if not _HAVE_UNO_DOC_EVENTS or doc is None:
        return
    session_id = calc_workbook_base_session_id(doc)
    _register(
        doc,
        state.key,
        lambda: _CalcPythonUnloadListener(ctx, session_id, state),
        lambda existing: existing.note_calc_identity(session_id),
    )


def ensure_python_session_cleared_on_unload(ctx: Any, doc: Any, session_id: str | None) -> None:
    """Drop *session_id* when *doc* closes.

    Calc ``calc:…`` reuses the =PY() listener (formula cache, spill, init).
    Writer/Draw ``rps:…`` and ``notebook:…`` share one listener per document.
    """
    if not session_id or doc is None or not _HAVE_UNO_DOC_EVENTS:
        return
    from plugin.scripting.session_key import SessionKey, SessionKind

    if SessionKey.parse(session_id).kind is SessionKind.CALC:
        WorkbookState.for_doc(doc)
        return
    from plugin.framework.uno_context import get_runtime_uid

    uid = get_runtime_uid(doc)
    key = f"py:{uid or session_id}"
    state = WorkbookState.known(doc)
    if state is None:
        state = WorkbookState(key=key)
    _register(
        doc,
        key,
        lambda: _CalcPythonUnloadListener(ctx, session_id, state, calc_cleanup=False),
        lambda existing: existing.note_session(session_id),
    )
