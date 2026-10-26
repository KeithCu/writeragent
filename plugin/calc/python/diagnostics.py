# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded per-workbook log of ``=PY()`` stdout / errors for the LibrePy sidebar.
Long stdout / message / traceback are clipped at record time; at most 16 workbooks are kept.

Recording from formula evaluation must stay UNO-light: only plain Python data.
Cell addresses are matched later when the sidebar refreshes.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any, Literal

from plugin.framework.errors import best_effort

DiagnosticFilter = Literal["all", "errors", "output"]

_MAX_ENTRIES_PER_WORKBOOK = 200
_CODE_SNIPPET_MAX = 240
_CLIP_STDOUT = 8000
_CLIP_MESSAGE = 2000
_CLIP_TRACEBACK = 8000
_MAX_WORKBOOKS = 16


def _clip(text: Any, limit: int) -> str:
    s = str(text or "")
    if len(s) <= limit:
        return s
    return f"{s[:limit]}\n… (truncated, {len(s) - limit} more characters)"


@dataclass(frozen=True)
class DiagnosticEntry:
    """One recorded ``=PY()`` evaluation outcome."""

    workbook_key: str
    code: str
    status: str  # "ok" | "error"
    message: str = ""
    stdout: str = ""
    traceback: str = ""
    timestamp: float = field(default_factory=time.time)
    sheet: str = ""
    address: str = ""

    @property
    def has_output(self) -> bool:
        return bool((self.stdout or "").strip())

    @property
    def is_error(self) -> bool:
        return self.status != "ok"

    def matches_filter(self, filt: DiagnosticFilter) -> bool:
        if filt == "errors":
            return self.is_error
        if filt == "output":
            return self.has_output or self.is_error
        return True

    def summary_line(self) -> str:
        """One-line label for list controls."""
        where = self.address or "(unknown cell)"
        if self.is_error:
            msg = (self.message or "error").strip().splitlines()[0][:80]
            return f"{where}: ERROR — {msg}"
        if self.has_output:
            out = self.stdout.strip().splitlines()[0][:80]
            return f"{where}: {out}"
        return f"{where}: ok"


class PythonDiagnosticsStore:
    """Thread-safe ring buffer of diagnostics keyed by workbook."""

    _max: int
    _lock: threading.Lock

    def __init__(self, *, max_entries: int = _MAX_ENTRIES_PER_WORKBOOK) -> None:
        self._max = max(1, int(max_entries))
        self._lock = threading.Lock()
        self._by_workbook: OrderedDict[str, deque[DiagnosticEntry]] = OrderedDict()
        self._listeners: list[Any] = []

    def record(self, *, workbook_key: str, code: str, status: str, message: str = "", stdout: str = "", traceback: str = "", sheet: str = "", address: str = "") -> DiagnosticEntry:
        key = (workbook_key or "unknown").strip() or "unknown"
        snippet = (code or "")[:_CODE_SNIPPET_MAX]
        entry = DiagnosticEntry(
            workbook_key=key,
            code=snippet,
            status="ok" if status == "ok" else "error",
            message=_clip(message, _CLIP_MESSAGE),
            stdout=_clip(stdout, _CLIP_STDOUT),
            traceback=_clip(traceback, _CLIP_TRACEBACK),
            sheet=str(sheet or ""),
            address=str(address or ""),
        )
        with self._lock:
            bucket = self._by_workbook.get(key)
            if bucket is None:
                # The store is a process-wide singleton and an evicted bucket is simply no longer listed.
                if len(self._by_workbook) >= _MAX_WORKBOOKS:
                    self._by_workbook.pop(next(iter(self._by_workbook)))
                bucket = deque(maxlen=self._max)
                self._by_workbook[key] = bucket
            else:
                self._by_workbook.move_to_end(key)
            bucket.append(entry)
            listeners = list(self._listeners)
        for cb in listeners:
            best_effort(lambda: cb(entry), "diagnostic listener failed")
        return entry

    def list_entries(self, workbook_key: str, *, filt: DiagnosticFilter = "all", newest_first: bool = True) -> list[DiagnosticEntry]:
        key = (workbook_key or "unknown").strip() or "unknown"
        with self._lock:
            bucket = list(self._by_workbook.get(key, ()))
        if newest_first:
            bucket.reverse()
        return [e for e in bucket if e.matches_filter(filt)]

    def latest_for_code(self, workbook_key: str, code: str) -> DiagnosticEntry | None:
        """Return the newest entry for exactly *code* (both sides clipped to 240 chars).

        Two different cells whose first 240 characters are identical are
        indistinguishable here (the cell list's locate_formula_cell_in_doc has
        the same limitation for duplicate code).
        """
        needle = (code or "")[:_CODE_SNIPPET_MAX]
        if not needle:
            return None
        for entry in self.list_entries(workbook_key, filt="all", newest_first=True):
            if entry.code == needle:
                return entry
        return None

    def add_listener(self, callback: Any) -> None:
        with self._lock:
            if callback not in self._listeners:
                self._listeners.append(callback)

    def remove_listener(self, callback: Any) -> None:
        with self._lock:
            if callback in self._listeners:
                self._listeners.remove(callback)


_STORE = PythonDiagnosticsStore()


def get_diagnostics_store() -> PythonDiagnosticsStore:
    return _STORE


def record_python_eval(*, workbook_key: str, code: str, status: str, message: str = "", stdout: str = "", traceback: str = "", sheet: str = "", address: str = "") -> DiagnosticEntry:
    """Record one ``=PY()`` outcome (safe to call from formula evaluation)."""
    return _STORE.record(workbook_key=workbook_key, code=code, status=status, message=message, stdout=stdout, traceback=traceback, sheet=sheet, address=address)


def diagnostics_workbook_key(ctx: Any, doc: Any | None = None) -> str:
    """Stable workbook key for the diagnostics store (UNO-light best effort).

    Uses the calling document only, and only reads an existing session id
    (``existing_calc_session_id`` does not mint a UDProp).
    """
    from plugin.framework.thread_guard import on_main_thread

    if doc is None or not on_main_thread():
        return "unknown"
    from plugin.scripting.session_manager import existing_calc_session_id

    return best_effort(lambda: existing_calc_session_id(doc) or "unknown", "diagnostics workbook key", "unknown")


def diagnostics_detail_text(entry: DiagnosticEntry) -> str:
    """Multi-line detail for the diagnostics text area."""
    lines = [f"Cell: {entry.address or '(unknown)'}", f"Status: {entry.status}", f"Time: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(entry.timestamp))}", "", "Code:", entry.code or "(empty)"]
    if entry.message:
        lines.extend(["", "Message:", entry.message.strip()])
    if entry.stdout:
        lines.extend(["", "stdout:", entry.stdout.strip()])
    if entry.traceback:
        lines.extend(["", "Traceback:", entry.traceback.strip()])
    return "\n".join(lines)
