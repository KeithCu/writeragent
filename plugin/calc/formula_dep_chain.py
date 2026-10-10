# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Formula dependency chain for Calc error diagnosis (``.uno:FormulaDepChain`` + fallback)."""

# crosshair: off
from __future__ import annotations

import logging
from typing import Any

from plugin.calc.address_utils import format_address
from plugin.calc.calc_utils import resolve_sheet_and_cell as _resolve_sheet_and_cell
from plugin.calc.inspector import CellInspector
from plugin.framework.errors import safe_json_loads

log = logging.getLogger("writeragent.calc")

_FORMULA_DEP_CHAIN_CMD = ".uno:FormulaDepChain"
# A full-column precedent such as A:Z is about 27 million cells. Walking
# every one with getCellByPosition freezes the UI. Stop after this many snapshots.
_MAX_PRECEDENT_CELLS = 10_000


def _sheet_name(sheet: Any) -> str:
    getter = getattr(sheet, "getName", None)
    if not callable(getter):
        return ""
    try:
        name = getter()
    except Exception:
        return ""
    return name if isinstance(name, str) else ""


def _address_on_sheet(sheet: Any, col: int, row: int) -> str:
    """Cell address, qualified with the sheet when its name is known.

    Cross-sheet precedents used to be emitted as a bare ``A1`` read from the
    formula's sheet. The sheet name makes ``Sheet2.A1`` distinct from ``A1``.
    """
    local = format_address(col, row)
    name = _sheet_name(sheet)
    if not name:
        return local
    if any(ch in name for ch in " .!'"):
        return "'%s'!%s" % (name.replace("'", "''"), local)
    return "%s.%s" % (name, local)


def _sheet_for_precedent(doc: Any, fallback: Any, sheet_index: Any) -> Any:
    """Spreadsheet named by ``CellRangeAddress.Sheet``.

    ``getSheets().getByIndex`` is the sheet that index names. Ignoring
    ``addr.Sheet`` makes ``=Data.A1`` report the formula sheet's A1, even
    though ``queryPrecedents`` carries the sheet index. A missing index
    (callers that only have the formula sheet) keeps *fallback*.
    """
    if doc is None or sheet_index is None:
        return fallback
    try:
        return doc.getSheets().getByIndex(int(sheet_index))
    except Exception:
        log.debug("Could not resolve precedent sheet %s", sheet_index, exc_info=True)
        return fallback


def _cell_snapshot(sheet: Any, col: int, row: int) -> dict[str, Any]:
    cell = sheet.getCellByPosition(col, row)
    from com.sun.star.table import CellContentType

    ctype = cell.getType()
    type_name = CellInspector._cell_type_name(ctype)
    addr = _address_on_sheet(sheet, col, row)
    snapshot: dict[str, Any] = {"address": addr, "type": type_name}

    try:
        err = cell.getError()
        if err:
            snapshot["error_code"] = err
    except Exception:
        pass
    try:
        if ctype == CellContentType.FORMULA:
            snapshot["formula"] = cell.getFormula()
            snapshot["value"] = cell.getValue()
        elif ctype == CellContentType.VALUE:
            snapshot["value"] = cell.getValue()
        else:
            snapshot["value"] = cell.getString()
    except Exception:
        pass
    return snapshot


def _precedents_via_formula_query(sheet: Any, col: int, row: int, doc: Any = None) -> dict[str, Any]:
    """Build a lightweight precedent list when ``FormulaDepChain`` UNO is unavailable.

    Precedent ranges are expanded to cell snapshots up to
    ``_MAX_PRECEDENT_CELLS``. ``truncated`` is true when the cap stopped
    the walk (for example a whole-column ``SUM``). ``doc`` resolves
    ``CellRangeAddress.Sheet`` so a cross-sheet precedent is read from
    that sheet. Without ``doc``, ranges are read from *sheet*.
    """
    try:
        from com.sun.star.sheet import XFormulaQuery
    except ImportError:
        return {"source": "formula_query_unavailable", "precedents": []}

    cell_range = sheet.getCellRangeByPosition(col, row, col, row)
    fq = cell_range.queryInterface(XFormulaQuery)
    if fq is None:
        return {"source": "formula_query_unavailable", "precedents": []}

    precedents: list[dict[str, Any]] = []
    # Set once the cap is hit so callers can tell a partial list from a
    # complete one. Whole-column refs used to expand with no limit.
    truncated = False
    try:
        ranges = fq.queryPrecedents(False)
        if ranges is None:
            return {"source": "formula_query", "precedents": precedents, "truncated": False}
        for addr in ranges.getRangeAddresses():
            # Sheet 0 is a real sheet. Only a missing index means "this sheet".
            prec_sheet = _sheet_for_precedent(doc, sheet, getattr(addr, "Sheet", None))
            for r in range(addr.StartRow, addr.EndRow + 1):
                for c in range(addr.StartColumn, addr.EndColumn + 1):
                    if len(precedents) >= _MAX_PRECEDENT_CELLS:
                        truncated = True
                        break
                    precedents.append(_cell_snapshot(prec_sheet, c, r))
                if truncated:
                    break
            if truncated:
                break
    except Exception:
        log.debug("queryPrecedents failed", exc_info=True)
    return {"source": "formula_query", "precedents": precedents, "truncated": truncated}


def fetch_formula_dep_chain(doc: Any, ctx: Any, address: str) -> dict[str, Any] | None:
    """Return dependency JSON for *address* using LO command values or ``XFormulaQuery``."""
    if doc is None:
        return None
    resolved = _resolve_sheet_and_cell(doc, address)
    if resolved is None:
        return None
    sheet, col, row = resolved

    if ctx is not None:
        # Navigation selects the cell for the UNO command. A disposed
        # document, headless run, or missing controller can throw here.
        # That used to abort before getCommandValues and the formula-query
        # fallback, so the dep chain was lost even though it does not need
        # the view.
        try:
            from plugin.calc.navigation import navigate_to_cell

            navigate_to_cell(doc, ctx, address)
        except Exception:
            log.debug("navigate_to_cell failed for %r; continuing dep chain", address, exc_info=True)

    chain: dict[str, Any] | None = None
    if hasattr(doc, "getCommandValues"):
        try:
            raw = doc.getCommandValues(_FORMULA_DEP_CHAIN_CMD)
            if raw:
                parsed = safe_json_loads(raw, default=None) if isinstance(raw, str) else raw
                if isinstance(parsed, dict):
                    chain = parsed.get("commandValues") if "commandValues" in parsed else parsed
                    if chain:
                        chain = dict(chain)
                        chain["source"] = "uno_formula_dep_chain"
        except Exception:
            log.debug("getCommandValues(%s) failed", _FORMULA_DEP_CHAIN_CMD, exc_info=True)

    if not chain:
        chain = _precedents_via_formula_query(sheet, col, row, doc)

    if chain is not None:
        chain.setdefault("cell", address.upper())
    return chain
