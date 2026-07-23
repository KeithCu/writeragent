# SPDX-License-Identifier: GPL-3.0-or-later
"""Data models for Excel ↔ DAG-style ``=PY`` conversion."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

HeaderMode = Literal["true", "false", "omit"]
DepRole = Literal["data", "ordering"]


@dataclass
class SheetInfo:
    """One workbook sheet with stable identity."""

    title: str
    order: int
    part_name: str  # e.g. xl/worksheets/sheet1.xml


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

    def sheet_order_map(self) -> dict[str, int]:
        return {s.title: s.order for s in self.sheets}

    def to_dict(self) -> dict[str, Any]:
        return {
            "scripts": list(self.scripts),
            "cells": [asdict(c) for c in self.cells],
            "sheets": [asdict(s) for s in self.sheets],
            "tables": dict(self.tables),
            "anchor_snapshots": dict(self.anchor_snapshots),
            "source_path": self.source_path,
        }

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
            SheetInfo(title=str(s["title"]), order=int(s["order"]), part_name=str(s.get("part_name") or ""))
            for s in data.get("sheets") or []
        ]
        return cls(
            scripts=[str(s) for s in data.get("scripts") or []],
            cells=cells,
            sheets=sheets,
            tables={str(k): str(v) for k, v in (data.get("tables") or {}).items()},
            anchor_snapshots={str(k): str(v) for k, v in (data.get("anchor_snapshots") or {}).items()},
            source_path=str(data.get("source_path") or ""),
        )


@dataclass
class BindingInfo:
    """One normalized data binding after dedup."""

    a1: str
    header_mode: HeaderMode = "omit"
    role: DepRole = "data"
    original_indices: list[int] = field(default_factory=list)  # original %P positions (0-based)


@dataclass
class ConvertedCell:
    """One cell after conversion."""

    sheet: str
    cell: str
    direction: str
    original_code: str
    converted_code: str
    data_args: list[str] = field(default_factory=list)
    ordering_args: list[str] = field(default_factory=list)
    bindings: list[BindingInfo] = field(default_factory=list)
    dag_formula: str = ""
    excel_formula: str = ""
    issues: list[str] = field(default_factory=list)
    shared_kernel: bool = False
    snapshot_deps: list[str] = field(default_factory=list)
    return_type: int = 0
    converted: bool = True
    array_ref: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "sheet": self.sheet,
            "cell": self.cell,
            "direction": self.direction,
            "original_code": self.original_code,
            "converted_code": self.converted_code,
            "data_args": list(self.data_args),
            "ordering_args": list(self.ordering_args),
            "bindings": [asdict(b) for b in self.bindings],
            "dag_formula": self.dag_formula,
            "excel_formula": self.excel_formula,
            "issues": list(self.issues),
            "shared_kernel": self.shared_kernel,
            "snapshot_deps": list(self.snapshot_deps),
            "return_type": self.return_type,
            "converted": self.converted,
            "array_ref": self.array_ref,
        }


@dataclass
class ConversionReport:
    """Full workbook conversion report."""

    direction: str
    source_path: str = ""
    cells: list[ConvertedCell] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        if self.issues:
            return False
        return all(c.converted for c in self.cells)

    def to_dict(self) -> dict[str, Any]:
        return {
            "direction": self.direction,
            "source_path": self.source_path,
            "ok": self.ok,
            "issues": list(self.issues),
            "cells": [c.to_dict() for c in self.cells],
        }
