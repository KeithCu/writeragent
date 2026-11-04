# SPDX-License-Identifier: GPL-3.0-or-later
"""Workbook state — single runtime state per open Calc document."""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
import threading
from typing import TYPE_CHECKING, Any, Callable
import weakref

from plugin.framework.errors import best_effort
from plugin.framework.queue_executor import main
from plugin.framework.stream_batch import DeadlineThread
from plugin.framework.thread_guard import _unwrap_uno
from plugin.framework.uno_context import get_runtime_uid

if TYPE_CHECKING:
    from plugin.calc.python.recalc import WorkerResultSession
    from plugin.calc.python.spill import SpillRecord

log = logging.getLogger(__name__)

_LOCK = threading.RLock()
_STATES: weakref.WeakKeyDictionary[Any, WorkbookState] = weakref.WeakKeyDictionary()
# PyUNO objects often reject weakref. Those ids are stored with a strong reference
# to the object so Python cannot reuse the id for a different document before unload.
_STATE_BY_DOC_ID: dict[int, tuple[WorkbookState, Any]] = {}


def _doc_objects(doc: Any) -> list[Any]:
    """*doc* and its unwrapped UNO target, without calling UNO methods."""
    objects = [doc]
    raw = _unwrap_uno(doc)
    if raw is not None and raw is not doc:
        objects.append(raw)
    return objects


def _invoke_fn(f: Callable[..., None], k: str) -> None:
    try:
        f(k)
    except TypeError:
        f()


@dataclass
class _DebounceEntry:
    fn: Callable[..., None]
    thread: DeadlineThread | None = None


class Debounce:
    """Per-workbook debounce coordinator using DeadlineThreads."""

    _test_sync: bool = False

    def __init__(self) -> None:
        self._lock: threading.Lock = threading.Lock()
        self._entries: dict[str, _DebounceEntry] = {}
        self._local_test_sync: bool = False

    def schedule(self, key: str, delay: float, fn: Callable[..., None]) -> None:
        """Store *fn* and arm the key's DeadlineThread for *delay*, posting to main on fire."""
        if self._local_test_sync or Debounce._test_sync:
            with self._lock:
                self._entries[key] = _DebounceEntry(fn=fn)

            def _fire_sync() -> None:
                with self._lock:
                    ent = self._entries.pop(key, None)
                if ent is not None and ent.fn is not None:
                    _invoke_fn(ent.fn, key)

            main.post(_fire_sync)
            return

        thread: DeadlineThread | None = None
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                entry = _DebounceEntry(fn=fn)
                self._entries[key] = entry

                def _fire(k: str = key) -> None:
                    target_fn: Callable[..., None] | None = None
                    with self._lock:
                        ent = self._entries.pop(k, None)
                        if ent is not None:
                            target_fn = ent.fn
                    if target_fn is not None:
                        main.post(lambda f=target_fn, target_k=k: _invoke_fn(f, target_k))

                thread = DeadlineThread(_fire, name=f"calc-debounce-{key}")
                entry.thread = thread
            else:
                entry.fn = fn
                thread = entry.thread
        if thread is not None:
            thread.arm(delay)

    def cancel(self, key: str) -> None:
        """Cancel and stop the thread for *key*."""
        with self._lock:
            entry = self._entries.pop(key, None)
        if entry is not None and entry.thread is not None:
            entry.thread.stop()

    def cancel_all(self) -> None:
        """Cancel and stop all debounce threads."""
        with self._lock:
            entries = list(self._entries.values())
            self._entries.clear()
        for ent in entries:
            if ent.thread is not None:
                ent.thread.stop()

    def flush(self, key: str) -> None:
        """Cancel the timer and invoke *fn* synchronously on the current thread."""
        with self._lock:
            entry = self._entries.pop(key, None)
        if entry is not None:
            if entry.thread is not None:
                entry.thread.stop()
            if entry.fn is not None:
                _invoke_fn(entry.fn, key)

    def rekey(self, old_key: str, new_key: str) -> None:
        """Rekey an existing debounce entry."""
        with self._lock:
            entry = self._entries.pop(old_key, None)
            if entry is None:
                return
            old_new = self._entries.pop(new_key, None)
            if old_new is not None and old_new.thread is not None:
                old_new.thread.stop()
            new_entry = _DebounceEntry(fn=entry.fn)
            self._entries[new_key] = new_entry

            def _fire(k: str = new_key) -> None:
                target_fn: Callable[..., None] | None = None
                with self._lock:
                    ent = self._entries.pop(k, None)
                    if ent is not None:
                        target_fn = ent.fn
                if target_fn is not None:
                    main.post(lambda f=target_fn, target_k=k: _invoke_fn(f, target_k))

            thread = DeadlineThread(_fire, name=f"calc-debounce-{new_key}")
            new_entry.thread = thread
            if entry.thread is not None:
                entry.thread.stop()
            thread.arm(0.1)

    def __contains__(self, key: str) -> bool:
        with self._lock:
            return key in self._entries

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def keys(self) -> list[str]:
        with self._lock:
            return list(self._entries.keys())


