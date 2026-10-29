# SPDX-License-Identifier: GPL-3.0-or-later
"""Resolve Excel formula deps to A1 ranges for DAG ``=PY(..., ranges)``.

This is **not** a Python rewrite. Excel's trailing PY args may be ``A1:B10``,
``Table1[#All]``, ``_xlfn.ANCHORARRAY(A6)``, or whole-row/column refs. We turn
those into concrete A1 strings so they can sit on the ``=PY`` formula (Calc
precedents). Table/spill extents are **snapshots** at convert time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from plugin.calc.excel_py_convert.models import ExcelWorkbookModel

from plugin.calc.address_utils import MAX_COL, MAX_ROW
from plugin.calc.excel_py_convert.parse_excel_ooxml import letters_to_col_index
from plugin.framework.deal_shim import DEAL_MAX_SOURCE, str_bounded, deal

_ANCHOR_RE = re.compile(r"^(?:_xlfn\.)?ANCHORARRAY\((.+)\)$", re.IGNORECASE)
_TABLE_ALL_RE = re.compile(r"^([A-Za-z_][\w.]*)\[#All\]$", re.IGNORECASE)
_SPILL_RE = re.compile(r"^(.+)#$")
# Sheet-qualified or bare ranges, including whole columns/rows: A:A, B:D, 1:10, Sheet!A:A
_RANGE_RE = re.compile(
    r"^("
    r"(?:'(?:[^']|'')+'|[A-Za-z_][\w.]*)!"  # optional sheet!
    r")?"
    r"("
    r"\$?[A-Za-z]+\$?\d+:\$?[A-Za-z]+\$?\d+"  # A1:B2
    r"|\$?[A-Za-z]+\$?\d+"  # A1
    r"|\$?[A-Za-z]+:\$?[A-Za-z]+"  # A:C whole columns
    r"|\$?\d+:\$?\d+"  # 1:10 whole rows
    r")$",
    re.IGNORECASE,
)


@dataclass
class ResolvedDep:
    """One dependency after resolution."""

    original: str
    a1: str
    kind: str  # range | table_snapshot | anchor_snapshot | unresolved
    note: str = ""


@deal.pre(lambda model, anchor, sheet_hint="": str_bounded(anchor, DEAL_MAX_SOURCE) and str_bounded(sheet_hint, DEAL_MAX_SOURCE))
def _lookup_anchor(model: ExcelWorkbookModel, anchor: str, sheet_hint: str = "") -> str | None:
    """Find an array/spill snapshot for *anchor* (bare or Sheet!A1)."""
    cleaned = anchor.strip()
    snaps = model.anchor_snapshots
    if not snaps:
        return None

    lower = {k.lower(): v for k, v in snaps.items()}

    def lookup(key: str) -> str | None:
        val = snaps.get(key)
        if val is not None:
            return val
        return lower.get(key.lower())

    if "!" in cleaned:
        sheet_part, _sep, addr = cleaned.rpartition("!")
        return lookup(f"{sheet_part}!{addr.replace('$', '').strip()}")

    addr = cleaned.replace("$", "").strip()
    if sheet_hint:
        raw_hint = f"{sheet_hint}!{addr}"
        val = lookup(raw_hint)
        if val is not None:
            return val
        quoted_hint = "'" + sheet_hint.replace("'", "''") + f"'!{addr}"
        val = lookup(quoted_hint)
        if val is not None:
            return val

    if not sheet_hint or len(getattr(model, "sheets", ()) or ()) <= 1:
        return lookup(addr)
    return None


def _a1_in_bounds(span: str) -> bool:
    for part in span.split(":"):
        part = part.replace("$", "")
        letters = part.rstrip("0123456789")
        digits = part[len(letters):]
        if letters and letters_to_col_index(letters) > MAX_COL + 1:
            return False
        if digits and not 1 <= int(digits) <= MAX_ROW + 1:
            return False
    return True


def _unresolved(raw: str, note: str = "") -> ResolvedDep:
    return ResolvedDep(original=raw, a1="", kind="unresolved", note=note)


def _anchor_snapshot(
    raw: str,
    anchor: str,
    model: ExcelWorkbookModel,
    sheet_hint: str,
    *,
    label: str,
) -> ResolvedDep:
    snap = _lookup_anchor(model, anchor, sheet_hint=sheet_hint)
    if snap:
        return ResolvedDep(original=raw, a1=snap, kind="anchor_snapshot", note=f"{label} → {snap}")
    return _unresolved(raw, note=f"{label} snapshot unavailable")


@deal.pre(lambda dep, model, sheet_hint="": str_bounded(dep, DEAL_MAX_SOURCE) and str_bounded(sheet_hint, DEAL_MAX_SOURCE))
def resolve_dep(dep: str, model: ExcelWorkbookModel, *, sheet_hint: str = "") -> ResolvedDep:
    """Map one PY trailing arg to an A1 address for DAG ``data`` args."""
    # Table/ANCHORARRAY/A1 regex hang under deep check (same class as parse_address).
    # crosshair: off
    raw = (dep or "").strip()
    if not raw:
        return _unresolved(raw, note="empty dep")

    m_anchor = _ANCHOR_RE.match(raw)
    if m_anchor:
        anchor = m_anchor.group(1).strip()
        return _anchor_snapshot(raw, anchor, model, sheet_hint, label=f"ANCHORARRAY({anchor})")

    # $# / $$# match _SPILL_RE, but the old shape gate treated a
    # $-only prefix as unrecognized. Keep that note.
    m_spill = _SPILL_RE.match(raw)
    if m_spill and len(raw.replace("$", "")) >= 2:
        anchor = m_spill.group(1)
        return _anchor_snapshot(raw, anchor, model, sheet_hint, label=raw)

    m_table = _TABLE_ALL_RE.match(raw)
    if m_table:
        name = m_table.group(1)
        ref = model.tables.get(name)
        if not ref:
            folded = name.lower()
            ref = next((value for table_name, value in model.tables.items() if table_name.lower() == folded), None)
        if ref:
            return ResolvedDep(original=raw, a1=ref.replace("$", ""), kind="table_snapshot", note=f"{name}[#All] → {ref}")
        return _unresolved(raw, note=f"unknown table {name!r}")

    sheet_part, sep, addr = raw.rpartition("!")
    cleaned = f"{sheet_part}{sep}{addr.replace('$', '')}" if sep else raw.replace("$", "")
    m = _RANGE_RE.match(cleaned)
    if m:
        if _a1_in_bounds(m.group(2)):
            return ResolvedDep(original=raw, a1=cleaned, kind="range")
        return _unresolved(raw, note=f"out-of-bounds A1 ref {raw!r}")

    return _unresolved(raw, note=f"unrecognized dep {raw!r}")
