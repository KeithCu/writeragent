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

This module also owns the product's one ``GlobalEventBroadcaster`` document-event
listener, installed once from ``menu_handlers.register_common_handlers``:

* On open events (``OnNew`` / ``OnLoad`` / ``OnCreate`` / ``OnLoadFinished``):
  geometric-recalc open and the ``WorkbookState`` unload-listener install.
* On ``OnLoadFinished``: Excel-PY convert and the Collabora ``GETPY`` prefix rewrite.
* On ``OnSave*Done``: Excel-PY export.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from plugin.calc.python.collabora_formula import maybe_rewrite_collabora_py_formulas
from plugin.calc.python.geometric_recalc import (
    clear_in_memory_geometric_state,
    maybe_geometric_on_document_open,
)
from plugin.calc.python.workbook_state import WorkbookState
from plugin.framework.errors import best_effort
from plugin.framework.queue_executor import main
from plugin.framework.uno_context import get_runtime_uid
from plugin.framework.uno_listeners import HAVE_UNO, BaseDocumentEventListener
from plugin.framework.worker_pool import run_in_background
from plugin.scripting.session_key import SessionKey, SessionKind
from plugin.scripting.session_manager import calc_workbook_base_session_id
from plugin.scripting.venv_worker import reset_python_session

log = logging.getLogger(__name__)

__all__ = [
    "ensure_calc_workbook_unload_resets_python",
    "ensure_python_session_cleared_on_unload",
    "install_global_document_event_listener",
    "is_calc_document",
    "rekey_listener",
    "reset_listeners_for_tests",
]

# Re-entrant: ensure_* holds this lock while calling note_*, and note_* /
# _teardown take it too. A plain Lock deadlocks that same-thread re-entry.
# note_calc_identity can also run off the main thread during unload.
_LOCK = threading.RLock()
_LISTENERS: dict[str, "_CalcPythonUnloadListener"] = {}


def rekey_listener(old: str, new: str) -> None:
    """Move the unload listener when a document's key is upgraded.

    Takes this module's ``_LOCK``. The listener moves from the old key to the
    new one, so unload finds it under the upgraded key and the new key does
    not install a second one. The listener's own ``_key`` moves too, so its
    unload removes the upgraded entry.
    """
    with _LOCK:
        listener = _LISTENERS.pop(old, None)
        if listener is not None:
            listener._key = new
            _LISTENERS[new] = listener


_UNLOAD_RESET_TIMEOUT_SEC: int = 3


def _should_retry(res: Any) -> bool:
    return (
        isinstance(res, dict)
        and res.get("status") == "error"
        and res.get("code") in ("WORKER_REENTRY", "VENV_TIMEOUT")
    )