@dataclass
class WorkbookState:
    key: str
    spills: dict[tuple[str, int, int], "SpillRecord"] = field(default_factory=dict)  # (sheet, row, col)
    loaded: bool = False
    listeners: dict[str, Any] = field(default_factory=dict)  # sheet name -> dispatcher
    matrix: dict[tuple[Any, ...], "WorkerResultSession"] = field(default_factory=dict)
    debounce: Debounce = field(default_factory=Debounce)

    def clear(self) -> None:
        """Drop this workbook's spills, listeners, matrix, and debounce timers."""
        self.spills.clear()
        self.listeners.clear()
        self.matrix.clear()
        self.debounce.cancel_all()
        self.loaded = False

    @classmethod
    def _remember(cls, doc: Any, state: WorkbookState) -> None:
        if doc is None:
            return
        with _LOCK:
            for obj in _doc_objects(doc):
                # Strong ref: PyUNO often rejects weakref, and id() can be reused.
                _STATE_BY_DOC_ID[id(obj)] = (state, obj)
                try:
                    _STATES[obj] = state
                except TypeError:
                    pass

    @classmethod
    def forget_key(cls, key: str) -> None:
        """Drop the cached state for an unloaded workbook."""
        if not key:
            return
        with _LOCK:
            to_clear: list[WorkbookState] = []
            for obj, st in list(_STATES.items()):
                if st.key == key:
                    _STATES.pop(obj, None)
                    to_clear.append(st)
            for doc_id, entry in list(_STATE_BY_DOC_ID.items()):
                if entry[0].key == key:
                    _STATE_BY_DOC_ID.pop(doc_id, None)
                    to_clear.append(entry[0])
            for st in to_clear:
                st.clear()

    @classmethod
    def all_states(cls) -> list[WorkbookState]:
        """Return all active WorkbookState instances across all open documents."""
        with _LOCK:
            seen: set[int] = set()
            res: list[WorkbookState] = []
            for st in _STATES.values():
                if id(st) not in seen:
                    seen.add(id(st))
                    res.append(st)
            for entry in _STATE_BY_DOC_ID.values():
                st = entry[0]
                if id(st) not in seen:
                    seen.add(id(st))
                    res.append(st)
            return res

    @classmethod
    def reset_for_tests(cls) -> None:
        """Reset all workbook states. For tests only."""
        with _LOCK:
            _STATES.clear()
            _STATE_BY_DOC_ID.clear()

    @classmethod
    def known(cls, doc: Any | None) -> WorkbookState | None:
        """Lookup existing state recorded on UI thread. No UNO."""
        if doc is None:
            return None
        objects = _doc_objects(doc)
        with _LOCK:
            for obj in objects:
                try:
                    found = _STATES.get(obj)
                except TypeError:
                    entry = _STATE_BY_DOC_ID.get(id(obj))
                    found = entry[0] if entry is not None else None
                if found is not None:
                    return found
        return None

    @classmethod
    def for_doc(cls, doc: Any | None) -> WorkbookState | None:
        """UI thread only: return existing state or create one."""
        if doc is None or not main.is_current():
            return None

        state = cls.known(doc)
        if state is not None:
            uid = get_runtime_uid(doc)
            if uid and uid != state.key:
                old_key = state.key
                state.key = uid
                from plugin.calc.python.workbook_lifecycle import rekey_listener

                rekey_listener(old_key, uid)
            return state

        # Bugfix: a property-only RuntimeUID read missed getRuntimeUID() and the attribute
        # and accepted non-strings. get_runtime_uid walks method, attribute, then property,
        # and keeps only str or int.
        uid = get_runtime_uid(doc)
        if uid:
            key = uid
        else:
            # Bugfix: What was wrong: with no RuntimeUID, every call minted a new
            # ``calc:unsaved:{uuid}`` and overwrote the remembered key, so a deferred
            # spill's scheduled key never matched the live key and the write returned
            # without touching the sheet.
            # How it happened: ``_lifecycle_key`` always called
            # ``calc_workbook_base_session_id``, and ``mint_document_key`` returns a
            # fresh uuid when the session property does not stick.
            # Why this change fixes it: a missing RuntimeUID reuses the key already
            # stored for this object. A new session id is computed only the first time.
            # A RuntimeUID read still replaces that fallback.
            import plugin.calc.python.workbook_lifecycle as _wb_lc

            key = _wb_lc.calc_workbook_base_session_id(doc)

        url = doc.getURL() if hasattr(doc, "getURL") else ""
        for other in cls.all_states():
            if (
                other.key == key
                or f"calc:{other.key}" == key
                or other.key == f"calc:{key}"
                or (url and (other.key == url or other.key == f"calc:{url}"))
            ):
                other.key = key
                cls._remember(doc, other)
                return other

        state = cls(key=key)
        cls._remember(doc, state)

        # Register unload listener the first time the state is created
        from plugin.calc.python.workbook_lifecycle import ensure_calc_workbook_unload_resets_python
        from plugin.framework.uno_context import get_ctx

        best_effort(
            lambda: ensure_calc_workbook_unload_resets_python(get_ctx(), doc, state=state),
            "python workbook unload listener install",
        )
        return state
