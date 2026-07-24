# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded per-workbook log of ``=PY()`` stdout / errors for the LibrePy sidebar.

Recording from formula evaluation must stay UNO-light: only plain Python data.
Cell addresses are matched later when the sidebar refreshes.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Literal

DiagnosticFilter = Literal["all", "errors", "output"]

_MAX_ENTRIES_PER_WORKBOOK = 200
_CODE_SNIPPET_MAX = 240


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

    def __init__(self, *, max_entries: int = _MAX_ENTRIES_PER_WORKBOOK) -> None:
        self._max = max(1, int(max_entries))
        self._lock = threading.Lock()
        self._by_workbook: dict[str, deque[DiagnosticEntry]] = {}
        self._listeners: list[Any] = []

    def record(
        self,
        *,
        workbook_key: str,
        code: str,
        status: str,
        message: str = "",
        stdout: str = "",
        traceback: str = "",
        sheet: str = "",
        address: str = "",
    ) -> DiagnosticEntry:
        key = (workbook_key or "unknown").strip() or "unknown"
        snippet = (code or "")[:_CODE_SNIPPET_MAX]
        entry = DiagnosticEntry(
            workbook_key=key,
            code=snippet,
            status="ok" if status == "ok" else "error",
            message=str(message or ""),
            stdout=str(stdout or ""),
            traceback=str(traceback or ""),
            sheet=str(sheet or ""),
            address=str(address or ""),
        )
        with self._lock:
            bucket = self._by_workbook.get(key)
            if bucket is None:
                bucket = deque(maxlen=self._max)
                self._by_workbook[key] = bucket
            bucket.append(entry)
            listeners = list(self._listeners)
        for cb in listeners:
            try:
                cb(entry)
            except Exception:
                pass
        return entry

    def list_entries(
        self,
        workbook_key: str,
        *,
        filt: DiagnosticFilter = "all",
        newest_first: bool = True,
    ) -> list[DiagnosticEntry]:
        key = (workbook_key or "unknown").strip() or "unknown"
        with self._lock:
            bucket = list(self._by_workbook.get(key, ()))
        if newest_first:
            bucket.reverse()
        return [e for e in bucket if e.matches_filter(filt)]

    def latest_for_code(self, workbook_key: str, code: str) -> DiagnosticEntry | None:
        """Return the newest entry whose code prefix matches *code*."""
        needle = (code or "")[:_CODE_SNIPPET_MAX]
        if not needle:
            return None
        for entry in self.list_entries(workbook_key, filt="all", newest_first=True):
            if entry.code == needle or entry.code.startswith(needle) or needle.startswith(entry.code):
                return entry
        return None

    def clear(self, workbook_key: str | None = None) -> None:
        with self._lock:
            if workbook_key is None:
                self._by_workbook.clear()
            else:
                self._by_workbook.pop((workbook_key or "").strip() or "unknown", None)

    def add_listener(self, callback: Any) -> None:
        with self._lock:
            if callback not in self._listeners:
                self._listeners.append(callback)

    def remove_listener(self, callback: Any) -> None:
        with self._lock:
            try:
                self._listeners.remove(callback)
            except ValueError:
                pass


_STORE = PythonDiagnosticsStore()


def get_diagnostics_store() -> PythonDiagnosticsStore:
    return _STORE


def record_python_eval(
    *,
    workbook_key: str,
    code: str,
    status: str,
    message: str = "",
    stdout: str = "",
    traceback: str = "",
    sheet: str = "",
    address: str = "",
) -> DiagnosticEntry:
    """Record one ``=PY()`` outcome (safe to call from formula evaluation)."""
    return _STORE.record(
        workbook_key=workbook_key,
        code=code,
        status=status,
        message=message,
        stdout=stdout,
        traceback=traceback,
        sheet=sheet,
        address=address,
    )


def diagnostics_detail_text(entry: DiagnosticEntry) -> str:
    """Multi-line detail for the diagnostics text area."""
    lines = [
        f"Cell: {entry.address or '(unknown)'}",
        f"Status: {entry.status}",
        f"Time: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(entry.timestamp))}",
        "",
        "Code:",
        entry.code or "(empty)",
    ]
    if entry.message:
        lines.extend(["", "Message:", entry.message.strip()])
    if entry.stdout:
        lines.extend(["", "stdout:", entry.stdout.strip()])
    if entry.traceback:
        lines.extend(["", "Traceback:", entry.traceback.strip()])
    return "\n".join(lines)
