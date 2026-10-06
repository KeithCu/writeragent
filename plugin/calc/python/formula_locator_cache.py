# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Formula cell location and MRU/LRU coordinate caching for Calc =PY() formulas."""

from __future__ import annotations

from collections import OrderedDict
import logging
import re
import threading
import time
from typing import Any

log = logging.getLogger(__name__)

MAX_FORMULAS_PER_DOC = 4096
DEFAULT_DOC_CACHE_TTL_SECONDS = 300.0  # 5 minutes idle TTL

# Handles Calc-escaped double-quotes ("") inside string literals and optional add-in
# OriginalNames (WriterAgent / LibrePy, or Collabora GETPY before open-rewrite).
_PY_FORMULA_CODE_REGEX = re.compile(
    r"(?:ORG\.(?:EXTENSION\.[A-Z0-9_.]+|COLLABORAOFFICE\.SHEET\.ADDIN\.PYTHONCOMPUTEFUNCTIONS)\.)?"
    r'(?:GETPYTHON|GETPY|PYTHON|PY)\s*\(\s*"((?:[^"]|"")*)"',
    re.IGNORECASE,
)


class DocumentFormulaCache:
    """Internal LRU/MRU formula coordinate cache for a single Calc document."""

    _max_size: int

    def __init__(self, max_size: int = MAX_FORMULAS_PER_DOC) -> None:
        self._max_size = max_size
        self.last_accessed: float = time.monotonic()
        # Key: code_str -> Value: list of (sheet_name, row, col) in MRU order
        self._cache: OrderedDict[str, list[tuple[str, int, int]]] = OrderedDict()

    def get(self, code_str: str) -> list[tuple[str, int, int]]:
        self.last_accessed = time.monotonic()
        if code_str in self._cache:
            self._cache.move_to_end(code_str)
            return list(self._cache[code_str])
        return []

    def put(self, code_str: str, sheet_name: str, row: int, col: int) -> None:
        self.last_accessed = time.monotonic()
        coord = (sheet_name, int(row), int(col))
        if code_str in self._cache:
            coords = self._cache[code_str]
            if coord in coords:
                coords.remove(coord)
            coords.insert(0, coord)
            self._cache.move_to_end(code_str)
        else:
            if len(self._cache) >= self._max_size:
                self._cache.popitem(last=False)
            self._cache[code_str] = [coord]

    def remove_coordinate(self, code_str: str, sheet_name: str, row: int, col: int) -> None:
        coord = (sheet_name, int(row), int(col))
        if code_str in self._cache:
            coords = self._cache[code_str]
            if coord in coords:
                coords.remove(coord)
            if not coords:
                del self._cache[code_str]

    def clear_sheet(self, sheet_name: str) -> None:
        """Remove all coordinates associated with sheet_name."""
        self.last_accessed = time.monotonic()
        to_delete: list[str] = []
        for code_str, coords in list(self._cache.items()):
            new_coords = [c for c in coords if c[0] != sheet_name]
            if new_coords:
                self._cache[code_str] = new_coords
            else:
                to_delete.append(code_str)
        for code_str in to_delete:
            del self._cache[code_str]

    def rename_sheet(self, old_sheet_name: str, new_sheet_name: str) -> None:
        """Update coordinates when a sheet is renamed."""
        self.last_accessed = time.monotonic()
        for code_str, coords in list(self._cache.items()):
            self._cache[code_str] = [(new_sheet_name if c[0] == old_sheet_name else c[0], c[1], c[2]) for c in coords]

    def clear(self) -> None:
        self._cache.clear()

    def __len__(self) -> int:
        return len(self._cache)


