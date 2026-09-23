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


def _cell_snapshot(sheet: Any, col: int, row: int) -> dict[str, Any]:
    cell = sheet.getCellByPosition(col, row)
    from com.sun.star.table import CellContentType

    ctype = cell.getType()
    type_name = CellInspector._cell_type_name(ctype)
    addr = format_address(col, row)
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


def _precedents_via_formula_query(sheet: Any, col: int, row: int) -> dict[str, Any]:
    """Build a lightweight precedent list when ``FormulaDepChain`` UNO is unavailable."""
    try:
        from com.sun.star.sheet import XFormulaQuery
    except ImportError:
        return {"source": "formula_query_unavailable", "precedents": []}

    cell_range = sheet.getCellRangeByPosition(col, row, col, row)
    fq = cell_range.queryInterface(XFormulaQuery)
    if fq is None:
        return {"source": "formula_query_unavailable", "precedents": []}

    precedents: list[dict[str, Any]] = []
    try:
        ranges = fq.queryPrecedents(False)
        if ranges is None:
            return {"source": "formula_query", "precedents": precedents}
        for addr in ranges.getRangeAddresses():
            for r in range(addr.StartRow, addr.EndRow + 1):
                for c in range(addr.StartColumn, addr.EndColumn + 1):
                    precedents.append(_cell_snapshot(sheet, c, r))
    except Exception:
        log.debug("queryPrecedents failed", exc_info=True)
    return {"source": "formula_query", "precedents": precedents}


def fetch_formula_dep_chain(doc: Any, ctx: Any, address: str) -> dict[str, Any] | None:
    """Return dependency JSON for *address* using LO command values or ``XFormulaQuery``."""
    if doc is None:
        return None
    resolved = _resolve_sheet_and_cell(doc, address)
    if resolved is None:
        return None
    sheet, col, row = resolved

    if ctx is not None:
        from plugin.calc.navigation import navigate_to_cell

        navigate_to_cell(doc, ctx, address)

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
        chain = _precedents_via_formula_query(sheet, col, row)

    if chain is not None:
        chain.setdefault("cell", address.upper())
    return chain
