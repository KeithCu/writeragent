# SPDX-License-Identifier: GPL-3.0-or-later
"""Test-only helpers for WorkbookState spills/listeners (replaces production dict proxies)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Iterator
from unittest.mock import MagicMock

from plugin.calc.python.workbook_state import WorkbookState


def ctx_with_doc(doc: Any = None) -> SimpleNamespace:
    """Build a minimal UNO-like context whose desktop returns *doc*."""
    desktop = MagicMock()
    desktop.getCurrentComponent.return_value = doc
    desktop.getComponents.return_value = None
    smgr = MagicMock()
    smgr.createInstanceWithContext.return_value = desktop
    return SimpleNamespace(ServiceManager=smgr, getServiceManager=lambda: smgr)


def reset_calc_workbook_states() -> None:
    _TEST_SENTINELS.clear()
    WorkbookState.reset_for_tests()


def workbook_state_for_doc(doc: Any) -> WorkbookState:
    """Return state for *doc*; requires ``main.is_current()`` or prior ``for_doc``."""
    st = WorkbookState.known(doc)
    if st is not None:
        return st
    st = WorkbookState.for_doc(doc)
    if st is None:
        raise AssertionError("workbook_state_for_doc: call on UI thread or register doc first")
    return st


def _state_for_doc_key(doc_key: str) -> WorkbookState | None:
    if not doc_key:
        return None
    for st in WorkbookState.all_states():
        if st.key == doc_key or st.scheduled_key_matches(doc_key):
            return st
    return None


_TEST_SENTINELS: dict[str, Any] = {}


def ensure_workbook_state(doc_key: str, doc: Any | None = None) -> WorkbookState:
    """Return or create a WorkbookState for unit tests (not used in production)."""
    if doc is not None:
        st = WorkbookState.known(doc) or WorkbookState.for_doc(doc)
        if st is not None:
            return st
    st = _state_for_doc_key(doc_key)
    if st is not None:
        return st
    sentinel = _TEST_SENTINELS.setdefault(doc_key, object())
    st = WorkbookState(key=doc_key)
    WorkbookState._remember(sentinel, st)
    return st


class _SpillRegistryTestAccess:
    def clear(self) -> None:
        for st in WorkbookState.all_states():
            st.spills.clear()

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, tuple) and len(key) == 4:
            doc_key, sheet, r, c = key
            st = _state_for_doc_key(str(doc_key))
            if st is None:
                raise KeyError(key)
            return st.spills[(str(sheet), int(r), int(c))]
        raise KeyError(key)

    def __setitem__(self, key: Any, value: Any) -> None:
        if isinstance(key, tuple) and len(key) == 4:
            doc_key, sheet, r, c = key
            st = _state_for_doc_key(str(doc_key))
            if st is None:
                st = ensure_workbook_state(str(doc_key))
            st.spills[(str(sheet), int(r), int(c))] = value
            return
        raise KeyError(key)

    def __contains__(self, key: Any) -> bool:
        try:
            self[key]
            return True
        except KeyError:
            return False

    def get(self, key: Any, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default

    def pop(self, key: Any, default: Any = None) -> Any:
        if isinstance(key, tuple) and len(key) == 4:
            doc_key, sheet, r, c = key
            st = _state_for_doc_key(str(doc_key))
            if st is None:
                return default
            sk = (str(sheet), int(r), int(c))
            if sk in st.spills:
                return st.spills.pop(sk)
            return default
        return default

    def keys(self) -> Iterator[tuple[Any, ...]]:
        for st in WorkbookState.all_states():
            for sheet, r, c in st.spills:
                yield (st.key, sheet, r, c)

    def __iter__(self) -> Iterator[tuple[Any, ...]]:
        return iter(list(self.keys()))

    def __len__(self) -> int:
        return sum(len(st.spills) for st in WorkbookState.all_states())


class _SheetModifyListenersTestAccess:
    def clear(self) -> None:
        for st in WorkbookState.all_states():
            st.listeners.clear()

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, tuple) and len(key) == 2:
            doc_key, sheet = key
            st = _state_for_doc_key(str(doc_key))
            if st is None or str(sheet) not in st.listeners:
                raise KeyError(key)
            return st.listeners[str(sheet)]
        raise KeyError(key)

    def __setitem__(self, key: Any, value: Any) -> None:
        if isinstance(key, tuple) and len(key) == 2:
            doc_key, sheet = key
            st = _state_for_doc_key(str(doc_key))
            if st is None:
                st = ensure_workbook_state(str(doc_key))
            st.listeners[str(sheet)] = value
            return
        raise KeyError(key)

    def __contains__(self, key: Any) -> bool:
        try:
            self[key]
            return True
        except KeyError:
            return False

    def __iter__(self) -> Iterator[tuple[Any, ...]]:
        for st in WorkbookState.all_states():
            for sheet in st.listeners:
                yield (st.key, sheet)


SPILL_REGISTRY = _SpillRegistryTestAccess()
SHEET_MODIFY_LISTENERS = _SheetModifyListenersTestAccess()