class FormulaLocationCache:
    """Thread-safe multi-document cache for Calc formula cell coordinates.

    Maintains isolated per-document sub-caches: doc_url -> DocumentFormulaCache.
    Supports any number of concurrently open documents (ideal for server/multi-session environments)
    while lifecycle hooks and a 5-minute idle TTL ensure automatic cleanup.
    """

    _max_formulas_per_doc: int
    _ttl_seconds: float
    _lock: threading.Lock

    def __init__(self, max_formulas_per_doc: int = MAX_FORMULAS_PER_DOC, ttl_seconds: float = DEFAULT_DOC_CACHE_TTL_SECONDS) -> None:
        self._max_formulas_per_doc = max_formulas_per_doc
        self._ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        # Key: doc_url -> Value: DocumentFormulaCache
        self._docs: dict[str, DocumentFormulaCache] = {}

    def _prune_expired_docs(self, now: float) -> None:
        """Drop document caches that have been idle longer than ttl_seconds."""
        if self._ttl_seconds <= 0:
            return
        expired = [url for url, dc in self._docs.items() if now - dc.last_accessed > self._ttl_seconds]
        for url in expired:
            self._docs.pop(url, None)

    def _get_doc_cache(self, doc_url: str, create: bool = False) -> DocumentFormulaCache | None:
        now = time.monotonic()
        self._prune_expired_docs(now)
        if doc_url in self._docs:
            return self._docs[doc_url]
        if create:
            doc_cache = DocumentFormulaCache(max_size=self._max_formulas_per_doc)
            self._docs[doc_url] = doc_cache
            return doc_cache
        return None

    def get(self, doc_url: str, code_str: str) -> list[tuple[str, int, int]]:
        """Return cached coordinate candidates for (doc_url, code_str), or empty list."""
        with self._lock:
            doc_cache = self._get_doc_cache(doc_url, create=False)
            if doc_cache is not None:
                return doc_cache.get(code_str)
            return []

    def put(self, doc_url: str, code_str: str, sheet_name: str, row: int, col: int) -> None:
        """Insert or promote (sheet_name, row, col) as MRU for (doc_url, code_str)."""
        with self._lock:
            doc_cache = self._get_doc_cache(doc_url, create=True)
            if doc_cache is not None:
                doc_cache.put(code_str, sheet_name, row, col)

    def remove_coordinate(self, doc_url: str, code_str: str, sheet_name: str, row: int, col: int) -> None:
        """Remove a stale coordinate candidate from (doc_url, code_str)."""
        with self._lock:
            doc_cache = self._get_doc_cache(doc_url, create=False)
            if doc_cache is not None:
                doc_cache.remove_coordinate(code_str, sheet_name, row, col)
                if len(doc_cache) == 0:
                    self._docs.pop(doc_url, None)

    def clear_sheet(self, doc_url: str, sheet_name: str) -> None:
        """Release cached formula locations for a specific sheet within a document."""
        with self._lock:
            doc_cache = self._docs.get(doc_url)
            if doc_cache is not None:
                doc_cache.clear_sheet(sheet_name)
                if len(doc_cache) == 0:
                    self._docs.pop(doc_url, None)

    def rename_sheet(self, doc_url: str, old_sheet_name: str, new_sheet_name: str) -> None:
        """Update cached sheet coordinates when a sheet is renamed."""
        with self._lock:
            doc_cache = self._docs.get(doc_url)
            if doc_cache is not None:
                doc_cache.rename_sheet(old_sheet_name, new_sheet_name)

    def clear_document(self, doc_url: str) -> None:
        """Release all cached formula locations for a specific document (e.g. on close)."""
        with self._lock:
            self._docs.pop(doc_url, None)

    def clear(self) -> None:
        """Clear all cached formula locations across all documents."""
        with self._lock:
            self._docs.clear()

    def document_count(self) -> int:
        """Number of open documents currently tracked in cache."""
        with self._lock:
            return len(self._docs)

    def formula_count(self, doc_url: str) -> int:
        """Number of distinct formula codes tracked for a specific document."""
        with self._lock:
            doc_cache = self._docs.get(doc_url)
            return len(doc_cache) if doc_cache is not None else 0

    def __len__(self) -> int:
        """Total number of formula codes cached across all documents."""
        with self._lock:
            return sum(len(d) for d in self._docs.values())


# Global default cache instance
FORMULA_LOCATION_CACHE = FormulaLocationCache()


def document_cache_key(doc: Any) -> str:
    """Stable cache key for a Calc document. Never empty-string URL.

    Unsaved books share ``getURL() == ""``; keying on that collides. Prefer
    ``RuntimeUID`` (same as workbook lifecycle), else the workbook session id
    which assigns a UUID for untitled docs.
    """
    from plugin.calc.python.workbook_lifecycle import _lifecycle_key

    return _lifecycle_key(doc)


