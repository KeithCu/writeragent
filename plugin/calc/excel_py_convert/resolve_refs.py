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

from plugin.framework.deal_shim import DEAL_MAX_SOURCE, str_bounded, deal

_ANCHOR_RE = re.compile(r"^(?:_xlfn\.)?ANCHORARRAY\((.+)\)$", re.IGNORECASE)
_TABLE_ALL_RE = re.compile(r"^([A-Za-z_][\w.]*)\[#All\]$", re.IGNORECASE)
_SPILL_RE = re.compile(r"^(.+)#$")
# Sheet-qualified or bare ranges, including whole columns/rows: A:A, B:D, 1:10, Sheet!A:A
_RANGE_RE = re.compile(
    r"^("
    r"(?:'[^']+'|[A-Za-z_][\w.]*)!"  # optional sheet!
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


def _lookup_anchor(model: ExcelWorkbookModel, anchor: str, sheet_hint: str = "") -> str | None:
    """Find an array/spill snapshot for *anchor* (bare or Sheet!A1)."""
    cleaned = anchor.replace("$", "").strip()
    snaps = model.anchor_snapshots
    for key in (
        cleaned,
        cleaned.upper(),
        f"{sheet_hint}!{cleaned}" if sheet_hint and "!" not in cleaned else "",
        f"'{sheet_hint}'!{cleaned}" if sheet_hint and "!" not in cleaned else "",
    ):
        if key and key in snaps:
            return snaps[key]
    # Case-insensitive scan
    lower = {k.lower(): v for k, v in snaps.items()}
    if cleaned.lower() in lower:
        return lower[cleaned.lower()]
    if sheet_hint:
        for variant in (f"{sheet_hint}!{cleaned}", f"'{sheet_hint}'!{cleaned}"):
            if variant.lower() in lower:
                return lower[variant.lower()]
    return None


def _has_anchorarray_shape(raw: str) -> bool:
    """True when *raw* can match ``_ANCHOR_RE`` (``ANCHORARRAY(...)`` / ``_xlfn.``).

    CrossHair's relib re-parses ``Pattern.pattern`` on symbolic ``re.match`` and
    ``PatternError``s on character-class fragments (check-all 33940151004:
    ``deps=['_']`` → unterminated character set). ``_`` is a prefix of
    ``_xlfn.`` but not a real anchor — skip the regex unless the call surface
    is present. Same class as ``formula_edit`` PY|PYTHON ``startswith``.
    """
    if not raw.endswith(")"):
        return False
    return "ANCHORARRAY(" in raw.upper()


def _has_table_all_shape(raw: str) -> bool:
    """True when *raw* can match ``_TABLE_ALL_RE`` (``Name[#All]``).

    The escaped ``[#All]`` suffix in ``_TABLE_ALL_RE`` is the fragment CrossHair
    relib treats as an open ``[`` class (check-all 33940151004). Junk like
    ``_`` / ``[`` must not reach ``re.match``.
    """
    return raw.upper().endswith("[#ALL]")


def _has_spill_shape(raw: str) -> bool:
    """True when *raw* can match ``_SPILL_RE`` (``A6#``, not ``[#All]``)."""
    spill = raw.replace("$", "")
    return len(spill) >= 2 and spill.endswith("#") and not spill.upper().endswith("[#ALL]")


def _has_range_shape(raw: str) -> bool:
    """True when *raw* can match ``_RANGE_RE`` (A1 / ``A:C`` / ``1:10`` / ``Sheet!…``).

    Lone ``_``, ``[``, ``:``, ``!`` are not ranges; do not feed them to a regex
    that contains ``[^']`` / ``[A-Za-z_]`` (relib character-set PatternError).
    Keep this a conservative *superset* of CPython ``_RANGE_RE`` matches so
    valid product deps still hit the regex.
    """
    if "!" in raw:
        sheet, _sep, rest = raw.partition("!")
        return bool(sheet) and bool(rest)
    if ":" in raw:
        left, _sep, right = raw.partition(":")
        return bool(left) and bool(right)
    stripped = raw.replace("$", "")
    i = 0
    n = len(stripped)
    while i < n and stripped[i].isalpha():
        i += 1
    if i == 0 or i == n:
        return False
    return stripped[i:].isdigit()


@deal.pre(lambda dep, model, sheet_hint="": str_bounded(dep, DEAL_MAX_SOURCE) and str_bounded(sheet_hint, DEAL_MAX_SOURCE))
@deal.post(lambda result: isinstance(result, ResolvedDep))
def resolve_dep(dep: str, model: ExcelWorkbookModel, *, sheet_hint: str = "") -> ResolvedDep:
    """Map one PY trailing arg to an A1 address for DAG ``data`` args."""
    # Table/ANCHORARRAY/A1 regex hang under deep check (same class as parse_address).
    # crosshair: off
    raw = (dep or "").strip()
    if not raw:
        return ResolvedDep(original=raw, a1="", kind="unresolved", note="empty dep")

    # Gate each regex on a cheap surface shape so CrossHair / junk deps never
    # re.match a character-class pattern (check-all 33940151004 PatternError).
    if _has_anchorarray_shape(raw):
        m_anchor = _ANCHOR_RE.match(raw)
        if m_anchor:
            anchor = m_anchor.group(1).strip().replace("$", "")
            snap = _lookup_anchor(model, anchor, sheet_hint=sheet_hint)
            if snap:
                return ResolvedDep(original=raw, a1=snap, kind="anchor_snapshot", note=f"ANCHORARRAY({anchor}) → {snap}")
            # Fail closed: do not silently shrink ANCHORARRAY to a single cell.
            return ResolvedDep(
                original=raw,
                a1="",
                kind="unresolved",
                note=f"ANCHORARRAY({anchor}) snapshot unavailable",
            )

    if _has_spill_shape(raw):
        spill_src = raw.replace("$", "")
        m_spill = _SPILL_RE.match(spill_src)
        if m_spill:
            anchor = m_spill.group(1)
            snap = _lookup_anchor(model, anchor, sheet_hint=sheet_hint)
            if snap:
                return ResolvedDep(original=raw, a1=snap, kind="anchor_snapshot", note=f"{raw} → {snap}")
            return ResolvedDep(original=raw, a1="", kind="unresolved", note=f"{raw} snapshot unavailable")

    if _has_table_all_shape(raw):
        m_table = _TABLE_ALL_RE.match(raw)
        if m_table:
            name = m_table.group(1)
            ref = model.tables.get(name)
            if ref:
                return ResolvedDep(original=raw, a1=ref.replace("$", ""), kind="table_snapshot", note=f"{name}[#All] → {ref}")
            return ResolvedDep(original=raw, a1="", kind="unresolved", note=f"unknown table {name!r}")

    cleaned = raw.replace("$", "")
    if _has_range_shape(cleaned) and _RANGE_RE.match(cleaned):
        return ResolvedDep(original=raw, a1=cleaned, kind="range")

    return ResolvedDep(original=raw, a1="", kind="unresolved", note=f"unrecognized dep {raw!r}")


def resolve_deps(deps: list[str], model: ExcelWorkbookModel, *, sheet_hint: str = "") -> list[ResolvedDep]:
    return [resolve_dep(d, model, sheet_hint=sheet_hint) for d in deps]
