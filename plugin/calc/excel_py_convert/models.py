# SPDX-License-Identifier: GPL-3.0-or-later
"""Data models for Excel ↔ DAG-style ``=PY`` conversion.

Excel structured-table / spill tokens (``Table[#All]``, ``ANCHORARRAY(...)``)
===========================================================================
Calc cannot evaluate those tokens today. On import we **snapshot** them to A1
ranges on the DAG ``=PY`` formula so the workbook can run. We also keep the
original Excel tokens in ``ConvertedCell.excel_deps`` (and package/udprop meta)
**only so export can put them back on ``_xlws.PY``** for round-trip fidelity.

This is an interchange hack, not Calc Table/spill support. LibreOffice/Calc
should grow real structured references and dynamic-array spill later; until
then, growing tables / live spill parents will not update the snapped A1 args.
See docs/scripting/ms-py-compatibility.md §5.8.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

# Cast/docs aliases only — CrossHair cannot proxy Literal in parameters or dataclass
# fields on the type heap (use str there).
HeaderMode = Literal["true", "false", "omit"]


@dataclass
class SheetInfo:
    """One workbook sheet with stable identity."""

    title: str
    order: int
    part_name: str  # e.g. xl/worksheets/sheet1.xml
    rel_id: str = ""  # relationship Id (r:id or Id) in workbook.xml


@dataclass
class ExcelPyCell:
    """One ``_xlfn._xlws.PY(scriptIndex, returnType, …deps)`` cell."""

    sheet: str  # human title
    cell: str
    script_index: int
    return_type: int
    deps: list[str] = field(default_factory=list)
    formula_raw: str = ""
    array_ref: str = ""  # formula @ref spill range when present (e.g. G13:I268)
    row: int = 0
    col: int = 0


@dataclass
class ExcelWorkbookModel:
    """Parsed Excel Python-in-Excel workbook (scripts + PY cells + tables)."""

    scripts: list[str] = field(default_factory=list)
    cells: list[ExcelPyCell] = field(default_factory=list)
    sheets: list[SheetInfo] = field(default_factory=list)
    # Qualified table refs: name → "'Sheet'.A1:B10" or "Sheet.A1:B10"
    tables: dict[str, str] = field(default_factory=dict)
    # Anchor/spill snapshots: "Sheet!A6" or "A6" → A1 range
    anchor_snapshots: dict[str, str] = field(default_factory=dict)
    source_path: str = ""
    issues: list[str] = field(default_factory=list)

    def sheet_order_map(self) -> dict[str, int]:
        return {s.title: s.order for s in self.sheets}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ExcelWorkbookModel:
        cells = [
            ExcelPyCell(
                sheet=str(c.get("sheet", "Sheet1")),
                cell=str(c["cell"]),
                script_index=int(c["script_index"]),
                return_type=int(c.get("return_type", 0)),
                deps=list(c.get("deps") or []),
                formula_raw=str(c.get("formula_raw") or ""),
                array_ref=str(c.get("array_ref") or ""),
                row=int(c.get("row") or 0),
                col=int(c.get("col") or 0),
            )
            for c in data.get("cells") or []
        ]
        sheets = [
            SheetInfo(
                title=str(s["title"]),
                order=int(s["order"]),
                part_name=str(s.get("part_name") or ""),
                rel_id=str(s.get("rel_id") or ""),
            )
            for s in data.get("sheets") or []
        ]
        return cls(
            scripts=[str(s) for s in data.get("scripts") or []],
            cells=cells,
            sheets=sheets,
            tables={str(k): str(v) for k, v in (data.get("tables") or {}).items()},
            anchor_snapshots={str(k): str(v) for k, v in (data.get("anchor_snapshots") or {}).items()},
            source_path=str(data.get("source_path") or ""),
            issues=[str(i) for i in (data.get("issues") or [])],
        )


def _as_header_mode(value: object) -> HeaderMode:
    if value == "true":
        return "true"
    if value == "false":
        return "false"
    return "omit"


@dataclass
class BindingInfo:
    """One normalized data binding after dedup."""

    a1: str
    header_mode: str = "omit"  # HeaderMode values; str for CrossHair
    role: str = "data"  # "data" | "ordering"; str for CrossHair
    original_indices: list[int] = field(default_factory=list)  # original %P positions (0-based)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BindingInfo:
        hm = _as_header_mode(data.get("header_mode"))
        role = "ordering" if data.get("role") == "ordering" else "data"
        return cls(
            a1=str(data.get("a1") or ""),
            header_mode=hm,
            role=role,
            original_indices=list(data.get("original_indices") or []),
        )


@dataclass
class ConvertedCell:
    """One cell after conversion."""

    sheet: str
    cell: str
    direction: str
    original_code: str
    converted_code: str
    data_args: list[str] = field(default_factory=list)
    ordering_args: list[str] = field(default_factory=list)  # legacy; converter relies on shared-kernel session
    # Parallel to data_args: original Excel dep tokens for _xlws.PY export (see module doc).
    excel_deps: list[str] = field(default_factory=list)
    bindings: list[BindingInfo] = field(default_factory=list)
    dag_formula: str = ""
    excel_formula: str = ""
    issues: list[str] = field(default_factory=list)
    shared_kernel: bool = False
    snapshot_deps: list[str] = field(default_factory=list)
    return_type: int = 0
    converted: bool = True
    array_ref: str = ""
    # Excel pythonScripts.xml index (0-based); retained for diagnostics / round-trip.
    script_index: int = -1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ConvertedCell:
        bindings: list[BindingInfo] = []
        for b in data.get("bindings") or []:
            if isinstance(b, dict):
                bindings.append(BindingInfo.from_dict(b))
        return cls(
            sheet=str(data.get("sheet") or "Sheet1"),
            cell=str(data.get("cell") or "A1"),
            direction=str(data.get("direction") or "dag"),
            original_code=str(data.get("original_code") or ""),
            converted_code=str(data.get("converted_code") or ""),
            data_args=[str(a) for a in (data.get("data_args") or [])],
            ordering_args=[str(a) for a in (data.get("ordering_args") or [])],
            excel_deps=[str(a) for a in (data.get("excel_deps") or [])],
            bindings=bindings,
            dag_formula=str(data.get("dag_formula") or ""),
            excel_formula=str(data.get("excel_formula") or ""),
            issues=[str(i) for i in (data.get("issues") or [])],
            shared_kernel=bool(data.get("shared_kernel")),
            snapshot_deps=[str(s) for s in (data.get("snapshot_deps") or [])],
            return_type=int(data.get("return_type") or 0),
            converted=bool(data.get("converted", True)),
            array_ref=str(data.get("array_ref") or ""),
            script_index=int(data["script_index"]) if data.get("script_index") is not None else -1,
        )


@dataclass
class ConversionReport:
    """Full workbook conversion report."""

    direction: str
    source_path: str = ""
    cells: list[ConvertedCell] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)

    @property
    def has_conversions(self) -> bool:
        return any(c.converted for c in self.cells)

    @property
    def ok(self) -> bool:
        return not self.issues and all(c.converted for c in self.cells)

    def to_dict(self) -> dict[str, Any]:
        return {"direction": self.direction, "source_path": self.source_path, "ok": self.ok, "issues": list(self.issues), "cells": [c.to_dict() for c in self.cells]}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ConversionReport:
        cells = [ConvertedCell.from_dict(c) for c in (data.get("cells") or []) if isinstance(c, dict)]
        return cls(direction=str(data.get("direction") or "dag"), source_path=str(data.get("source_path") or ""), cells=cells, issues=[str(i) for i in (data.get("issues") or [])])