def extract_code_from_py_formula(formula: str) -> str | None:
    """Extract and unescape the Python code string argument from a =PY() / =PYTHON() formula."""
    if not formula:
        return None
    match = _PY_FORMULA_CODE_REGEX.search(formula)
    if match:
        raw_code = match.group(1)
        return raw_code.replace('""', '"')
    return None


def is_matching_py_formula(formula: str, code_str: str) -> bool:
    """True if formula is a =PY() / =PYTHON() call whose code argument equals code_str."""
    if not formula or not code_str:
        return False
    upper = formula.upper()
    if "PYTHON" not in upper and "PY" not in upper:
        return False
    extracted = extract_code_from_py_formula(formula)
    if extracted is None:
        return False
    if extracted == code_str:
        return True
    ext_norm = extracted.replace("\r\n", "\n").strip()
    code_norm = code_str.replace("\r\n", "\n").strip()
    return ext_norm == code_norm


def _matching_cells_in_range(sheet: Any, cell_range: Any, code_str: str, *, doc_url: str = "", cache: FormulaLocationCache | None = None, sheet_name: str = "") -> list[tuple[Any, int, int]]:
    """Matching formula cells in *cell_range*. A filled matrix of the same code is one origin."""
    addr = cell_range.getRangeAddress()
    hits: list[tuple[Any, int, int]] = []
    for r in range(addr.StartRow, addr.EndRow + 1):
        for c in range(addr.StartColumn, addr.EndColumn + 1):
            cell = sheet.getCellByPosition(c, r)
            formula = cell.getFormula()
            if not formula:
                continue
            upper = formula.upper()
            if "PYTHON" not in upper and "PY" not in upper:
                continue
            if cache is not None and doc_url:
                extracted_code = extract_code_from_py_formula(formula)
                if extracted_code:
                    cache.put(doc_url, extracted_code, sheet_name, r, c)
            if is_matching_py_formula(formula, code_str):
                hits.append((cell, r, c))
    if len(hits) <= 1:
        return hits
    min_r = min(h[1] for h in hits)
    max_r = max(h[1] for h in hits)
    min_c = min(h[2] for h in hits)
    max_c = max(h[2] for h in hits)
    expected = (max_r - min_r + 1) * (max_c - min_c + 1)
    if len(hits) == expected:
        # Array/matrix formula: one origin at top-left.
        for cell, r, c in hits:
            if r == min_r and c == min_c:
                return [(cell, r, c)]
    return hits


def search_sheet_for_formula(sheet: Any, code_str: str, *, doc_url: str = "", cache: FormulaLocationCache | None = None) -> tuple[Any, int, int] | None:
    """Query formula cells on sheet. Returns a match only when exactly one origin matches.

    Duplicate ``=PY("same")`` cells are indistinguishable (XAddIn has no calling cell);
    first-match would spill into the wrong formula. Matrix/array formulas are one origin
    (top-left of the formula range).
    """
    matches = collect_matching_formula_origins(sheet, code_str, doc_url=doc_url, cache=cache)
    if len(matches) == 1:
        return matches[0]
    return None


def collect_matching_formula_origins(sheet: Any, code_str: str, *, doc_url: str = "", cache: FormulaLocationCache | None = None) -> list[tuple[Any, int, int]]:
    """All matching formula origins on *sheet* (one per queryContentCells range)."""
    found: list[tuple[Any, int, int]] = []
    try:
        # com.sun.star.sheet.CellFlags.FORMULA = 16
        formula_cells = sheet.queryContentCells(16)
        if formula_cells is None:
            return found
        count = formula_cells.getCount() if hasattr(formula_cells, "getCount") else 0
        sheet_name = sheet.getName() if hasattr(sheet, "getName") else "Sheet1"
        for i in range(count):
            cell_range = formula_cells.getByIndex(i)
            found.extend(_matching_cells_in_range(sheet, cell_range, code_str, doc_url=doc_url, cache=cache, sheet_name=sheet_name))
    except Exception:
        log.debug("search_sheet_for_formula failed on sheet", exc_info=True)
    return found


