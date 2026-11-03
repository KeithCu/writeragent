# SPDX-License-Identifier: GPL-3.0-or-later
"""Workbook state — single runtime state per open Calc document."""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
import threading
from typing import TYPE_CHECKING, Any
import weakref

from plugin.framework.errors import best_effort
from plugin.framework.queue_executor import main
from plugin.framework.thread_guard import _unwrap_uno
from plugin.framework.uno_context import get_runtime_uid

if TYPE_CHECKING:
    from plugin.calc.python.function import SpillRecord, WorkerResultSession

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


@dataclass
class WorkbookState:
    key: str
    spills: dict[tuple[str, int, int], "SpillRecord"] = field(default_factory=dict)  # (sheet, row, col)
    loaded: bool = False
    listeners: dict[str, Any] = field(default_factory=dict)  # sheet name -> dispatcher
    matrix: dict[tuple[Any, ...], "WorkerResultSession"] = field(default_factory=dict)
    # debounce arrives in PR4

    def clear(self) -> None:
        """Drop this workbook's spills, listeners, and matrix."""
        self.spills.clear()
        self.listeners.clear()
        self.matrix.clear()
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
            for obj, st in list(_STATES.items()):
                if st.key == key:
                    _STATES.pop(obj, None)
            for doc_id, entry in list(_STATE_BY_DOC_ID.items()):
                if entry[0].key == key:
                    _STATE_BY_DOC_ID.pop(doc_id, None)

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
