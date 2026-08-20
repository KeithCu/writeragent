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

# Regex to extract code string literal from =PY("...") or =PYTHON("...", ...) formulas.
# Handles Calc-escaped double-quotes ("") inside string literals and supports optional add-in namespaces.
_PY_FORMULA_CODE_REGEX = re.compile(
    r'(?:ORG\.EXTENSION\.[A-Z0-9_.]+\.)?(?:PY|PYTHON)\s*\(\s*"((?:[^"]|"")*)"',
    re.IGNORECASE,
)


class DocumentFormulaCache:
    """Internal LRU/MRU formula coordinate cache for a single Calc document."""

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

    def __init__(
        self,
        max_formulas_per_doc: int = MAX_FORMULAS_PER_DOC,
        ttl_seconds: float = DEFAULT_DOC_CACHE_TTL_SECONDS,
    ) -> None:
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


def search_sheet_for_formula(
    sheet: Any,
    code_str: str,
    *,
    doc_url: str = "",
    cache: FormulaLocationCache | None = None,
) -> tuple[Any, int, int] | None:
    """Query formula cells on sheet, opportunistically warm cache for all PY cells, and return match."""
    try:
        # com.sun.star.sheet.CellFlags.FORMULA = 16
        formula_cells = sheet.queryContentCells(16)
        if formula_cells is not None:
            count = formula_cells.getCount() if hasattr(formula_cells, "getCount") else 0
            sheet_name = sheet.getName() if hasattr(sheet, "getName") else "Sheet1"
            matched = None
            for i in range(count):
                cell_range = formula_cells.getByIndex(i)
                addr = cell_range.getRangeAddress()
                for r in range(addr.StartRow, addr.EndRow + 1):
                    for c in range(addr.StartColumn, addr.EndColumn + 1):
                        cell = sheet.getCellByPosition(c, r)
                        formula = cell.getFormula()
                        if not formula:
                            continue
                        upper = formula.upper()
                        if "PYTHON" in upper or "PY" in upper:
                            # Opportunistically cache every discovered =PY() / =PYTHON() cell on the sheet
                            if cache is not None and doc_url:
                                extracted_code = extract_code_from_py_formula(formula)
                                if extracted_code:
                                    cache.put(doc_url, extracted_code, sheet_name, r, c)
                            if matched is None and is_matching_py_formula(formula, code_str):
                                matched = (cell, r, c)
            return matched
    except Exception:
        log.debug("search_sheet_for_formula failed on sheet", exc_info=True)
    return None


def locate_formula_cell_in_doc(
    ctx: Any,
    doc: Any,
    code_str: str,
    *,
    cache: FormulaLocationCache | None = None,
) -> tuple[Any, Any, tuple[int, int]] | None:
    """Find (sheet, cell, (row, col)) containing the Python formula across all sheets in doc.

    Uses fast-path selection check, validates cached coordinates in O(1), and falls back
    to queryContentCells(16) across workbook sheets on cache miss.
    """
    if doc is None:
        return None

    active_cache = cache if cache is not None else FORMULA_LOCATION_CACHE
    doc_url = document_cache_key(doc)

    # 1. Fast-path on active sheet / selection
    active_sheet = None
    try:
        ctrl = doc.getCurrentController() if hasattr(doc, "getCurrentController") else None
        if ctrl is not None and hasattr(ctrl, "getActiveSheet"):
            active_sheet = ctrl.getActiveSheet()
        if active_sheet is not None and ctrl is not None and hasattr(ctrl, "getSelection"):
            selection = ctrl.getSelection()
            if selection is not None and hasattr(selection, "getRangeAddress"):
                addr = selection.getRangeAddress()
                candidates = [
                    (addr.StartRow, addr.StartColumn),
                    (addr.StartRow - 1, addr.StartColumn),
                    (addr.StartRow, addr.StartColumn - 1),
                ]
                for r, c in candidates:
                    if r >= 0 and c >= 0:
                        cell = active_sheet.getCellByPosition(c, r)
                        formula = cell.getFormula()
                        if is_matching_py_formula(formula, code_str):
                            sheet_name = active_sheet.getName() if hasattr(active_sheet, "getName") else "Sheet1"
                            active_cache.put(doc_url, code_str, sheet_name, r, c)
                            return (active_sheet, cell, (r, c))
    except Exception:
        log.debug("locate_formula_cell_in_doc: selection fast path failed", exc_info=True)

    # 2. Check cached coordinates for (doc_url, code_str)
    cached_coords = active_cache.get(doc_url, code_str)
    if cached_coords:
        sheets = doc.getSheets() if hasattr(doc, "getSheets") else None
        if sheets is not None:
            for sheet_name, r, c in cached_coords:
                try:
                    if hasattr(sheets, "hasByName") and not sheets.hasByName(sheet_name):
                        active_cache.remove_coordinate(doc_url, code_str, sheet_name, r, c)
                        continue
                    sheet = sheets.getByName(sheet_name)
                    cell = sheet.getCellByPosition(c, r)
                    formula = cell.getFormula()
                    if is_matching_py_formula(formula, code_str):
                        active_cache.put(doc_url, code_str, sheet_name, r, c)
                        return (sheet, cell, (r, c))
                    # Formula moved or modified: prune stale candidate
                    active_cache.remove_coordinate(doc_url, code_str, sheet_name, r, c)
                except Exception:
                    active_cache.remove_coordinate(doc_url, code_str, sheet_name, r, c)

    # 3. Search active sheet formula ranges (opportunistically warms cache for all PY cells on sheet)
    if active_sheet is not None:
        res = search_sheet_for_formula(active_sheet, code_str, doc_url=doc_url, cache=active_cache)
        if res is not None:
            cell, r, c = res
            return (active_sheet, cell, (r, c))

    # 4. Fallback: Search all other sheets in the workbook
    try:
        sheets = doc.getSheets() if hasattr(doc, "getSheets") else None
        if sheets is not None:
            count = sheets.getCount() if hasattr(sheets, "getCount") else 0
            for i in range(count):
                sheet = sheets.getByIndex(i)
                try:
                    if (
                        active_sheet is not None
                        and hasattr(sheet, "getName")
                        and hasattr(active_sheet, "getName")
                        and sheet.getName() == active_sheet.getName()
                    ):
                        continue
                except Exception:
                    pass
                res = search_sheet_for_formula(sheet, code_str, doc_url=doc_url, cache=active_cache)
                if res is not None:
                    cell, r, c = res
                    return (sheet, cell, (r, c))
    except Exception:
        log.debug("locate_formula_cell_in_doc failed across sheets", exc_info=True)

    return None


def locate_formula_cell(
    ctx: Any,
    sheet: Any,
    code_str: str,
    *,
    cache: FormulaLocationCache | None = None,
) -> tuple[int, int] | None:
    """Find (row, col) containing the Python formula on sheet."""
    try:
        if not (hasattr(ctx, "ServiceManager") or hasattr(ctx, "getServiceManager")):
            return None
        from plugin.calc.python.function import _get_calc_doc

        doc = _get_calc_doc(ctx)
        if doc is not None:
            located = locate_formula_cell_in_doc(ctx, doc, code_str, cache=cache)
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