def _collect_formula_origins_in_doc(doc: Any, code_str: str, *, cache: FormulaLocationCache | None = None, max_matches: int = 2) -> list[tuple[Any, Any, tuple[int, int]]]:
    """Scan doc sheets for formula code matches, stopping once max_matches is reached."""
    if doc is None:
        return []
    active_cache = cache if cache is not None else FORMULA_LOCATION_CACHE
    doc_url = document_cache_key(doc)
    collected: list[tuple[Any, Any, tuple[int, int]]] = []
    seen: set[tuple[str, int, int]] = set()

    def _add(sheet: Any, cell: Any, row: int, col: int) -> None:
        try:
            name = sheet.getName() if hasattr(sheet, "getName") else ""
        except Exception:
            name = ""
        key = (name, row, col)
        if key in seen:
            return
        seen.add(key)
        collected.append((sheet, cell, (row, col)))

    try:
        sheets = doc.getSheets() if hasattr(doc, "getSheets") else None
        if sheets is not None:
            count = sheets.getCount() if hasattr(sheets, "getCount") else 0
            for i in range(count):
                sheet = sheets.getByIndex(i)
                for cell, r, c in collect_matching_formula_origins(sheet, code_str, doc_url=doc_url, cache=active_cache):
                    _add(sheet, cell, r, c)
                    if len(collected) >= max_matches:
                        return collected
    except Exception:
        log.debug("scan across sheets failed in doc", exc_info=True)
    return collected


def locate_formula_cell_in_doc(ctx: Any, doc: Any, code_str: str, *, cache: FormulaLocationCache | None = None) -> tuple[Any, Any, tuple[int, int]] | None:
    """Find unique (sheet, cell, (row, col)) for this Python formula in *doc*.

    Returns None when zero or two+ origins match: XAddIn has no calling cell, so
    spilling or caching from the first hit writes the wrong formula.
    """
    if doc is None:
        return None

    active_cache = cache if cache is not None else FORMULA_LOCATION_CACHE
    doc_url = document_cache_key(doc)
    # Uniqueness is a negative ("no second origin"). FormulaLocationCache is an
    # opportunistic MRU, not a complete index, so a cached coord still matching
    # does not prove there is no duplicate. Do not skip queryContentCells on a
    # cache hit (that was the first-match bug: spill/image/scalar wrote the wrong
    # cell). Follow-up: a live complete index (sheet modify listener tracking
    # every =PY() origin as unique vs ambiguous) could skip the walk when the
    # formula set is known unchanged. Until then every unique-origin lookup must
    # scan formula cells.
    collected = _collect_formula_origins_in_doc(doc, code_str, cache=active_cache, max_matches=2)
    if len(collected) != 1:
        return None
    sheet, cell, coord = collected[0]
    try:
        sheet_name = sheet.getName() if hasattr(sheet, "getName") else "Sheet1"
        active_cache.put(doc_url, code_str, sheet_name, coord[0], coord[1])
        for stale_sheet, stale_r, stale_c in list(active_cache.get(doc_url, code_str)):
            if (stale_sheet, stale_r, stale_c) != (sheet_name, coord[0], coord[1]):
                active_cache.remove_coordinate(doc_url, code_str, stale_sheet, stale_r, stale_c)
    except Exception:
        pass
    return (sheet, cell, coord)


