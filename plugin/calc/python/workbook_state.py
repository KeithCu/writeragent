# SPDX-License-Identifier: GPL-3.0-or-later
"""Workbook state — single runtime state per open Calc document."""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
import threading
import time
from typing import TYPE_CHECKING, Any, Callable
import weakref

from plugin.framework.errors import best_effort
from plugin.framework.queue_executor import main
from plugin.framework.stream_batch import DeadlineThread
from plugin.framework.thread_guard import unwrap_uno
from plugin.framework.uno_context import get_runtime_uid

if TYPE_CHECKING:
    from plugin.calc.python.function import WorkerResultSession
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
    raw = unwrap_uno(doc)
    if raw is not None and raw is not doc:
        objects.append(raw)
    return objects


class Debounce:
    """Per-workbook debounce: one DeadlineThread, many keyed callbacks."""

    def __init__(self, post: Callable[[Callable[[], None]], None] | None = None) -> None:
        self._post: Callable[[Callable[[], None]], None] = post if post is not None else main.post
        self._lock: threading.Lock = threading.Lock()
        self._entries: dict[str, tuple[Callable[[], None], float]] = {}
        self._timer: DeadlineThread | None = None

    def _arm_timer_locked(self) -> None:
        if not self._entries:
            if self._timer is not None:
                self._timer.cancel()
            return
        next_deadline = min(deadline for _, deadline in self._entries.values())
        delay = max(0.0, next_deadline - time.monotonic())
        if self._timer is None:

            def _fire() -> None:
                self._on_timer()

            self._timer = DeadlineThread(_fire, name="calc-debounce")
        self._timer.arm(delay)

    def _on_timer(self) -> None:
        now = time.monotonic()
        due: list[Callable[[], None]] = []
        with self._lock:
            for k, (fn, deadline) in list(self._entries.items()):
                if deadline <= now:
                    due.append(fn)
                    del self._entries[k]
            self._arm_timer_locked()
        for fn in due:
            self._post(fn)

    def schedule(self, key: str, delay: float, fn: Callable[[], None]) -> None:
        """Store *fn* and arm for *delay* seconds from now (reschedules *key*)."""
        with self._lock:
            self._entries[key] = (fn, time.monotonic() + delay)
            self._arm_timer_locked()

    def cancel(self, key: str) -> None:
        with self._lock:
            self._entries.pop(key, None)
            self._arm_timer_locked()

    def cancel_all(self) -> None:
        with self._lock:
            self._entries.clear()
            if self._timer is not None:
                self._timer.stop()
                self._timer = None

    def flush(self, key: str) -> None:
        with self._lock:
            ent = self._entries.pop(key, None)
            self._arm_timer_locked()
        if ent is not None:
            ent[0]()

    def rekey(self, old_key: str, new_key: str) -> None:
        with self._lock:
            ent = self._entries.pop(old_key, None)
            if ent is None:
                return
            self._entries.pop(new_key, None)
            self._entries[new_key] = ent
            self._arm_timer_locked()

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
    prior_keys: set[str] = field(default_factory=set)

    def scheduled_key_matches(self, scheduled: str) -> bool:
        """True when *scheduled* is this workbook's current or superseded lifecycle id."""
        if not scheduled:
            return False
        sk = scheduled
        k = self.key
        if k == sk or k == f"calc:{sk}" or f"calc:{k}" == sk:
            return True
        return sk in self.prior_keys

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
                if st.key == key or st.scheduled_key_matches(key):
                    _STATES.pop(obj, None)
                    to_clear.append(st)
            for doc_id, entry in list(_STATE_BY_DOC_ID.items()):
                if entry[0].key == key or entry[0].scheduled_key_matches(key):
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
                state.prior_keys.add(old_key)
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
            # Without RuntimeUID, mint the session id once per document object (not each call).
            import plugin.calc.python.workbook_lifecycle as _wb_lc

            key = _wb_lc.calc_workbook_base_session_id(doc)

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
