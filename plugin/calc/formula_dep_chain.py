# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Formula dependency chain for Calc error diagnosis (``.uno:FormulaDepChain`` + fallback)."""

# crosshair: off
from __future__ import annotations

from typing import Any

from plugin.calc.address_utils import format_address, qualify_a1, split_sheet_prefix
from plugin.calc.calc_utils import cell_value_and_type, query_interface
from plugin.calc.calc_utils import resolve_sheet_and_cell as _resolve_sheet_and_cell
from plugin.framework.errors import best_effort, safe_json_loads

_FORMULA_DEP_CHAIN_CMD = ".uno:FormulaDepChain"
# A full-column precedent such as A:Z is about 27 million cells. Walking
# every one with getCellByPosition freezes the UI, and each snapshot is a
# verbose dict. 80 matches the chat range budget in cells.py; a whole-column
# SUM used to return 10,000 dicts. Stop after this many snapshots.
_MAX_PRECEDENT_CELLS = 80


def _sheet_name(sheet: Any) -> str:
    getter = getattr(sheet, "getName", None)
    if not callable(getter):
        return ""
    name = getter()
    return name if isinstance(name, str) else ""


def _sheet_for_precedent(doc: Any, fallback: Any, sheet_index: Any) -> Any:
    """Spreadsheet named by ``CellRangeAddress.Sheet``.

    ``getSheets().getByIndex`` is the sheet that index names. Ignoring
    ``addr.Sheet`` makes ``=Data.A1`` report the formula sheet's A1, even
    though ``queryPrecedents`` carries the sheet index. A missing index
    (callers that only have the formula sheet) keeps *fallback*.
    """
    if doc is None or sheet_index is None:
        return fallback
    return doc.getSheets().getByIndex(int(sheet_index))


def _cell_snapshot(sheet: Any, col: int, row: int, sheet_name: str | None = None) -> dict[str, Any]:
    cell = sheet.getCellByPosition(col, row)
    val, type_name = cell_value_and_type(cell, empty_as_none=False)
    if sheet_name is None:
        sheet_name = _sheet_name(sheet)
    addr = qualify_a1(sheet_name, format_address(col, row)) if sheet_name else format_address(col, row)
    snapshot: dict[str, Any] = {"address": addr, "type": type_name, "value": val}

    err = cell.getError()
    if err:
        snapshot["error_code"] = err
    if type_name == "formula":
        snapshot["formula"] = cell.getFormula()
    return snapshot


def _precedents_via_formula_query(sheet: Any, col: int, row: int, doc: Any = None) -> dict[str, Any]:
    """Build a lightweight precedent list when ``FormulaDepChain`` UNO is unavailable.

    Precedent ranges are expanded to cell snapshots up to
    ``_MAX_PRECEDENT_CELLS``. ``truncated`` is true when the cap stopped
    the walk (for example a whole-column ``SUM``). ``doc`` resolves
    ``CellRangeAddress.Sheet`` so a cross-sheet precedent is read from
    that sheet. Without ``doc``, ranges are read from *sheet*.
    """
    cell_range = sheet.getCellRangeByPosition(col, row, col, row)
    # pyuno needs a UNO type here; the imported IDL class raises "Couldn't convert".
    fq = query_interface(cell_range, "com.sun.star.sheet.XFormulaQuery")
    if fq is None:
        return {"source": "formula_query_unavailable", "precedents": []}

    precedents: list[dict[str, Any]] = []
    # Set once the cap is hit so callers can tell a partial list from a
    # complete one. Whole-column refs used to expand with no limit.
    truncated = False
    ranges = best_effort(lambda: fq.queryPrecedents(False), "queryPrecedents failed")
    if ranges is None:
        return {"source": "formula_query", "precedents": precedents, "truncated": False, "total_cells": 0}
    addresses = ranges.getRangeAddresses()
    total_cells = sum((addr.EndRow - addr.StartRow + 1) * (addr.EndColumn - addr.StartColumn + 1) for addr in addresses)
    for addr in addresses:
        # Sheet 0 is a real sheet. Only a missing index means "this sheet".
        prec_sheet = _sheet_for_precedent(doc, sheet, getattr(addr, "Sheet", None))
        prec_name = _sheet_name(prec_sheet)
        for r in range(addr.StartRow, addr.EndRow + 1):
            for c in range(addr.StartColumn, addr.EndColumn + 1):
                if len(precedents) >= _MAX_PRECEDENT_CELLS:
                    truncated = True
                    break
                precedents.append(_cell_snapshot(prec_sheet, c, r, sheet_name=prec_name))
            if truncated:
                break
        if truncated:
            break
    return {"source": "formula_query", "precedents": precedents, "truncated": truncated, "total_cells": total_cells}


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
        from plugin.calc.navigation import navigate_to_cell

        best_effort(lambda: navigate_to_cell(doc, ctx, address), f"navigate_to_cell failed for {address!r}; continuing dep chain")

    chain: dict[str, Any] | None = None
    if hasattr(doc, "getCommandValues"):
        raw = best_effort(lambda: doc.getCommandValues(_FORMULA_DEP_CHAIN_CMD), f"getCommandValues({_FORMULA_DEP_CHAIN_CMD}) failed")
        if raw:
            parsed = safe_json_loads(raw, default=None, strict=True) if isinstance(raw, str) else raw
            if isinstance(parsed, dict):
                chain = parsed.get("commandValues") if "commandValues" in parsed else parsed
                if chain:
                    chain = dict(chain)
                    chain["source"] = "uno_formula_dep_chain"

    if not chain:
        chain = _precedents_via_formula_query(sheet, col, row, doc)

    if chain is not None:
        local = format_address(col, row)
        sheet_name = _sheet_name(sheet)
        cell = qualify_a1(sheet_name, local) if split_sheet_prefix(address)[0] is not None and sheet_name else local
        chain.setdefault("cell", cell)
    return chain