def locate_formula_cell_in_open_docs(
    ctx: Any,
    preferred_doc: Any | None,
    code_str: str,
    *,
    cache: FormulaLocationCache | None = None,
) -> tuple[Any, Any, Any, tuple[int, int]] | None:
    """Find unique (doc, sheet, cell, (row, col)) for this formula across open Calc docs.

    1. Checks preferred_doc first:
       - Unique match -> returns (preferred_doc, sheet, cell, coord).
       - Ambiguous (2+ matches in preferred_doc) -> returns None (same-doc ambiguity).
       - 0 matches -> falls through to other open Calc docs.
    2. Only when preferred_doc has 0 matches (or was None):
       Walks other open Calc docs on the main thread.
       Returns (doc, sheet, cell, coord) if and only if exactly one origin
       exists across all other open docs; otherwise None.
    """
    active_cache = cache if cache is not None else FORMULA_LOCATION_CACHE

    if preferred_doc is not None:
        pref_matches = _collect_formula_origins_in_doc(preferred_doc, code_str, cache=active_cache, max_matches=2)
        if len(pref_matches) == 1:
            sheet, cell, coord = pref_matches[0]
            doc_url = document_cache_key(preferred_doc)
            try:
                sheet_name = sheet.getName() if hasattr(sheet, "getName") else "Sheet1"
                active_cache.put(doc_url, code_str, sheet_name, coord[0], coord[1])
                for stale_sheet, stale_r, stale_c in list(active_cache.get(doc_url, code_str)):
                    if (stale_sheet, stale_r, stale_c) != (sheet_name, coord[0], coord[1]):
                        active_cache.remove_coordinate(doc_url, code_str, stale_sheet, stale_r, stale_c)
            except Exception:
                pass
            return (preferred_doc, sheet, cell, coord)
        elif len(pref_matches) > 1:
            # Ambiguity within preferred document stays an error; do not consult other docs.
            return None

    from plugin.framework.thread_guard import on_main_thread

    if not on_main_thread():
        return None

    try:
        from plugin.framework.uno_context import get_desktop, uno_same
        from plugin.scripting.document_scripts import _enumerate_calc_documents

        desktop = get_desktop(ctx)
        calcs = _enumerate_calc_documents(desktop)
    except Exception:
        log.debug("locate_formula_cell_in_open_docs desktop enumeration failed", exc_info=True)
        return None

    total_matches: list[tuple[Any, Any, Any, tuple[int, int]]] = []
    for other_doc in calcs:
        if preferred_doc is not None:
            if other_doc is preferred_doc:
                continue
            try:
                if uno_same(other_doc, preferred_doc):
                    continue
            except Exception:
                pass

        matches = _collect_formula_origins_in_doc(other_doc, code_str, cache=active_cache, max_matches=2)
        if len(matches) > 1:
            # 2+ matches in this document means overall ambiguity
            return None
        elif len(matches) == 1:
            sheet, cell, coord = matches[0]
            total_matches.append((other_doc, sheet, cell, coord))
            if len(total_matches) > 1:
                # Multiple documents have matching formula cells
                return None

    if len(total_matches) == 1:
        doc, sheet, cell, coord = total_matches[0]
        # What was wrong: if target_doc was the wrong document (e.g. an untitled doc opened first),
        # image egress failed to locate the formula cell and aborted image insertion.
        # How: fallback to other open Calc documents when preferred has 0 hits.
        # Why: locate_formula_cell_in_open_docs recovers the correct calling document.
        log.debug(
            "locate_formula_cell_in_open_docs: fallback picked different doc %s for formula code",
            document_cache_key(doc),
        )
        doc_url = document_cache_key(doc)
        try:
            sheet_name = sheet.getName() if hasattr(sheet, "getName") else "Sheet1"
            active_cache.put(doc_url, code_str, sheet_name, coord[0], coord[1])
            for stale_sheet, stale_r, stale_c in list(active_cache.get(doc_url, code_str)):
                if (stale_sheet, stale_r, stale_c) != (sheet_name, coord[0], coord[1]):
                    active_cache.remove_coordinate(doc_url, code_str, stale_sheet, stale_r, stale_c)
        except Exception:
            pass
        return (doc, sheet, cell, coord)

    return None


def locate_formula_cell(ctx: Any, sheet: Any, code_str: str, *, cache: FormulaLocationCache | None = None, doc: Any | None = None) -> tuple[int, int] | None:
    """Find (row, col) containing the Python formula on sheet."""
    try:
        from plugin.framework.thread_guard import on_main_thread

        if not on_main_thread() or not (hasattr(ctx, "ServiceManager") or hasattr(ctx, "getServiceManager")):
            return None
        target_doc = doc
        if target_doc is None:
            try:
                from plugin.calc.python.sheet_modify import _owning_calc_doc

                target_doc = _owning_calc_doc(sheet)
            except Exception:
                pass
        if target_doc is None:
            from plugin.calc.python.function import _get_calc_doc

            target_doc = _get_calc_doc(ctx)
        if target_doc is not None:
            located = locate_formula_cell_in_doc(ctx, target_doc, code_str, cache=cache)
            if located is not None:
                found_sheet, _, (r, c) = located
                if found_sheet == sheet or (hasattr(found_sheet, "getName") and hasattr(sheet, "getName") and found_sheet.getName() == sheet.getName()):
                    return (r, c)
    except Exception:
        pass

    # Fallback to direct sheet search
    res = search_sheet_for_formula(sheet, code_str)
    if res is not None:
        _, r, c = res
        return (r, c)

    return None