class _CalcPythonUnloadListener(BaseDocumentEventListener):
    _key: str  # this listener's _LISTENERS key (rekey_listener moves it)
    _workbook_session_id: str
    _state: WorkbookState | None  # None for Writer/Draw rps:/notebook: listeners
    _teardown_done: bool
    _extra_session_ids: set[str]

    def __init__(
        self,
        key: str,
        workbook_session_id: str,
        state: WorkbookState | None = None,
    ) -> None:
        super().__init__()
        self._key = key
        self._workbook_session_id = workbook_session_id
        self._state = state
        self._teardown_done = False
        self._extra_session_ids = set()

    def note_session(self, session_id: str) -> None:
        """Remember another worker session on this same document (rps + notebook)."""
        with _LOCK:
            if not session_id or session_id == self._workbook_session_id:
                return
            self._extra_session_ids.add(session_id)

    def note_calc_identity(self, session_id: str) -> None:
        """Remember a session id this workbook grew after Save.

        An unsaved file's worker id is ``calc:{uuid}``. After Save it
        becomes ``calc:{file URL}``. Keeping only the first id resets the
        uuid session on close and leaves the file-URL kernel warm.

        The session id and the extra-session-id set are also read from ``_teardown``, which
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
        """Retry resetting a worker session that timed out or returned WORKER_REENTRY."""
        # Every caller waits for the pipe lock; this retry runs only after
        # a lease timeout, refusal, or IPC timeout.
        # This loop sleeps up to 5 s (ten 0.5 s waits), so run it on a dedicated background
        # worker. On the shared pool that sleep starves other tasks.
        res: Any = None
        for _attempt in range(10):
            time.sleep(0.5)
            # Check after the sleep: a reopen can register during it.
            if self._session_has_new_listener(sid):
                log.debug("python_workbook_lifecycle: %s reopened; dropping reset retry", sid)
                return
            try:
                res = reset_python_session(sid, lease_wait_sec=_UNLOAD_RESET_TIMEOUT_SEC)
            except Exception:
                log.debug("python_workbook_lifecycle: retry reset raised", exc_info=True)
                break
            if not _should_retry(res):
                return
        if _should_retry(res):
            log.warning("python_workbook_lifecycle: session %s still busy or timed out after retry timeout", sid)

    def _reset_one(self, sid: str) -> None:
        res = reset_python_session(
            sid,
            timeout_sec=_UNLOAD_RESET_TIMEOUT_SEC,
            lease_wait_sec=_UNLOAD_RESET_TIMEOUT_SEC,
        )
        if _should_retry(res):
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
        # The UI waits at most 3 s; every reset after that goes through
        # _retry_reset, which drops the id when the workbook is reopened.
        for sid in session_ids:
            if sid:
                best_effort(lambda: self._reset_one(sid), "python_workbook_lifecycle: reset on unload")

    def _release_calc_state(self, session_ids: tuple[str, ...]) -> None:
        """Drop this workbook's in-memory Calc state (Calc listeners only) and its worker kernels. Caller does not hold ``_LOCK``."""
        state = self._state
        if state is not None:
            # state.clear() drops only this workbook's matrix cache; other open workbooks keep theirs.
            best_effort(state.clear, "python_workbook_lifecycle: spill state clear")

            def _clear_geo() -> None:
                for sid in session_ids:
                    clear_in_memory_geometric_state(workbook_key=sid)

            best_effort(_clear_geo, "python_workbook_lifecycle: geometric state clear")
        self._reset_sessions(session_ids)

    def _teardown(self) -> None:
        # Take the lock: _teardown_done, the session id and the extra-session-id
        # set are written by note_calc_identity off the main thread, so copying
        # the set while it changes would raise, and a Save during unload must
        # not leave the new kernel out of the reset list.
        with _LOCK:
            if self._teardown_done:
                return
            self._teardown_done = True
            _LISTENERS.pop(self._key, None)
            session_ids = (self._workbook_session_id, *tuple(self._extra_session_ids))
        if self._state is not None:
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


def ensure_calc_workbook_unload_resets_python(doc: Any, *, state: WorkbookState) -> None:
    """Register a one-time listener so closing *doc* clears worker init/cell sessions."""
    if not HAVE_UNO or doc is None:
        return
    session_id = calc_workbook_base_session_id(doc)
    _register(
        doc,
        state.key,
        lambda: _CalcPythonUnloadListener(state.key, session_id, state),
        lambda existing: existing.note_calc_identity(session_id),
    )


def ensure_python_session_cleared_on_unload(doc: Any, session_id: str | None) -> None:
    """Drop *session_id* when *doc* closes.

    Calc ``calc:…`` reuses the =PY() listener (formula cache, spill, init).
    Writer/Draw ``rps:…`` and ``notebook:…`` share one listener per document.
    Writer/Draw listeners carry no WorkbookState: they reset worker sessions only.
    """
    if not session_id or doc is None or not HAVE_UNO:
        return
    if SessionKey.parse(session_id).kind is SessionKind.CALC:
        WorkbookState.for_doc(doc)
        return
    uid = get_runtime_uid(doc)
    key = f"py:{uid or session_id}"
    _register(
        doc,
        key,
        lambda: _CalcPythonUnloadListener(key, session_id),
        lambda existing: existing.note_session(session_id),
    )


_global_listener: Any = None

# Only *Done* events — OnSave/OnSaveAs also fire and would double-patch the file.
_SAVE_DONE_EVENTS = frozenset({"OnSaveDone", "OnSaveAsDone", "OnSaveToDone"})
# Factory ``private:factory/scalc`` fires OnNew, not OnLoadFinished. Geometric
# strip state and the unload listener are set up on these events too.
_GEOMETRIC_OPEN_EVENTS = frozenset({"OnLoadFinished", "OnNew", "OnLoad", "OnCreate"})


def is_calc_document(doc: Any) -> bool:
    """True when *doc* is a Calc SpreadsheetDocument, swallowing disposed/UNO errors."""
    try:
        return bool(doc and doc.supportsService("com.sun.star.sheet.SpreadsheetDocument"))
    except Exception:
        return False


def _geometric_open_job(ctx: Any, doc: Any) -> None:
    maybe_geometric_on_document_open(ctx, doc)
    # Wire OnUnload here so closing the workbook resets its kernel and
    # geometric state; client-side clear cannot reach soffice.
    if is_calc_document(doc):
        best_effort(lambda: WorkbookState.for_doc(doc), "python_workbook_lifecycle: unload listener install")


def _doc_from_event(event: Any) -> Any | None:
    # UNO getattr on a disposed ViewController raises RuntimeException (not AttributeError),
    # so getattr(..., None) is not enough — common on unload / Draw teardown during tests.
    # Factory ``OnNew`` / ``OnCreate`` for a hidden ``scalc`` often has Source=doc
    # but getCurrentController() throws — do not skip ``is_calc_document(source)``.
    try:
        controller = event.ViewController
        if controller is not None:
            return controller.getModel()
    except Exception:  # noqa: S110 - disposed ViewController; probe falls through to Source
        pass
    try:
        source = event.Source
    except Exception:
        source = None
    if source is not None and is_calc_document(source):
        return source
    try:
        if source is not None:
            ctrl = source.getCurrentController()
            if ctrl is not None:
                return ctrl.getModel()
    except Exception:  # noqa: S110 - getCurrentController throws on hidden factory OnNew; probe falls through
        pass
    return None


class _GlobalDocumentEventListener(BaseDocumentEventListener):
    _ctx: Any

    def __init__(self, ctx: Any) -> None:
        super().__init__()
        self._ctx = ctx

    def on_document_event(self, Event: Any) -> None:  # noqa: N803 -- UNO signature
        name = getattr(Event, "EventName", "") or ""
        doc = _doc_from_event(Event)
        # Hidden factory OnNew/OnCreate often has Source the Writer
        # keeper (leftover scalc is not focused), so the geometric
        # open hook still runs when ``doc is None``.
        if name in _GEOMETRIC_OPEN_EVENTS:
            try:
                log.info("python_workbook_lifecycle: geometric on_open event=%s has_doc=%s", name, doc is not None)
                # VCL OnNew/OnCreate is often not Python's MainThread. main.run inlines when
                # main.is_current() is true (including an untagged Dummy thread in dev
                # builds), so a leftover shared =PY() sees the session id. Release bundles
                # keep UNTAGGED_DUMMY_IS_CURRENT False, so a URP open there still marshals
                # and can time out.
                main.run(_geometric_open_job, self._ctx, doc)
            except Exception:
                log.warning("geometric recalc on open failed", exc_info=True)
        if doc is None:
            return
        if name == "OnLoadFinished" or name in _SAVE_DONE_EVENTS:
            # Lazy: auto_open imports is_calc_document from this module, and Run Python Script
            # imports this module in Writer.
            from plugin.calc.excel_py_convert.auto_open import (
                maybe_convert_excel_py_document,
                maybe_export_excel_py_on_save,
            )

            if name == "OnLoadFinished":
                # Runs inline because LibreOffice sends OnLoadFinished from inside
                # SfxBaseModel::load, which holds SolarMutex (sfx2/source/doc/sfxbasemodel.cxx
                # load, SfxModelGuard in sfx2/source/inc/docundomanager.hxx). Posting with
                # main.run from a release-build URP thread would wait 30 s for a main thread
                # that cannot take SolarMutex, and then skip the conversion.
                maybe_convert_excel_py_document(doc)
                try:
                    # URP OnLoadFinished can run on Dummy with SolarMutex held: main.run inlines when is_current().
                    # Pass the callable first—do not pass ctx as the first positional (main treats it as fn).
                    main.run(maybe_rewrite_collabora_py_formulas, doc)
                except Exception:
                    # FIXME: failures are logged and the open continues. This also hides a
                    # mis-wired main.run (TypeError); narrow it once a raise here is shown not
                    # to abort OnLoadFinished for other listeners.
                    log.warning("collabora PY rewrite on open failed", exc_info=True)
                return
            maybe_export_excel_py_on_save(self._ctx, doc)


def install_global_document_event_listener(ctx: Any) -> None:
    """Attach the one GlobalEventBroadcaster listener (open, load-finished, save-done events).

    The first caller claims the slot under ``_LOCK`` before any UNO call, so a second or
    re-entrant call returns without attaching a duplicate. A failed attach frees the slot.
    """
    global _global_listener
    listener = _GlobalDocumentEventListener(ctx)
    with _LOCK:
        if _global_listener is not None:
            return
        _global_listener = listener
    try:
        broadcaster = ctx.getServiceManager().createInstanceWithContext(
            "com.sun.star.frame.GlobalEventBroadcaster",
            ctx,
        )
        broadcaster.addDocumentEventListener(listener)
        log.debug("python_workbook_lifecycle: global document event listener attached")
    except Exception:
        with _LOCK:
            _global_listener = None
        log.warning("python_workbook_lifecycle: global document event listener install failed", exc_info=True)


def reset_listeners_for_tests() -> None:
    """Clear tracked unload listeners and the global event listener (tests only)."""
    global _global_listener
    with _LOCK:
        _LISTENERS.clear()
        _global_listener = None
